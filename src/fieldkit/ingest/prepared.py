"""Strict immutable intent records for transcript output preparation."""

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fieldkit.ingest.paths import compute_vault_path
from fieldkit.tasks.classifier import ClassifiedItem, ItemClass
from fieldkit.tasks.effects import TaskEffect, validate_effect
from fieldkit.util.json_decode import unique_json_object
from fieldkit.util.workspace_paths import resolve_workspace_output, validate_relative_output_path

MAX_PREPARED_BYTES = 4_000_000
NOTE_MARKER_PREFIX = "fieldkit-ingest-note:"
_BoundedText = Annotated[str, Field(max_length=8192)]
_SourceId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")]
_Slug = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")]


def prepared_note_marker(source_id: str, intent_digest: str) -> str:
    """Bind a note footer to its source and original prepared intent."""
    if re.fullmatch(r"[a-f0-9]{64}", intent_digest) is None:
        raise ValueError("Invalid prepared note identity")
    source_digest = hashlib.sha256((NOTE_MARKER_PREFIX + "v1:" + source_id).encode("utf-8")).hexdigest()
    return f"<!-- {NOTE_MARKER_PREFIX}v1:{source_digest}:{intent_digest} -->"


def owns_prepared_note(content: str, marker: str) -> bool:
    """Require exactly one real top-level marker and nonempty note content."""
    if (
        content.count(NOTE_MARKER_PREFIX) != 1
        or not content.replace(marker, "", 1).strip()
        or not content.rstrip().endswith(marker)
    ):
        return False
    return any(
        token.type == "html_block" and token.level == 0 and token.content.strip() == marker
        for token in MarkdownIt("commonmark").parse(content)
    )


def validate_pursuit_title(title: str) -> None:
    """Require a nonblank single-line title for a pursuit activity link."""
    if not title.strip() or any(unicodedata.category(char).startswith("C") or char in "\u2028\u2029" for char in title):
        raise ValueError("Invalid pursuit meeting title")


class PreparedTask(BaseModel):
    """Schema-v1 task decision, independent of the live classifier's types."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    text: _BoundedText
    cls: Literal["my_task", "waiting_on", "drop"]
    owner: _BoundedText
    rationale: _BoundedText
    pursuit_label: _BoundedText


class PreparedMeeting(BaseModel):
    """Exact note and task decisions, independent of later configuration changes."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1]
    pipeline_id: Literal["transcript-ingest"]
    source_id: _SourceId
    pipeline_version: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._+-]+$")]
    vault_relative_path: Annotated[str, Field(min_length=1, max_length=1024)]
    note_content: Annotated[str, Field(min_length=1, max_length=1_000_000)]
    note_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    account: _Slug
    meeting_date: Annotated[str, Field(min_length=10, max_length=10)]
    meeting_title: _BoundedText
    pursuits: Annotated[tuple[_Slug, ...], Field(max_length=1000)]
    action_items: Annotated[tuple[_BoundedText, ...], Field(max_length=2000)]
    tasks: Annotated[tuple[PreparedTask, ...], Field(max_length=2000)]
    degraded: bool

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> int:
        """Do not accept booleans as the integer schema version."""
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @field_validator("vault_relative_path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        """Reject traversal and ambiguous platform-dependent path syntax."""
        return validate_relative_output_path(value)

    @field_validator("meeting_date")
    @classmethod
    def calendar_date(cls, value: str) -> str:
        """Require a real calendar date in canonical ISO form."""
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Invalid meeting date")
        return value

    @model_validator(mode="after")
    def validate_contents(self) -> Self:
        """Bind note bytes and authorize the exact source-specific destination."""
        if NOTE_MARKER_PREFIX in self.note_content:
            raise ValueError("Prepared note uses reserved ownership namespace")
        # Fixed-width stand-in tests Markdown structure before an intent digest exists.
        marker = f"<!-- {NOTE_MARKER_PREFIX}v1:{'0' * 64}:{'0' * 64} -->"
        if not owns_prepared_note(self.note_content + "\n\n" + marker + "\n", marker):
            raise ValueError("Invalid prepared note content")
        if len({slug.casefold() for slug in self.pursuits}) != len(self.pursuits):
            raise ValueError("Prepared pursuit targets must be unique")
        if tuple(task.text for task in self.tasks) != self.action_items:
            raise ValueError("Prepared task decisions do not match action item positions")
        if self.pursuits:
            validate_pursuit_title(self.meeting_title)
        for position, task in enumerate(self.tasks):
            validate_effect(
                TaskEffect(
                    self.source_id,
                    position,
                    ClassifiedItem(task.text, ItemClass(task.cls), task.owner, task.rationale, task.pursuit_label),
                ),
                meeting_date=self.meeting_date,
                meeting_title=self.meeting_title,
            )
        if hashlib.sha256(self.note_content.encode("utf-8")).hexdigest() != self.note_sha256:
            raise ValueError("Prepared note digest mismatch")
        expected = compute_vault_path(
            Path(), self.account, self.meeting_date, self.meeting_title, source_id=self.source_id
        )
        if self.vault_relative_path != expected.as_posix():
            raise ValueError("Invalid meeting output destination")
        return self


def decode_prepared(raw: str) -> PreparedMeeting:
    """Decode a bounded journal record without reflecting sensitive input errors."""
    try:
        if len(raw) > MAX_PREPARED_BYTES or len(raw.encode("utf-8")) > MAX_PREPARED_BYTES:
            raise ValueError("Prepared output exceeds its bound")
        json.loads(raw, object_pairs_hook=unique_json_object)
        return PreparedMeeting.model_validate_json(raw)
    except (ValueError, RecursionError):
        raise ValueError("Invalid prepared ingest output") from None


def prepared_digest(prepared: PreparedMeeting) -> str:
    """Bind schema-v1 file effects and completion to the same complete intent."""
    return hashlib.sha256(prepared.model_dump_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReplayIntent:
    """Canonical validated intent and its once-computed effect fingerprint."""

    prepared: PreparedMeeting = field(repr=False)
    digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Do not trust model-copy construction or accept a caller-supplied digest."""
        canonical = decode_prepared(self.prepared.model_dump_json())
        object.__setattr__(self, "prepared", canonical)
        object.__setattr__(self, "digest", prepared_digest(canonical))


def resolve_prepared_path(prepared: PreparedMeeting, workspace: Path) -> Path:
    """Resolve the recorded relative path and reject existing symlink escapes."""
    return resolve_workspace_output(workspace, prepared.vault_relative_path)


def _checkpoint_key(source_id: str) -> str:
    """Keep source identifiers out of checkpoint keys."""
    return "prepared-output:v1:" + hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def load_prepared(conn: sqlite3.Connection, source_id: str) -> PreparedMeeting | None:
    """Load and validate retained intent without changing database state."""
    row = conn.execute(
        "SELECT CASE WHEN typeof(value) = 'text' AND length(CAST(value AS BLOB)) <= ? "
        "THEN value END FROM checkpoints WHERE pipeline_id = ? AND key = ?",
        (MAX_PREPARED_BYTES, "transcript-ingest", _checkpoint_key(source_id)),
    ).fetchone()
    if row is None:
        return None
    if not isinstance(row[0], str):
        raise ValueError("Invalid prepared ingest output")
    prepared = decode_prepared(row[0])
    if prepared.source_id != source_id:
        raise ValueError("Invalid prepared ingest output identity")
    return prepared


def save_prepared(conn: sqlite3.Connection, prepared: PreparedMeeting) -> PreparedMeeting:
    """Commit immutable intent for a claimed source; never commit caller work."""
    if conn.in_transaction:
        raise ValueError("Prepared output requires an idle connection")
    raw = prepared.model_dump_json()
    validated = decode_prepared(raw)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            "SELECT pipeline_id, status FROM sources WHERE source_id = ?", (validated.source_id,)
        ).fetchone()
        if source is None or tuple(source) != ("transcript-ingest", "in_progress"):
            raise ValueError("Prepared output requires a claimed transcript source")
        existing = load_prepared(conn, validated.source_id)
        if existing is not None:
            if existing != validated:
                raise ValueError("Conflicting prepared ingest output")
            return existing
        conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, pipeline_id, key, value) VALUES (?, ?, ?, ?)",
            (str(uuid4()), validated.pipeline_id, _checkpoint_key(validated.source_id), raw),
        )
    return validated


