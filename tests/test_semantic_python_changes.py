"""Tests for the fail-closed semantic Python change classifier."""

from pathlib import Path

import pytest

from scripts import semantic_python_changes

pytestmark = pytest.mark.unit


def test_docstring_only_change_is_not_semantic() -> None:
    """Changing only documentation does not require the serial impact suite."""
    base = '"""Old module documentation."""\n\ndef example() -> int:\n    """Old function documentation."""\n    return 1\n'
    candidate = '"""New module documentation."""\n\ndef example() -> int:\n    """New function documentation."""\n    return 1\n'

    assert semantic_python_changes._source_changes_semantics(base, candidate) is False


def test_comment_only_change_is_not_semantic() -> None:
    """Comments do not change executable behavior."""
    assert semantic_python_changes._source_changes_semantics("value = 1\n", "# explanation\nvalue = 1\n") is False


def test_behavior_change_is_semantic() -> None:
    """A changed return value requires impact testing."""
    assert semantic_python_changes._source_changes_semantics("value = 1\n", "value = 2\n") is True


def test_invalid_candidate_is_fail_closed() -> None:
    """Invalid source requires impact testing rather than being treated as docs-only."""
    assert semantic_python_changes._source_changes_semantics("value = 1\n", "def incomplete(\n") is True


def test_changed_python_paths_fail_closed_when_git_cannot_read_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing base revision cannot waive impact testing."""
    monkeypatch.setattr(semantic_python_changes, "_changed_python_paths", lambda *_: ["src/example.py"])
    monkeypatch.setattr(semantic_python_changes, "_git_source", lambda *_: None)

    assert semantic_python_changes.has_semantic_python_changes("base", "head", tmp_path) is True


def test_changed_python_paths_include_deleted_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Deleting Python source remains behavior-affecting until impact tests pass."""
    captured: list[str] = []

    def _run_git(_: Path, *arguments: str) -> str:
        captured.extend(arguments)
        return "src/fieldkit/removed.py\nREADME.md\n"

    monkeypatch.setattr(semantic_python_changes, "_run_git", _run_git)

    assert semantic_python_changes._changed_python_paths("base", "head", tmp_path) == ["src/fieldkit/removed.py"]
    assert "--diff-filter=ACMRD" in captured


def test_working_tree_source_rejects_path_escape(tmp_path: Path) -> None:
    """Git path anomalies cannot make a semantic change appear docs-only."""
    assert semantic_python_changes._working_tree_source("../outside.py", tmp_path) is None


def test_uncommitted_semantic_change_requires_impact_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The local gate compares its base against the worktree, not only HEAD."""
    source_path = tmp_path / "src" / "example.py"
    source_path.parent.mkdir()
    source_path.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.setattr(semantic_python_changes, "_changed_python_paths", lambda *_: ["src/example.py"])
    monkeypatch.setattr(semantic_python_changes, "_git_source", lambda *_: "value = 1\n")

    assert semantic_python_changes.has_semantic_python_changes("base", "head", tmp_path) is True
