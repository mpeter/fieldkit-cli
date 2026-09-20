"""Read stable ambience-companion JSONL session snapshots."""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fieldkit.ingest.constants import AMBIENT_TRANSCRIPT_PIPELINE

MAX_AMBIENT_SOURCE_BYTES = 5_000_000
MAX_AMBIENT_TRANSCRIPT_CHARS = MAX_AMBIENT_SOURCE_BYTES
_SESSION_RE = re.compile(r"^session-(\d{8}T\d{6}Z)\.jsonl$")
_REQUIRED_FIELDS = frozenset({"text", "started_at", "ended_at", "segment_id"})


class AmbientSourceError(ValueError):
    """An ambient source is unsafe, malformed, or changed while being read."""


@dataclass(frozen=True)
class AmbientSegment:
    """One validated speaker-labelled utterance."""

    text: str
    started_at: datetime
    ended_at: datetime
    segment_id: str
    speaker: str


@dataclass(frozen=True)
class AmbientSnapshot:
    """Immutable content-addressed view of one completed ambient session."""

    source_id: str
    file_hash: str
    path: Path
    relative_path: Path
    meeting_date: date
    segments: list[AmbientSegment]
    transcript: str


def get_ambient_root(fieldkit_home: Path) -> Path:
    """Return the canonical ambience-companion session directory."""
    return fieldkit_home / "scratch" / "ambient"


def _parse_timestamp(value: Any, *, line_number: int, field: str) -> datetime:
    if not isinstance(value, str):
        raise AmbientSourceError(f"line {line_number} has invalid timestamp in {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AmbientSourceError(f"line {line_number} has invalid timestamp in {field}") from exc
    if parsed.tzinfo is None:
        raise AmbientSourceError(f"line {line_number} has invalid timestamp in {field}: timezone required")
    return parsed.astimezone(UTC)


def _parse_segment(raw: object, *, line_number: int) -> AmbientSegment:
    if not isinstance(raw, dict):
        raise AmbientSourceError(f"line {line_number} must be a JSON object")
    missing = sorted(_REQUIRED_FIELDS - raw.keys())
    if missing:
        raise AmbientSourceError(f"line {line_number} missing required field(s): {', '.join(missing)}")

    text = raw["text"]
    segment_id = raw["segment_id"]
    speaker = raw.get("speaker", "")
    if not isinstance(text, str) or not text.strip():
        raise AmbientSourceError(f"line {line_number} text must be a nonblank string")
    if not isinstance(segment_id, str) or not segment_id.strip():
        raise AmbientSourceError(f"line {line_number} segment_id must be a nonblank string")
    if not isinstance(speaker, str):
        raise AmbientSourceError(f"line {line_number} speaker must be a string")

    started_at = _parse_timestamp(raw["started_at"], line_number=line_number, field="started_at")
    ended_at = _parse_timestamp(raw["ended_at"], line_number=line_number, field="ended_at")
    if ended_at < started_at:
        raise AmbientSourceError(f"line {line_number} ended_at precedes started_at")
    return AmbientSegment(
        text=text.strip(),
        started_at=started_at,
        ended_at=ended_at,
        segment_id=segment_id,
        speaker=speaker.strip(),
    )


def _meeting_date(path: Path, segments: list[AmbientSegment]) -> date:
    if segments:
        return segments[0].started_at.date()
    match = _SESSION_RE.fullmatch(path.name)
    if match is None:
        raise AmbientSourceError(f"invalid ambient session filename: {path.name}")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).date()


def load_ambient_snapshot(path: Path, *, ambient_root: Path) -> AmbientSnapshot:
    """Validate and read one stable, bounded session snapshot."""
    resolved_root = ambient_root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise AmbientSourceError(f"source is outside ambient root: {resolved_path}")
    if _SESSION_RE.fullmatch(resolved_path.name) is None:
        raise AmbientSourceError(f"invalid ambient session filename: {resolved_path.name}")

    before = resolved_path.stat()
    if before.st_size > MAX_AMBIENT_SOURCE_BYTES:
        raise AmbientSourceError(f"ambient source {resolved_path.name} exceeds {MAX_AMBIENT_SOURCE_BYTES} bytes")
    payload = resolved_path.read_bytes()
    after = resolved_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise AmbientSourceError(f"ambient source changed while reading: {resolved_path.name}")

    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AmbientSourceError(f"ambient source {resolved_path.name} is not valid UTF-8") from exc

    segments: list[AmbientSegment] = []
    seen_segment_ids: set[str] = set()
    for line_number, line in enumerate(decoded.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AmbientSourceError(f"line {line_number} is not valid JSON") from exc
        segment = _parse_segment(raw, line_number=line_number)
        if segment.segment_id in seen_segment_ids:
            raise AmbientSourceError(f"line {line_number} repeats segment_id {segment.segment_id}")
        seen_segment_ids.add(segment.segment_id)
        segments.append(segment)

    transcript = "\n".join(
        f"[{segment.started_at.isoformat()}] {segment.speaker + ': ' if segment.speaker else ''}{segment.text}"
        for segment in segments
    )
    if len(transcript) > MAX_AMBIENT_TRANSCRIPT_CHARS:
        raise AmbientSourceError(f"ambient transcript exceeds {MAX_AMBIENT_TRANSCRIPT_CHARS} rendered characters")
    digest = hashlib.sha256(payload).hexdigest()
    return AmbientSnapshot(
        source_id=f"ambient:{digest}",
        file_hash=digest,
        path=resolved_path,
        relative_path=resolved_path.relative_to(resolved_root),
        meeting_date=_meeting_date(resolved_path, segments),
        segments=segments,
        transcript=transcript,
    )


def discover_ambient_snapshots(ambient_root: Path, *, include_latest: bool = False) -> list[AmbientSnapshot]:
    """Return stable ambient snapshots, protecting the newest active file by default."""
    snapshots, errors = scan_ambient_snapshots(ambient_root, include_latest=include_latest)
    if errors:
        raise AmbientSourceError(errors[0][1])
    return snapshots


def scan_ambient_snapshots(
    ambient_root: Path, *, include_latest: bool = False
) -> tuple[list[AmbientSnapshot], list[tuple[Path, str]]]:
    """Read eligible snapshots while retaining per-file validation failures."""
    paths = sorted(ambient_root.glob("session-*.jsonl")) if ambient_root.is_dir() else []
    eligible = paths if include_latest else paths[:-1]
    snapshots: list[AmbientSnapshot] = []
    errors: list[tuple[Path, str]] = []
    for path in eligible:
        try:
            snapshots.append(load_ambient_snapshot(path, ambient_root=ambient_root))
        except (AmbientSourceError, OSError) as exc:
            errors.append((path, str(exc)))
    return snapshots, errors


def register_ambient_snapshots(
    conn: sqlite3.Connection,
    snapshots: list[AmbientSnapshot],
) -> list[AmbientSnapshot]:
    """Register snapshots as pending sources, once per content hash."""
    registered: list[AmbientSnapshot] = []
    try:
        for snapshot in snapshots:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO sources
                    (source_id, pipeline_id, file_path, file_hash, status, meeting_title, meeting_date)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    snapshot.source_id,
                    AMBIENT_TRANSCRIPT_PIPELINE,
                    snapshot.relative_path.as_posix(),
                    snapshot.file_hash,
                    snapshot.path.stem,
                    snapshot.meeting_date.isoformat(),
                ),
            )
            if cursor.rowcount == 1:
                registered.append(snapshot)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return registered
