"""Process registered ambient transcript snapshots into meeting artifacts."""

import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fieldkit.errors import LLMError
from fieldkit.ingest.ambient import AmbientSourceError, get_ambient_root, load_ambient_snapshot
from fieldkit.ingest.constants import AMBIENT_TRANSCRIPT_PIPELINE
from fieldkit.ingest.pipeline import compute_vault_path, stage1_clean, stage2_extract
from fieldkit.ingest.router import match_pursuits_for_account, route_by_content
from fieldkit.ingest.sources import claim_pending_source

_YAML_KEYWORDS = frozenset(("true", "false", "null", "yes", "no", "on", "off"))
_YAML_SPECIAL_STARTS = (":", "{", "[", "|", ">", "!", "&", "*", "#", "?", "-", '"', "'")
_YAML_NUMERIC_RE = re.compile(r"[+-]?(\d[\d_]*(\.\d*)?|\.\d+)([eE][+-]?\d+)?")

MIN_MEANINGFUL_TRANSCRIPT_CHARS = 100
AmbientStatus = Literal["processed", "skipped", "deferred", "failed"]


@dataclass(frozen=True)
class AmbientOutcome:
    """One source's terminal batch outcome."""

    source_id: str
    status: AmbientStatus
    content_path: str | None = None
    reason: str | None = None


def _yaml_scalar(value: object) -> str:
    """Render a bounded ambient frontmatter value as a safe YAML scalar."""
    if not isinstance(value, str):
        return str(value)
    if not _yaml_needs_quote(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_needs_quote(value: str) -> bool:
    """Return whether YAML could reinterpret or split a string value."""
    return (
        value == ""
        or ": " in value
        or "\n" in value
        or "\r" in value
        or "\x00" in value
        or "---" in value
        or value.startswith(_YAML_SPECIAL_STARTS)
        or value.lower() in _YAML_KEYWORDS
        or _YAML_NUMERIC_RE.fullmatch(value) is not None
    )


def _render_frontmatter(fields: list[tuple[str, object]]) -> str:
    lines = ["---"]
    for key, value in fields:
        if isinstance(value, list):
            lines.append(f"{key}:" if value else f"{key}: []")
            lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _set_source_status(conn: sqlite3.Connection, source_id: str, status: str) -> None:
    processed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if status == "processed" else None
    conn.execute(
        "UPDATE sources SET status = ?, processed_at = ? WHERE source_id = ?",
        (status, processed_at, source_id),
    )
    conn.commit()


def _insert_artifact(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    pipeline_version: str,
    artifact_type: str,
    content_path: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO artifacts
            (artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), source_id, AMBIENT_TRANSCRIPT_PIPELINE, artifact_type, content_path, pipeline_version),
    )
    conn.commit()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _render_note(
    *,
    source_id: str,
    source_path: str,
    meeting_date: str,
    account: str,
    pursuits: list[str],
    participants: list[str],
    action_items: list[str],
    key_decisions: list[str],
    key_topics: list[str],
    confidence: str,
    routing_confidence: str,
    pipeline_version: str,
    transcript: str,
) -> str:
    title = f"Ambient session {meeting_date}"
    fields: list[tuple[str, object]] = [
        ("account", account),
        ("type", "meeting"),
        ("source_id", source_id),
        ("source_path", source_path),
        ("source_format", "ambient-jsonl"),
        ("pipeline", AMBIENT_TRANSCRIPT_PIPELINE),
        ("pipeline_version", pipeline_version),
        ("meeting_date", meeting_date),
        ("meeting_title", title),
        ("accounts", [account]),
        ("pursuits", pursuits),
        ("participants", participants),
        ("action_items", action_items),
        ("key_decisions", key_decisions),
        ("key_topics", key_topics),
        ("confidence", confidence),
        ("routing_confidence", routing_confidence),
    ]
    frontmatter = _render_frontmatter(fields)
    pursuit_links = ""
    if pursuits:
        pursuit_links = "\n**Pursuits:** " + ", ".join(f"[{slug}](../pursuits/{slug}.md)" for slug in pursuits) + "\n"
    actions = ""
    if action_items:
        actions = "\n## Action Items\n\n" + "\n".join(f"- [ ] {item}" for item in action_items) + "\n"
    return (
        f"{frontmatter}\n\n# {title}\n\n**Date:** {meeting_date}\n"
        f"**Source:** `{source_path}`\n{pursuit_links}\n---\n\n## Transcript\n\n{transcript.strip()}\n{actions}"
    )


