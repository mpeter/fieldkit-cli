"""Task updates preserve old content on contention and replacement failure."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.config import get_fieldkit_data
from fieldkit.tasks.classifier import ClassifiedItem, ItemClass
from fieldkit.tasks.effects import TaskEffect
from fieldkit.tasks.writer import append_to_tasks
from fieldkit.util.atomic import PathLockTimeoutError, exclusive_file_lock, prepare_runtime_lock_path

pytestmark = pytest.mark.unit


def _append(path: Path, text: str = "Send proposal") -> tuple[int, int]:
    item = ClassifiedItem(text, ItemClass.MY_TASK, "Alice", "Assigned", "Acme")
    source_id = "source-" + str(len(text)) + "-" + text.replace(":", "").replace(" ", "-")
    return append_to_tasks(
        [TaskEffect(source_id, 0, item)],
        path,
        meeting_date="2026-09-27",
        meeting_title="Meeting",
        runtime_root=get_fieldkit_data(),
    )


def test_task_replay_preserves_user_edits(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    assert _append(path) == (1, 0)
    edited = path.read_text(encoding="utf-8").replace("- **[Acme]** Send proposal", "- [x] Sent revised proposal")
    path.write_text(edited, encoding="utf-8")
    assert _append(path) == (0, 0)
    assert path.read_text(encoding="utf-8") == edited


def test_task_heading_without_final_newline(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    path.write_text("## Active", encoding="utf-8")
    assert _append(path) == (1, 0)
    assert path.read_text(encoding="utf-8").startswith("## Active\n- ")
    assert _append(path) == (0, 0)


@pytest.mark.parametrize("kind", ["directory", "invalid_utf8", "oversize"])
def test_task_reader_refuses_unsafe_input(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "TASKS.md"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"\xff" if kind == "invalid_utf8" else b"x" * 101)
    with (
        patch("fieldkit.tasks.writer.MAX_TASK_BYTES", 100),
        pytest.raises(ValueError, match="Cannot read stable regular text file"),
    ):
        _append(path)
    assert (
        path.is_dir()
        if kind == "directory"
        else path.read_bytes() == (b"\xff" if kind == "invalid_utf8" else b"x" * 101)
    )


@pytest.mark.skipif(os.name != "posix", reason="FIFO probe requires POSIX")
def test_task_reader_refuses_fifo(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="Cannot read stable regular text file"):
        _append(path)


def test_task_update_respects_output_byte_bound(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    original = "## Active\n"
    path.write_text(original, encoding="utf-8")
    with patch("fieldkit.tasks.writer.MAX_TASK_BYTES", 100), pytest.raises(ValueError, match="exceeds byte limit"):
        _append(path)
    assert path.read_text(encoding="utf-8") == original


def test_task_moved_to_wrong_section_is_not_completed(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    assert _append(path) == (1, 0)
    changed = path.read_text(encoding="utf-8").replace("## Active", "## Waiting On")
    path.write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="Task provenance conflicts with saved decision"):
        _append(path)
    assert path.read_text(encoding="utf-8") == changed


def test_changed_decision_refuses_same_source_position(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    item = ClassifiedItem("Send proposal", ItemClass.MY_TASK, "Alice", "Assigned", "Acme")
    assert append_to_tasks(
        [TaskEffect("source", 0, item)], path, meeting_date="", meeting_title="", runtime_root=get_fieldkit_data()
    ) == (1, 0)
    original = path.read_bytes()
    changed = ClassifiedItem("Send revised proposal", ItemClass.MY_TASK, "Alice", "Assigned", "Acme")
    with pytest.raises(ValueError, match="Task provenance conflicts with saved decision"):
        append_to_tasks(
            [TaskEffect("source", 0, changed)],
            path,
            meeting_date="",
            meeting_title="",
            runtime_root=get_fieldkit_data(),
        )
    assert path.read_bytes() == original


def test_legacy_unmarked_task_is_not_silently_adopted(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    original = "## Active\n- **[Acme]** Send proposal\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="Unmarked task requires explicit reconciliation"):
        _append(path)
    assert path.read_text(encoding="utf-8") == original


def test_duplicate_batch_identity_fails_before_io(tmp_path: Path) -> None:
    path = tmp_path / "new" / "TASKS.md"
    item = ClassifiedItem("Send proposal", ItemClass.MY_TASK, "Alice", "Assigned", "Acme")
    effect = TaskEffect("source", 0, item)
    with pytest.raises(ValueError, match="Duplicate task effect identity"):
        append_to_tasks([effect, effect], path, meeting_date="", meeting_title="", runtime_root=get_fieldkit_data())
    assert not path.parent.exists()


@pytest.mark.parametrize("real_heading", [False, True])
def test_task_section_ignores_fenced_example(tmp_path: Path, real_heading: bool) -> None:
    path = tmp_path / "TASKS.md"
    example = "```markdown\n## Active\n- Example\n```\n"
    path.write_text(example + ("\n## Active\n" if real_heading else ""), encoding="utf-8")
    assert _append(path) == (1, 0)
    content = path.read_text(encoding="utf-8")
    assert content.startswith(example)
    assert "Send proposal" in content[len(example) :]
    assert _append(path) == (0, 0)


def test_duplicate_real_task_sections_leave_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    original = "## Active\n\n## Active\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="Ambiguous Markdown section"):
        _append(path)
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("opening", ["```markdown", "<!--", "<pre>"])
def test_unclosed_non_task_block_cannot_capture_new_task(tmp_path: Path, opening: str) -> None:
    path = tmp_path / "TASKS.md"
    original = opening + "\nExample\n"
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid or ambiguous task provenance"):
        _append(path)
    assert path.read_text(encoding="utf-8") == original


def test_task_lock_lives_only_under_runtime_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(__name__ + ".get_fieldkit_data", lambda: runtime)
    path = workspace / "TASKS.md"
    assert _append(path) == (1, 0)
    lock = prepare_runtime_lock_path(path, runtime, "tasks")
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600
    assert not list(workspace.rglob("*.lock"))


def test_task_lock_timeout_leaves_existing_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    path.write_text("## Active\n", encoding="utf-8")
    with (
        exclusive_file_lock(prepare_runtime_lock_path(path, get_fieldkit_data(), "tasks")),
        patch("fieldkit.tasks.writer.TASK_WRITE_TIMEOUT_SECONDS", 0, create=True),
        pytest.raises(PathLockTimeoutError, match="Timed out acquiring path lock"),
    ):
        _append(path)
    assert path.read_text(encoding="utf-8") == "## Active\n"


def test_task_replace_failure_preserves_old_content_and_cleans_temp(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    path.write_text("## Active\n", encoding="utf-8")
    with (
        patch.object(Path, "replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        _append(path)
    assert path.read_text(encoding="utf-8") == "## Active\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_task_writer_rejects_symlink_target(tmp_path: Path) -> None:
    other = tmp_path / "other.md"
    other.write_text("untouched", encoding="utf-8")
    path = tmp_path / "TASKS.md"
    path.symlink_to(other)
    with pytest.raises(ValueError, match="Refusing symlinked tasks file"):
        _append(path)
    assert other.read_text(encoding="utf-8") == "untouched"


def test_concurrent_task_writers_preserve_all_updates(tmp_path: Path) -> None:
    path = tmp_path / "TASKS.md"
    texts = [f"Task {index}: send proposal" for index in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda text: _append(path, text), texts))
    assert results == [(1, 0)] * len(texts)
    content = path.read_text(encoding="utf-8")
    assert all(content.count(text) == 1 for text in texts)
