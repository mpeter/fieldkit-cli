"""Tests for the fail-closed semantic Python change classifier."""

import subprocess
from pathlib import Path

import pytest

from scripts import semantic_python_changes

pytestmark = pytest.mark.unit


def test_git_queries_ignore_ambient_repository_redirection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repositories = []
    for name, content in (("real", "real\n"), ("decoy", "decoy\n")):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=5)
        (repo / "example.py").write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "example.py"], cwd=repo, check=True, timeout=5)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test Contributor",
                "-c",
                "user.email=contributor@example.com",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=repo,
            check=True,
            timeout=5,
        )
        repositories.append(repo)
    real, decoy = repositories
    expected = semantic_python_changes._run_git(real, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    observed = semantic_python_changes._run_git(real, "rev-parse", "HEAD")

    assert observed == expected


@pytest.mark.parametrize("change", ["staged", "mode", "symlink", "comment"])
def test_real_git_candidate_and_entry_changes(tmp_path: Path, change: str) -> None:
    """Only comments, not staged behavior or entry metadata, qualify for a skip."""
    assert semantic_python_changes._run_git(tmp_path, "init") is not None
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    assert semantic_python_changes._run_git(tmp_path, "add", "example.py") is not None
    assert (
        semantic_python_changes._run_git(
            tmp_path,
            "-c",
            "user.name=Test Contributor",
            "-c",
            "user.email=contributor@example.com",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "fixture",
        )
        is not None
    )
    if change == "staged":
        source.write_text("value = 2\n", encoding="utf-8")
        assert semantic_python_changes._run_git(tmp_path, "add", "example.py") is not None
        source.write_text("value = 1\n", encoding="utf-8")
    elif change == "mode":
        source.chmod(0o755)
    elif change == "symlink":
        source.unlink()
        source.symlink_to("target.py")
        (tmp_path / "target.py").write_text("value = 1\n", encoding="utf-8")
        (tmp_path / ".git" / "info" / "exclude").write_text("target.py\n", encoding="utf-8")
    else:
        source.write_text("# reader explanation\nvalue = 1\n", encoding="utf-8")

    result = semantic_python_changes.has_test_relevant_changes("HEAD", tmp_path)

    assert result is (change != "comment")
    if change != "staged":
        assert semantic_python_changes._run_git(tmp_path, "add", "example.py") is not None
    candidate_tree = semantic_python_changes._run_git(tmp_path, "write-tree")
    assert candidate_tree is not None
    hosted_result = semantic_python_changes.has_non_prose_changes("HEAD", candidate_tree.strip(), tmp_path)
    assert hosted_result is True


def test_untracked_prose_symlink_requires_tests(tmp_path: Path) -> None:
    """The prose allowlist cannot hide new symlinks."""
    assert semantic_python_changes._run_git(tmp_path, "init") is not None
    base_tree = semantic_python_changes._run_git(tmp_path, "write-tree")
    assert base_tree is not None
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").symlink_to("missing.md")

    result = semantic_python_changes.has_test_relevant_changes(base_tree.strip(), tmp_path)

    assert result is True


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/fieldkit/data.sql", True),
        ("src/fieldkit/skills/example/SKILL.md", True),
        ("AGENTS.md", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        ("docs/release-readiness/policy.json", True),
        ("openspec/specs/example/spec.md", True),
        ("new.py", True),
        ("odd\nname.py", True),
        ("docs/guide.md", False),
    ],
)
@pytest.mark.parametrize("tracked", [True, False])
@pytest.mark.parametrize("executable", [True, False])
def test_real_git_inventory(tmp_path: Path, path: str, expected: bool, tracked: bool, executable: bool) -> None:
    """Real Git inventories retain package data, untracked sources and unusual names."""
    assert semantic_python_changes._run_git(tmp_path, "init") is not None
    source = tmp_path / path
    source.parent.mkdir(parents=True, exist_ok=True)
    if tracked:
        source.write_text("value = 1\n", encoding="utf-8")
        assert semantic_python_changes._run_git(tmp_path, "add", "--", path) is not None
    assert (
        semantic_python_changes._run_git(
            tmp_path,
            "-c",
            "user.name=Test Contributor",
            "-c",
            "user.email=contributor@example.com",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "test fixture",
        )
        is not None
    )
    source.write_text("value = 2\n", encoding="utf-8")
    if executable:
        source.chmod(0o755)
        expected = True

    result = semantic_python_changes.has_test_relevant_changes("HEAD", tmp_path)

    assert result is expected
    expected_full = executable or path != "docs/guide.md"
    full_result = semantic_python_changes.requires_full_test_suite("HEAD", tmp_path, candidate_revision=None)
    assert full_result is expected_full

    assert semantic_python_changes._run_git(tmp_path, "add", "--", path) is not None
    candidate_tree = semantic_python_changes._run_git(tmp_path, "write-tree")
    assert candidate_tree is not None
    hosted_result = semantic_python_changes.has_non_prose_changes("HEAD", candidate_tree.strip(), tmp_path)
    assert hosted_result is expected
    hosted_full = semantic_python_changes.requires_full_test_suite(
        "HEAD", tmp_path, candidate_revision=candidate_tree.strip()
    )
    assert hosted_full is expected_full


