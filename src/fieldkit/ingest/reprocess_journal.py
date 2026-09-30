"""Versioned, immutable intent for replacing one existing meeting artifact."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fieldkit.ingest.paths import validate_meeting_relative_path
from fieldkit.ingest.reprocess_note import validate_reprocess_content
from fieldkit.ingest.writeback import parse_meeting_frontmatter
from fieldkit.util.json_decode import unique_json_object
from fieldkit.util.workspace_paths import resolve_workspace_output

# JSON can encode one note byte as six characters; metadata also needs room.
MAX_REPROCESS_BYTES = 24_100_000
MAX_REPROCESS_RECORDS = 1000
MAX_REPROCESS_BATCH_BYTES = 64_000_000
_Version = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._+-]+$")]


class PreparedReprocess(BaseModel):
    """Exact final bytes and original artifact identity, independent of providers."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1]
    artifact_id: Annotated[str, Field(min_length=1, max_length=256)]
    source_id: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")]
    artifact_type: Literal["vault_note", "meeting-note"]
    recorded_artifact_path: Annotated[str, Field(min_length=1, max_length=4096)]
    workspace_root: Annotated[str, Field(min_length=1, max_length=4096)]
    relative_path: Annotated[str, Field(min_length=1, max_length=1024)]
    from_version: _Version
    to_version: _Version
    original_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    mode: Annotated[int, Field(ge=0, le=0o777)]
    note_content: Annotated[str, Field(min_length=1, max_length=4_000_000)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> int:
        """Do not interpret a JSON boolean as a schema version."""
        if type(value) is not int:
            raise ValueError("Invalid reprocess schema version")
        return value

    @model_validator(mode="after")
    def validate_intent(self) -> Self:
        """Authorize portable syntax, provenance, ownership, and target version."""
        validate_meeting_relative_path(self.relative_path)
        root = Path(self.workspace_root)
        artifact = Path(self.recorded_artifact_path)
        relative = Path(self.relative_path)
        if (
            not root.is_absolute()
            or "\x00" in self.workspace_root
            or "\x00" in self.recorded_artifact_path
            or root.as_posix() != self.workspace_root
            or ".." in root.parts
            or artifact.as_posix() != self.recorded_artifact_path
            or not artifact.is_absolute()
            or ".." in artifact.parts
            or artifact.parts[-len(relative.parts) :] != relative.parts
        ):
            raise ValueError("Invalid reprocess root binding")
        validate_reprocess_content(self.note_content, self.source_id, self.artifact_id)
        if parse_meeting_frontmatter(self.note_content).get("pipeline_version") != self.to_version:
            raise ValueError("Invalid reprocess version provenance")
        return self

    @property
    def path(self) -> Path:
        """The sole filesystem destination, derived from canonical intent fields."""
        return Path(self.workspace_root) / self.relative_path

    @property
    def recorded_root(self) -> Path:
        """Lexical root of the SQL-only spelling, checked before file effects."""
        return Path(self.recorded_artifact_path).parents[len(Path(self.relative_path).parts) - 1]

    def replacement_digest(self) -> str:
        """Hash the exact final publish bytes, including the ownership footer."""
        return hashlib.sha256(self.note_content.encode("utf-8")).hexdigest()


def decode_reprocess(raw: str) -> PreparedReprocess:
    """Reject oversized, ambiguous, or malformed journals without exposing data."""
    try:
        if len(raw) > MAX_REPROCESS_BYTES or len(raw.encode("utf-8")) > MAX_REPROCESS_BYTES:
            raise ValueError("Reprocess journal exceeds its bound")
        json.loads(raw, object_pairs_hook=unique_json_object)
        return PreparedReprocess.model_validate_json(raw)
    except (ValueError, RecursionError):
        raise ValueError("Invalid prepared reprocess output") from None


def resolve_reprocess_path(intent: PreparedReprocess, workspace: Path) -> Path:
    """Bind current roots and historical alias spelling to one canonical target."""
    try:
        root = workspace.resolve(strict=True)
        if str(root) != intent.workspace_root or intent.recorded_root.resolve(strict=True) != root:
            raise ValueError("Reprocess workspace differs from retained intent")
        path = resolve_workspace_output(root, intent.relative_path)
        if resolve_workspace_output(intent.recorded_root, intent.relative_path) != path:
            raise ValueError("Conflicting reprocess path identity")
        return path
    except (OSError, RuntimeError):
        raise ValueError("Cannot verify reprocess path identity") from None


def reprocess_checkpoint_key(artifact_id: str) -> str:
    """Keep artifact identifiers out of checkpoint keys."""
    return "prepared-reprocess:v1:" + hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()


def load_reprocess(conn: sqlite3.Connection, artifact_id: str) -> PreparedReprocess | None:
    """Load one bounded record and verify its checkpoint identity."""
    row = conn.execute(
        "SELECT CASE WHEN typeof(value) = 'text' AND length(CAST(value AS BLOB)) <= ? "
        "THEN value END FROM checkpoints WHERE pipeline_id = ? AND key = ?",
        (MAX_REPROCESS_BYTES, "transcript-ingest", reprocess_checkpoint_key(artifact_id)),
    ).fetchone()
    if row is None:
        return None
    return _decode_retained(reprocess_checkpoint_key(artifact_id), row[0])


def _decode_retained(key: object, raw: object) -> PreparedReprocess:
    """Validate the one canonical retained representation and checkpoint key."""
    if not isinstance(key, str) or not isinstance(raw, str):
        raise ValueError("Invalid prepared reprocess output")
    intent = decode_reprocess(raw)
    if raw != intent.model_dump_json():
        raise ValueError("Noncanonical prepared reprocess output")
    if key != reprocess_checkpoint_key(intent.artifact_id):
        raise ValueError("Invalid prepared reprocess identity")
    return intent


def load_reprocess_batch(conn: sqlite3.Connection) -> tuple[PreparedReprocess, ...]:
    """Validate every retained reprocess record under count and aggregate bounds.

    Unknown versions, wrong pipeline ownership, and misbound keys are errors,
    not records to silently skip. No caller may apply an item before this
    function returns the complete validated tuple.
    """
    rows = conn.execute(
        "SELECT CASE WHEN pipeline_id = 'transcript-ingest' THEN pipeline_id END, "
        "CASE WHEN length(CAST(key AS BLOB)) <= 128 THEN key END, "
        "CASE WHEN typeof(value) = 'text' AND length(CAST(value AS BLOB)) <= ? "
        "THEN value END FROM checkpoints WHERE key LIKE 'prepared-reprocess:%' ORDER BY key LIMIT ?",
        (MAX_REPROCESS_BYTES, MAX_REPROCESS_RECORDS + 1),
    )
    total_bytes = 0
    intents: list[PreparedReprocess] = []
    for pipeline, key, raw in rows:
        if len(intents) >= MAX_REPROCESS_RECORDS:
            raise ValueError("Reprocess journal count exceeds its bound")
        if pipeline != "transcript-ingest" or not isinstance(raw, str):
            raise ValueError("Invalid prepared reprocess output")
        total_bytes += len(raw.encode("utf-8"))
        if total_bytes > MAX_REPROCESS_BATCH_BYTES:
            raise ValueError("Reprocess journal batch exceeds its byte bound")
        intents.append(_decode_retained(key, raw))
    return tuple(intents)


def require_original_artifact(conn: sqlite3.Connection, intent: PreparedReprocess) -> None:
    """Refuse stale artifact identity or an uncompleted original source."""
    row = conn.execute(
        "SELECT 1 FROM artifacts a JOIN sources s ON s.source_id = a.source_id "
        "WHERE a.artifact_id = ? AND a.source_id = ? AND a.pipeline_id = 'transcript-ingest' "
        "AND a.artifact_type = ? AND a.content_path = ? AND a.pipeline_version = ? "
        "AND s.pipeline_id = 'transcript-ingest' AND s.status = 'processed'",
        (
            intent.artifact_id,
            intent.source_id,
            intent.artifact_type,
            intent.recorded_artifact_path,
            intent.from_version,
        ),
    ).fetchone()
    if row is None:
        raise ValueError("Conflicting reprocess artifact identity")


def save_reprocess(conn: sqlite3.Connection, intent: PreparedReprocess) -> PreparedReprocess:
    """Commit immutable replacement intent without committing caller work."""
    if conn.in_transaction:
        raise ValueError("Reprocess journal requires an idle connection")
    raw = intent.model_dump_json()
    validated = decode_reprocess(raw)
    resolve_reprocess_path(validated, Path(validated.workspace_root))
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        require_original_artifact(conn, validated)
        retained = load_reprocess(conn, validated.artifact_id)
        if retained is not None:
            if retained != validated:
                raise ValueError("Conflicting prepared reprocess output")
            return retained
        conn.execute(
            "INSERT INTO checkpoints (checkpoint_id, pipeline_id, key, value) VALUES (?, ?, ?, ?)",
            (str(uuid4()), "transcript-ingest", reprocess_checkpoint_key(validated.artifact_id), raw),
        )
    return validated


def complete_reprocess(conn: sqlite3.Connection, intent: PreparedReprocess) -> None:
    """Commit artifact advancement and journal deletion after verified publication.

    The caller holds the transcript run lock and note lock and has verified
    exact final file bytes and mode. This database operation does not publish
    files or independently prove their state.
    """
    if conn.in_transaction:
        raise ValueError("Reprocess completion requires an idle connection")
    validated = decode_reprocess(intent.model_dump_json())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        require_original_artifact(conn, validated)
        if load_reprocess(conn, validated.artifact_id) != validated:
            raise ValueError("Conflicting prepared reprocess output")
        updated = conn.execute(
            "UPDATE artifacts SET pipeline_version = ? WHERE artifact_id = ? AND source_id = ? "
            "AND pipeline_id = 'transcript-ingest' AND artifact_type = ? "
            "AND content_path = ? AND pipeline_version = ?",
            (
                validated.to_version,
                validated.artifact_id,
                validated.source_id,
                validated.artifact_type,
                validated.recorded_artifact_path,
                validated.from_version,
            ),
        )
        deleted = conn.execute(
            "DELETE FROM checkpoints WHERE pipeline_id = 'transcript-ingest' AND key = ? AND value = ?",
            (reprocess_checkpoint_key(validated.artifact_id), validated.model_dump_json()),
        )
        if updated.rowcount != 1 or deleted.rowcount != 1:
            raise ValueError("Conflicting reprocess completion state")
