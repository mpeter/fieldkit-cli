"""Replay retained meeting replacements without providers or model calls."""

import hashlib
import sqlite3
import stat
from pathlib import Path

from fieldkit.ingest import note_effect
from fieldkit.ingest.db import ArtifactRow
from fieldkit.ingest.reprocess_journal import (
    PreparedReprocess,
    complete_reprocess,
    load_reprocess,
    load_reprocess_batch,
    require_original_artifact,
    resolve_reprocess_path,
    save_reprocess,
)
from fieldkit.ingest.reprocess_note import (
    ReprocessNote,
    capture_reprocess_note,
    render_reprocessed_content,
    validate_reprocess_content,
)
from fieldkit.util.atomic import atomic_text_write, exclusive_file_lock, prepare_runtime_lock_path
from fieldkit.util.text_snapshot import TextSnapshot, read_text_snapshot

MAX_REPROCESS_ARTIFACTS = 100_000


def _verify_current_note(intent: PreparedReprocess, path: Path) -> TextSnapshot:
    """Accept only the original bytes or exact retained final bytes and mode."""
    current = read_text_snapshot(path, max_bytes=note_effect.MAX_NOTE_BYTES)
    validate_reprocess_content(current.content, intent.source_id, intent.artifact_id)
    if stat.S_IMODE(current.info.st_mode) != intent.mode:
        raise ValueError("Reprocess note permissions changed")
    if (
        current.content != intent.note_content
        and hashlib.sha256(current.content.encode("utf-8")).hexdigest() != intent.original_sha256
    ):
        raise ValueError("Reprocess note changed after preparation")
    return current


def preflight_reprocess(conn: sqlite3.Connection, workspace: Path) -> tuple[PreparedReprocess, ...]:
    """Check the entire retained batch and transcript target ownership read-only.

    Call under the shared transcript run lock, before selecting or applying
    any recovery item. Replay rechecks mutable state under the note lock.
    """
    if conn.in_transaction:
        raise ValueError("Reprocess preflight requires an idle connection")
    intents = load_reprocess_batch(conn)
    targets: dict[Path, str] = {}
    for intent in intents:
        path = resolve_reprocess_path(intent, workspace)
        require_original_artifact(conn, intent)
        _verify_current_note(intent, path)
        if path in targets:
            raise ValueError("Ambiguous reprocess journal target")
        targets[path] = intent.artifact_id
    _require_unique_targets(conn, targets)
    return intents


def _require_unique_targets(conn: sqlite3.Connection, targets: dict[Path, str]) -> None:
    """Reject multiple transcript artifacts claiming a canonical destination."""
    if not targets:
        return
    rows = conn.execute(
        "SELECT CASE WHEN length(CAST(artifact_id AS BLOB)) <= 256 THEN artifact_id END, "
        "CASE WHEN typeof(content_path) = 'text' "
        "AND length(CAST(content_path AS BLOB)) <= 4096 THEN content_path END FROM artifacts "
        "WHERE pipeline_id = 'transcript-ingest' AND content_path IS NOT NULL ORDER BY artifact_id LIMIT ?",
        (MAX_REPROCESS_ARTIFACTS + 1,),
    )
    for index, (artifact_id, recorded) in enumerate(rows):
        if index >= MAX_REPROCESS_ARTIFACTS:
            raise ValueError("Reprocess artifact inventory exceeds its bound")
        if not isinstance(artifact_id, str) or not isinstance(recorded, str) or not Path(recorded).is_absolute():
            raise ValueError("Invalid transcript artifact path")
        try:
            path = Path(recorded).resolve()
        except (OSError, RuntimeError, ValueError):
            raise ValueError("Cannot verify transcript artifact target") from None
        if path in targets and targets[path] != artifact_id:
            raise ValueError("Ambiguous reprocess artifact target")


def prepare_reprocess(
    conn: sqlite3.Connection,
    *,
    artifact: ArtifactRow,
    note: ReprocessNote,
    content: str,
    to_version: str,
    runtime_root: Path,
) -> PreparedReprocess:
    """Retain exact replacement intent before effects, under the note lock.

    The caller holds the transcript run lock across preparation and replay.
    Revalidate the pre-provider snapshot before committing the journal; replay
    subsequently rechecks it and owns all file publication and completion.
    """
    if conn.in_transaction:
        raise ValueError("Reprocess preparation requires an idle connection")
    row = conn.execute("SELECT artifact_type FROM artifacts WHERE artifact_id = ?", (artifact.artifact_id,)).fetchone()
    if row is None or row[0] not in ("vault_note", "meeting-note"):
        raise ValueError("Invalid reprocess artifact type")
    intent = PreparedReprocess(
        schema_version=1,
        artifact_id=artifact.artifact_id,
        source_id=artifact.source_id,
        artifact_type=row[0],
        recorded_artifact_path=artifact.content_path,
        workspace_root=str(note.workspace),
        relative_path=note.relative_path,
        from_version=artifact.pipeline_version,
        to_version=to_version,
        original_sha256=hashlib.sha256(note.original.content.encode("utf-8")).hexdigest(),
        mode=stat.S_IMODE(note.original.info.st_mode),
        note_content=render_reprocessed_content(note, content),
    )
    path = resolve_reprocess_path(intent, note.workspace)
    lock = prepare_runtime_lock_path(path, runtime_root, "meeting-note")
    with exclusive_file_lock(lock, timeout_seconds=note_effect.NOTE_WRITE_TIMEOUT_SECONDS):
        resolve_reprocess_path(intent, note.workspace)
        current = capture_reprocess_note(artifact, intent.recorded_root)
        if (
            current.original.identity != note.original.identity
            or current.original.content != note.original.content
            or stat.S_IMODE(current.original.info.st_mode) != intent.mode
        ):
            raise ValueError("Reprocess note changed after preparation")
        _require_unique_targets(conn, {path: artifact.artifact_id})
        return save_reprocess(conn, intent)


def replay_reprocess(
    conn: sqlite3.Connection,
    artifact_id: str,
    workspace: Path,
    *,
    runtime_root: Path,
) -> Path:
    """Apply exact saved bytes and complete while retaining the shared note lock.

    The caller holds the database-specific transcript run lock. A previously
    applied replacement is verified rather than rewritten. Any conflicting
    file, mode, root, or database identity retains the journal and fails closed.
    Directory namespaces must remain stable against hostile same-user renames.
    """
    if conn.in_transaction:
        raise ValueError("Reprocess replay requires an idle connection")
    intent = load_reprocess(conn, artifact_id)
    if intent is None:
        raise ValueError("Reprocess replay requires retained intent")
    path = resolve_reprocess_path(intent, workspace)
    require_original_artifact(conn, intent)
    lock = prepare_runtime_lock_path(path, runtime_root, "meeting-note")
    with exclusive_file_lock(lock, timeout_seconds=note_effect.NOTE_WRITE_TIMEOUT_SECONDS):
        resolve_reprocess_path(intent, workspace)
        require_original_artifact(conn, intent)
        current = _verify_current_note(intent, path)
        if current.content != intent.note_content:
            atomic_text_write(path, intent.note_content, mode=intent.mode)
        resolve_reprocess_path(intent, workspace)
        final = read_text_snapshot(path, max_bytes=note_effect.MAX_NOTE_BYTES)
        if final.content != intent.note_content or stat.S_IMODE(final.info.st_mode) != intent.mode:
            raise ValueError("Reprocess note changed before completion")
        complete_reprocess(conn, intent)
    return path