def process_ambient_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    fieldkit_home: Path,
    pipeline_version: str,
) -> AmbientOutcome:
    """Claim and process one pending ambient source."""
    if not claim_pending_source(conn, source_id):
        return AmbientOutcome(source_id, "skipped", reason="source was already claimed")

    row = conn.execute(
        "SELECT file_path, file_hash FROM sources WHERE source_id = ? AND pipeline_id = ?",
        (source_id, AMBIENT_TRANSCRIPT_PIPELINE),
    ).fetchone()
    if row is None:
        _set_source_status(conn, source_id, "failed")
        return AmbientOutcome(source_id, "failed", reason="source record is missing")

    try:
        ambient_root = get_ambient_root(fieldkit_home)
        snapshot = load_ambient_snapshot(ambient_root / row["file_path"], ambient_root=ambient_root)
        if snapshot.file_hash != row["file_hash"] or snapshot.source_id != source_id:
            raise AmbientSourceError("registered ambient source content changed")

        if len(snapshot.transcript.strip()) < MIN_MEANINGFUL_TRANSCRIPT_CHARS:
            _insert_artifact(
                conn,
                source_id=source_id,
                pipeline_version=pipeline_version,
                artifact_type="ambient-noise",
                content_path=None,
            )
            _set_source_status(conn, source_id, "processed")
            return AmbientOutcome(source_id, "skipped", reason="noise")

        route = route_by_content(snapshot.transcript)
        if route.accounts == ["unknown"] or len(route.accounts) != 1:
            _set_source_status(conn, source_id, "pending")
            return AmbientOutcome(source_id, "deferred", reason="account route is not unique")

        account = route.accounts[0]
        cleaned = stage1_clean(snapshot.transcript)
        meta = stage2_extract(cleaned)
        meta.accounts = [account]
        meta.pursuits = match_pursuits_for_account(account, keywords=meta.key_topics + meta.key_decisions)
        meeting_date = snapshot.meeting_date.isoformat()
        note = _render_note(
            source_id=source_id,
            source_path=snapshot.relative_path.as_posix(),
            meeting_date=meeting_date,
            account=account,
            pursuits=meta.pursuits,
            participants=meta.participants,
            action_items=meta.action_items,
            key_decisions=meta.key_decisions,
            key_topics=meta.key_topics,
            confidence=meta.confidence,
            routing_confidence=route.confidence.value,
            pipeline_version=pipeline_version,
            transcript=cleaned.text,
        )
        path = compute_vault_path(fieldkit_home, account, meeting_date, f"ambient-session-{source_id[-12:]}")
        _atomic_write(path, note)
        _insert_artifact(
            conn,
            source_id=source_id,
            pipeline_version=pipeline_version,
            artifact_type="vault_note",
            content_path=str(path),
        )
        _set_source_status(conn, source_id, "processed")
        return AmbientOutcome(source_id, "processed", content_path=str(path))
    except LLMError as exc:
        if exc.category == "auth":
            _set_source_status(conn, source_id, "pending")
            raise
        if exc.category == "rate-limit":
            _set_source_status(conn, source_id, "pending")
            return AmbientOutcome(source_id, "deferred", reason=str(exc))
        _set_source_status(conn, source_id, "failed")
        return AmbientOutcome(source_id, "failed", reason=str(exc))
    except (AmbientSourceError, OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        _set_source_status(conn, source_id, "failed")
        return AmbientOutcome(source_id, "failed", reason=str(exc))
