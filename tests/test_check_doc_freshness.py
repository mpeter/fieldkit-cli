"""Unit tests for scripts/check_doc_freshness.py.

Tests cover frontmatter parsing, path validation, staleness detection,
git unavailability, and the exit-0 guarantee on all error paths.
"""

import importlib.util
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the script as a module (it lives in scripts/, not a package).
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_doc_freshness.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_freshness", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_doc(tmp_path: Path, name: str, content: str) -> Path:
    """Write a markdown file to tmp_path and return its path."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Frontmatter parsing tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_valid_frontmatter(tmp_path: Path) -> None:
    """Valid frontmatter returns last_reviewed and covers list."""
    doc = _make_doc(
        tmp_path,
        "test.md",
        "---\nlast_reviewed: 2026-06-21\ncovers:\n  - src/fieldkit/foo.py\n---\n# Doc\n",
    )
    last_reviewed, covers = _mod._parse_frontmatter(doc)
    assert last_reviewed == "2026-06-21"
    assert covers == ["src/fieldkit/foo.py"]


@pytest.mark.unit
def test_parse_missing_covers_key(tmp_path: Path) -> None:
    """Frontmatter with last_reviewed but no covers: returns empty list."""
    doc = _make_doc(
        tmp_path,
        "test.md",
        "---\nlast_reviewed: 2026-06-21\n---\n# Doc\n",
    )
    last_reviewed, covers = _mod._parse_frontmatter(doc)
    assert last_reviewed == "2026-06-21"
    assert covers == []


@pytest.mark.unit
def test_parse_no_frontmatter(tmp_path: Path) -> None:
    """Doc with no frontmatter returns (None, [])."""
    doc = _make_doc(tmp_path, "test.md", "# Just a heading\n\nNo frontmatter here.\n")
    last_reviewed, covers = _mod._parse_frontmatter(doc)
    assert last_reviewed is None
    assert covers == []


@pytest.mark.unit
def test_parse_malformed_yaml_skips_with_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Malformed YAML frontmatter prints a warning and returns (None, [])."""
    doc = _make_doc(
        tmp_path,
        "test.md",
        '---\nlast_reviewed: "2026-06-21\ncovers:\n  - src/foo.py\n---\n',
    )
    last_reviewed, covers = _mod._parse_frontmatter(doc)
    assert last_reviewed is None
    assert covers == []
    captured = capsys.readouterr()
    assert "malformed" in captured.err.lower() or "WARNING" in captured.err


@pytest.mark.unit
def test_parse_multiple_covers(tmp_path: Path) -> None:
    """Multiple covers: entries are all returned."""
    doc = _make_doc(
        tmp_path,
        "test.md",
        "---\nlast_reviewed: 2026-06-21\ncovers:\n  - src/fieldkit/a.py\n  - src/fieldkit/b.py\n---\n",
    )
    _, covers = _mod._parse_frontmatter(doc)
    assert covers == ["src/fieldkit/a.py", "src/fieldkit/b.py"]


@pytest.mark.unit
def test_invalid_date_format_warns_and_skips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Frontmatter with invalid last_reviewed format emits a warning and returns (None, [])."""
    doc = _make_doc(
        tmp_path,
        "test.md",
        "---\nlast_reviewed: 21-06-2026\ncovers:\n  - src/fieldkit/foo.py\n---\n# Doc\n",
    )
    last_reviewed, covers = _mod._parse_frontmatter(doc)
    assert last_reviewed is None
    assert covers == []
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "invalid last_reviewed format" in captured.err
    assert "YYYY-MM-DD" in captured.err


# ---------------------------------------------------------------------------
# Path validation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_path_validation_traversal_rejected(tmp_path: Path) -> None:
    """Path with .. that escapes repo root is rejected."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    assert not _mod._is_repo_relative("../../.git/config", repo_root)


@pytest.mark.unit
def test_path_validation_absolute_rejected(tmp_path: Path) -> None:
    """Absolute path is rejected."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    assert not _mod._is_repo_relative("/etc/passwd", repo_root)


@pytest.mark.unit
def test_path_validation_valid_passes(tmp_path: Path) -> None:
    """Valid repo-relative path passes."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    assert _mod._is_repo_relative("src/fieldkit/x.py", repo_root)