def complete_prepared(conn: sqlite3.Connection, prepared: PreparedMeeting, workspace: Path) -> Path:
    """Commit completion after the caller has successfully replayed every effect.

    The artifact identity binds the entire prepared payload, making an identical
    completion retry a no-op. This function does not apply or verify file effects.
    """
    if conn.in_transaction:
        raise ValueError("Prepared output requires an idle connection")
    validated = decode_prepared(prepared.model_dump_json())
    path = resolve_prepared_path(validated, workspace)
    artifact_id = "prepared-v1:" + prepared_digest(validated)
    expected_artifact = (artifact_id, validated.pipeline_id, str(path), validated.pipeline_version)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            "SELECT pipeline_id, status FROM sources WHERE source_id = ?", (validated.source_id,)
        ).fetchone()
        retained = load_prepared(conn, validated.source_id)
        artifacts = conn.execute(
            "SELECT artifact_id, pipeline_id, content_path, pipeline_version FROM artifacts "
            "WHERE source_id = ? AND artifact_type = 'vault_note' LIMIT 2",
            (validated.source_id,),
        ).fetchall()
        if (
            source is not None
            and tuple(source) == (validated.pipeline_id, "processed")
            and retained is None
            and [tuple(row) for row in artifacts] == [expected_artifact]
        ):
            return path
        if (
            source is None
            or tuple(source) != (validated.pipeline_id, "in_progress")
            or retained != validated
            or artifacts
        ):
            raise ValueError("Invalid prepared completion state")
        inserted = conn.execute(
            "INSERT INTO artifacts "
            "(artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version) "
            "VALUES (?, ?, ?, 'vault_note', ?, ?)",
            (artifact_id, validated.source_id, validated.pipeline_id, str(path), validated.pipeline_version),
        )
        updated = conn.execute(
            "UPDATE sources SET status = 'processed', processed_at = datetime('now') "
            "WHERE source_id = ? AND pipeline_id = ? AND status = 'in_progress'",
            (validated.source_id, validated.pipeline_id),
        )
        deleted = conn.execute(
            "DELETE FROM checkpoints WHERE pipeline_id = ? AND key = ?",
            (validated.pipeline_id, _checkpoint_key(validated.source_id)),
        )
        if (inserted.rowcount, updated.rowcount, deleted.rowcount) != (1, 1, 1):
            raise ValueError("Invalid prepared completion transition")
    return path
