"""Prepared note publication is atomic and preserves only proven owned output."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.config import get_fieldkit_data
from fieldkit.ingest.note_effect import publish_prepared_note
from fieldkit.ingest.paths import compute_vault_path
from fieldkit.ingest.prepared import PreparedMeeting, ReplayIntent
from fieldkit.util.atomic import PathLockTimeoutError, exclusive_file_lock, prepare_runtime_lock_path

pytestmark = pytest.mark.unit
_NOTE_PROBE_TIMEOUT_SECONDS = 10


def _prepared(note: str = "# Meeting\nReviewed proposal.\n") -> PreparedMeeting:
    return PreparedMeeting(
        schema_version=1,
        pipeline_id="transcript-ingest",
        source_id="source-1",
        pipeline_version="0.1.0",
        vault_relative_path=compute_vault_path(
            Path(), "acme", "2026-09-27", "Meeting", source_id="source-1"
        ).as_posix(),
        note_content=note,
        note_sha256=hashlib.sha256(note.encode("utf-8")).hexdigest(),
        account="acme",
        meeting_date="2026-09-27",
        meeting_title="Meeting",
        pursuits=(),
        action_items=(),
        tasks=(),
        degraded=False,
    )


def test_new_note_is_owned_and_retry_preserves_edits(tmp_path: Path) -> None:
    prepared = _prepared()
    path = publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path == tmp_path / prepared.vault_relative_path
    original = path.read_text(encoding="utf-8")
    assert prepared.note_content in original
    edited = original.replace("Reviewed proposal.", "Reviewed and approved revised proposal.")
    path.write_text(edited, encoding="utf-8")
    assert publish_prepared_note(ReplayIntent(prepared), tmp_path) == path
    assert path.read_text(encoding="utf-8") == edited
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", ["unmarked", "duplicate", "different", "fenced", "orphan", "not_last"])
def test_ambiguous_existing_note_is_not_overwritten(tmp_path: Path, mutation: str) -> None:
    prepared = _prepared()
    path = publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path.is_file()
    original = path.read_text(encoding="utf-8")
    marker = original[original.index("<!-- fieldkit-ingest-note:") :].strip()
    altered = {
        "unmarked": prepared.note_content,
        "duplicate": original + marker + "\n",
        "different": original.replace(":v1:", ":v2:"),
        "fenced": "```markdown\n" + original + "```\n",
        "orphan": marker + "\n",
        "not_last": original + "Content after ownership footer\n",
    }[mutation]
    path.write_text(altered, encoding="utf-8")
    with pytest.raises(ValueError, match="Conflicting prepared note ownership"):
        publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path.read_text(encoding="utf-8") == altered


def test_different_saved_intent_cannot_adopt_existing_note(tmp_path: Path) -> None:
    prepared = _prepared()
    path = publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path.is_file()
    original = path.read_bytes()
    changed = prepared.model_copy(update={"degraded": True})
    with pytest.raises(ValueError, match="Conflicting prepared note ownership"):
        publish_prepared_note(ReplayIntent(changed), tmp_path)
    assert path.read_bytes() == original


def test_failed_first_publication_leaves_no_partial_note(tmp_path: Path) -> None:
    prepared = _prepared()
    path = tmp_path / prepared.vault_relative_path
    with (
        patch("fieldkit.util.atomic.os.link", side_effect=OSError("publication failed")),
        pytest.raises(OSError, match="publication failed"),
    ):
        publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert not path.exists()
    assert not list(path.parent.glob("*.tmp"))


def test_note_publication_does_not_clobber_racing_writer(tmp_path: Path) -> None:
    prepared = _prepared()
    path = tmp_path / prepared.vault_relative_path
    original_link = os.link

    def race(source: Path, destination: Path) -> None:
        destination.write_text("Other writer's note", encoding="utf-8")
        original_link(source, destination)

    with (
        patch("fieldkit.util.atomic.os.link", side_effect=race),
        pytest.raises(ValueError, match="Conflicting prepared note ownership"),
    ):
        publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path.read_text(encoding="utf-8") == "Other writer's note"


def test_note_publication_accepts_racing_identical_owned_output(tmp_path: Path) -> None:
    prepared = _prepared()
    original_link = os.link

    def race(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())
        original_link(source, destination)

    with patch("fieldkit.util.atomic.os.link", side_effect=race):
        path = publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path == tmp_path / prepared.vault_relative_path
    assert prepared.note_content in path.read_text(encoding="utf-8")


def test_prepared_note_whitespace_is_preserved_exactly(tmp_path: Path) -> None:
    prepared = _prepared("# Meeting\nBody with trailing spaces  \n\n\n")
    path = publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path.read_text(encoding="utf-8").startswith(prepared.note_content + "\n\n")


def test_note_lock_lives_only_under_runtime_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr("fieldkit.ingest.note_effect.get_fieldkit_data", lambda: runtime)
    path = publish_prepared_note(ReplayIntent(_prepared()), workspace)
    assert path.is_file()
    lock = prepare_runtime_lock_path(path, runtime, "meeting-note")
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600
    assert not list(workspace.rglob("*.lock"))


def test_note_lock_is_bounded_and_does_not_publish_on_contention(tmp_path: Path) -> None:
    prepared = _prepared()
    path = tmp_path / prepared.vault_relative_path
    with (
        exclusive_file_lock(prepare_runtime_lock_path(path, get_fieldkit_data(), "meeting-note")),
        patch("fieldkit.ingest.note_effect.NOTE_WRITE_TIMEOUT_SECONDS", 0),
        pytest.raises(PathLockTimeoutError, match="Timed out acquiring path lock"),
    ):
        publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert not path.exists()


def test_note_rejects_redirected_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "accounts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Prepared output escapes workspace"):
        publish_prepared_note(ReplayIntent(_prepared()), workspace)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("note", ["```markdown\nMeeting", "<!--\nMeeting"])
def test_unpublishable_note_cannot_hide_or_inject_ownership(tmp_path: Path, note: str) -> None:
    with pytest.raises(ValueError, match="Invalid prepared note content"):
        prepared = _prepared(note)
        publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_prepared_record_rejects_note_marker_injection() -> None:
    with pytest.raises(ValueError, match="Prepared note uses reserved ownership namespace"):
        _prepared("<!-- fieldkit-ingest-note:v1:forged -->")


def test_existing_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    prepared = _prepared()
    path = tmp_path / prepared.vault_relative_path
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    probe = (
        "import sys\nfrom pathlib import Path\n"
        "from fieldkit.ingest.prepared import ReplayIntent, decode_prepared\n"
        "from fieldkit.ingest.note_effect import publish_prepared_note\n"
        "try:\n publish_prepared_note(ReplayIntent(decode_prepared(sys.argv[1])), Path(sys.argv[2]))\n"
        "except ValueError as exc:\n print(str(exc))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, prepared.model_dump_json(), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=_NOTE_PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "Cannot verify prepared note file"


@pytest.mark.parametrize("payload", [b"x" * 9, "界界界".encode()])
def test_oversized_existing_note_is_rejected_before_parsing(tmp_path: Path, payload: bytes) -> None:
    prepared = _prepared()
    path = publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path.is_file()
    path.write_bytes(payload)
    with (
        patch("fieldkit.ingest.note_effect.MAX_NOTE_BYTES", 8, create=True),
        pytest.raises(ValueError, match="Cannot verify prepared note file"),
    ):
        publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert path.read_bytes() == payload


@pytest.mark.parametrize("kind", ["directory", "symlink", "invalid_utf8"])
def test_unverifiable_existing_note_is_never_read_as_owned_output(tmp_path: Path, kind: str) -> None:
    prepared = _prepared()
    path = tmp_path / prepared.vault_relative_path
    path.parent.mkdir(parents=True)
    other = tmp_path / "other.md"
    other.write_text("Unrelated", encoding="utf-8")
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(other)
    else:
        path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match=r"Cannot verify prepared note file|redirects its authorized destination"):
        publish_prepared_note(ReplayIntent(prepared), tmp_path)
    assert other.read_text(encoding="utf-8") == "Unrelated"