@pytest.mark.unit
def test_path_validation_nested_valid_passes(tmp_path: Path) -> None:
    """Nested repo-relative path passes."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    assert _mod._is_repo_relative("hooks/pre_commit.py", repo_root)


# ---------------------------------------------------------------------------
# Staleness detection tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_staleness_warns_when_commits_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When git log returns output, a staleness warning is printed."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # Create the covers path so existence check passes.
    src = repo_root / "src" / "fieldkit"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("# foo", encoding="utf-8")

    doc_path = tmp_path / "test.md"
    doc_path.write_text("# test", encoding="utf-8")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "abc123 Some commit\n"

    with patch("subprocess.run", return_value=mock_result):
        _mod._check_staleness(doc_path, "2026-06-01", "src/fieldkit/foo.py", repo_root)

    captured = capsys.readouterr()
    assert "stale" in captured.err.lower() or "WARNING" in captured.err
    assert "test.md" in captured.err


@pytest.mark.unit
def test_staleness_no_warning_when_no_commits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When git log returns empty output, no warning is printed."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    src = repo_root / "src" / "fieldkit"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("# foo", encoding="utf-8")

    doc_path = tmp_path / "test.md"
    doc_path.write_text("# test", encoding="utf-8")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        _mod._check_staleness(doc_path, "2026-06-21", "src/fieldkit/foo.py", repo_root)

    captured = capsys.readouterr()
    assert captured.err == ""


@pytest.mark.unit
def test_nonexistent_path_warns_and_skips_git(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Non-existent covers path emits a warning and does not call git."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    doc_path = tmp_path / "test.md"
    doc_path.write_text("# test", encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        _mod._check_staleness(doc_path, "2026-06-21", "src/fieldkit/missing.py", repo_root)
        mock_run.assert_not_called()

    captured = capsys.readouterr()
    assert "not exist" in captured.err or "WARNING" in captured.err


@pytest.mark.unit
def test_invalid_path_warns_and_skips_git(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Path escaping repo root emits a warning and does not call git."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    doc_path = tmp_path / "test.md"
    doc_path.write_text("# test", encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        _mod._check_staleness(doc_path, "2026-06-21", "../../.git/config", repo_root)
        mock_run.assert_not_called()

    captured = capsys.readouterr()
    assert "not repo-relative" in captured.err or "WARNING" in captured.err


# ---------------------------------------------------------------------------
# Git unavailability test
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_git_log_nonzero_exit_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When git log exits non-zero, a warning is printed but no staleness warning follows."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    src = repo_root / "src" / "fieldkit"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("# foo", encoding="utf-8")

    doc_path = tmp_path / "test.md"
    doc_path.write_text("# test", encoding="utf-8")

    mock_result = MagicMock()
    mock_result.returncode = 128
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        _mod._check_staleness(doc_path, "2026-06-21", "src/fieldkit/foo.py", repo_root)

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "stale" not in captured.err.lower()


@pytest.mark.unit
def test_git_unavailable_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """FileNotFoundError from git subprocess causes exit 0 with warning."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    src = repo_root / "src"
    src.mkdir()
    (src / "foo.py").write_text("# foo", encoding="utf-8")

    doc_path = tmp_path / "test.md"
    doc_path.write_text("# test", encoding="utf-8")

    with (
        patch("subprocess.run", side_effect=FileNotFoundError("git not found")),
        pytest.raises(SystemExit) as exc_info,
    ):
        _mod._check_staleness(doc_path, "2026-06-21", "src/foo.py", repo_root)

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err or "git" in captured.err.lower()


# ---------------------------------------------------------------------------
# Exit-0 guarantee tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_exits_zero_with_no_docs_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """main() exits 0 even when docs/ directory does not exist."""
    with (
        patch.object(_mod, "_repo_root", return_value=tmp_path),
        pytest.raises(SystemExit) as exc_info,
    ):
        _mod.main()
    assert exc_info.value.code == 0


@pytest.mark.unit
def test_main_returns_normally_with_valid_docs(tmp_path: Path) -> None:
    """main() returns normally when docs/ exists with valid frontmatter and no staleness."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    src_dir = tmp_path / "src" / "fieldkit"
    src_dir.mkdir(parents=True)
    (src_dir / "foo.py").write_text("# foo", encoding="utf-8")

    (docs_dir / "test.md").write_text(
        "---\nlast_reviewed: 2026-06-21\ncovers:\n  - src/fieldkit/foo.py\n---\n# Doc\n",
        encoding="utf-8",
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with (
        patch.object(_mod, "_repo_root", return_value=tmp_path),
        patch("subprocess.run", return_value=mock_result),
    ):
        # main() should return normally (exit 0 implicitly via return).
        _mod.main()
