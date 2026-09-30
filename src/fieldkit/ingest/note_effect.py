"""Atomic, source-owned publication of an already-prepared meeting note."""

from pathlib import Path

from fieldkit.config import get_fieldkit_data
from fieldkit.ingest.prepared import (
    ReplayIntent,
    owns_prepared_note,
    prepared_note_marker,
    resolve_prepared_path,
)
from fieldkit.util.atomic import atomic_text_create, exclusive_file_lock, prepare_runtime_lock_path
from fieldkit.util.text_snapshot import read_text_snapshot

NOTE_WRITE_TIMEOUT_SECONDS = 5
MAX_NOTE_BYTES = 4_000_000


def _read_note(path: Path) -> str | None:
    """Read a bounded, stable regular file without following a leaf redirect."""
    try:
        return read_text_snapshot(path, max_bytes=MAX_NOTE_BYTES).content
    except FileNotFoundError:
        return None
    except ValueError:
        raise ValueError("Cannot verify prepared note file") from None


def publish_prepared_note(intent: ReplayIntent, workspace: Path) -> Path:
    """Publish new output or preserve proven ownership without overwriting edits.

    The caller must commit the prepared journal before invoking file effects.
    Unmarked existing notes are not adopted, even when their bytes match.
    The workspace directory namespace must remain stable; preflight path checks
    do not defend against a hostile same-user process replacing ancestors.
    """
    validated = intent.prepared
    marker = prepared_note_marker(validated.source_id, intent.digest)
    content = validated.note_content + "\n\n" + marker + "\n"
    if not owns_prepared_note(content, marker):
        raise ValueError("Invalid prepared note content")
    path = resolve_prepared_path(validated, workspace)
    lock_path = prepare_runtime_lock_path(path, get_fieldkit_data(), "meeting-note")
    with exclusive_file_lock(lock_path, timeout_seconds=NOTE_WRITE_TIMEOUT_SECONDS):
        resolve_prepared_path(validated, workspace)
        existing = _read_note(path)
        if existing is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            resolve_prepared_path(validated, workspace)
            try:
                atomic_text_create(path, content)
            except FileExistsError:
                resolve_prepared_path(validated, workspace)
            existing = _read_note(path)
        if existing is None or not owns_prepared_note(existing, marker):
            raise ValueError("Conflicting prepared note ownership")
    return path