@pytest.mark.parametrize("output", [None, "malformed\0docs/guide.md\0"])
def test_hosted_inventory_failure_requires_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: str | None
) -> None:
    """Unavailable or malformed Git metadata cannot produce a docs-only CI skip."""
    monkeypatch.setattr(semantic_python_changes, "_run_git", lambda *_: output)
    result = semantic_python_changes.has_non_prose_changes("base", "candidate", tmp_path)
    assert result is True


@pytest.mark.parametrize(
    "path",
    [
        "src/fieldkit/gmail/schema.sql",
        "src/fieldkit/_data/settings.json",
        "src/fieldkit/skills/brief/SKILL.md",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
    ],
)
def test_non_python_runtime_changes_require_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str) -> None:
    """Executable package data and policy changes are not documentation-only."""
    monkeypatch.setattr(semantic_python_changes, "_run_git", lambda *_: path + "\0")
    result = semantic_python_changes.has_test_relevant_changes("base", tmp_path)
    assert result is True


def test_untracked_python_requires_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A newly created source file cannot disappear from the local gate inventory."""

    def git_output(_root: Path, *arguments: str) -> str:
        return "new.py\0" if arguments[0] == "ls-files" else ""

    monkeypatch.setattr(semantic_python_changes, "_run_git", git_output)
    result = semantic_python_changes.has_test_relevant_changes("base", tmp_path)
    assert result is True


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


def test_changed_paths_fail_closed_when_git_cannot_read_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing base revision cannot waive impact testing."""
    monkeypatch.setattr(semantic_python_changes, "_changed_paths", lambda *_: ["src/example.py"])
    monkeypatch.setattr(semantic_python_changes, "_git_source", lambda *_: None)

    assert semantic_python_changes.has_test_relevant_changes("base", tmp_path) is True


def test_changed_paths_include_deleted_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Deleting Python source remains behavior-affecting until impact tests pass."""
    captured: list[str] = []

    def _run_git(_: Path, *arguments: str) -> str:
        captured.extend(arguments)
        return "src/fieldkit/removed.py\0README.md\0"

    monkeypatch.setattr(semantic_python_changes, "_run_git", _run_git)

    assert semantic_python_changes._changed_paths("base", tmp_path) == ["README.md", "src/fieldkit/removed.py"]
    assert "--no-renames" in captured


def test_working_tree_source_rejects_path_escape(tmp_path: Path) -> None:
    """Git path anomalies cannot make a semantic change appear docs-only."""
    assert semantic_python_changes._working_tree_source("../outside.py", tmp_path) is None


def test_uncommitted_semantic_change_requires_impact_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The local gate compares its base against the worktree, not only HEAD."""
    source_path = tmp_path / "src" / "example.py"
    source_path.parent.mkdir()
    source_path.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.setattr(semantic_python_changes, "_changed_paths", lambda *_: ["src/example.py"])
    monkeypatch.setattr(semantic_python_changes, "_git_source", lambda *_: "value = 1\n")

    assert semantic_python_changes.has_test_relevant_changes("base", tmp_path) is True
