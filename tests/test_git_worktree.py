"""Exercise candidate identity checks against real isolated Git repositories."""

import os
import subprocess
from pathlib import Path

import pytest

from scripts import git_worktree


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_PARAMETERS",
        "GIT_REPLACE_REF_BASE",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_NO_REPLACE_OBJECTS",
    ],
)
def test_git_environment_rejects_inherited_overrides(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "untrusted")
    monkeypatch.setenv("FIELDKIT_TEST_SENTINEL", "preserved")

    result = git_worktree.git_environment()

    assert {key: value for key, value in result.items() if key.startswith("GIT_")} == {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    assert result["FIELDKIT_TEST_SENTINEL"] == "preserved"


def _git(repo: Path, *args: str) -> str:
    """Run bounded Git commands without inheriting contributor hooks."""
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create a committed repository containing only fictional fixture data."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "test: initialize candidate")
    return tmp_path


@pytest.mark.integration
def test_clean_candidate_identity(repository: Path) -> None:
    """Cleanliness and revision agree with the actual committed tree."""
    git_worktree.require_clean_worktree(repository)
    revision = git_worktree.head_revision(repository)
    assert revision == _git(repository, "rev-parse", "HEAD")


@pytest.mark.integration
@pytest.mark.parametrize("state", ["staged", "unstaged", "untracked"])
def test_uncommitted_content_is_rejected(repository: Path, state: str) -> None:
    """No kind of uncommitted source can inherit a clean HEAD assessment."""
    path = repository / ("new.txt" if state == "untracked" else "tracked.txt")
    path.write_text("changed\n", encoding="utf-8")
    if state == "staged":
        _git(repository, "add", "tracked.txt")

    with pytest.raises(ValueError, match="clean worktree"):
        git_worktree.require_clean_worktree(repository)


@pytest.mark.integration
def test_redirected_worktree_cannot_hide_dirty_candidate(
    repository: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient Git routing must not substitute a clean tree for candidate bytes."""
    clean = tmp_path_factory.mktemp("clean-git-worktree")
    (clean / "tracked.txt").write_text("initial\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("unverified\n", encoding="utf-8")
    monkeypatch.setenv("GIT_WORK_TREE", str(clean))

    with pytest.raises(ValueError, match="clean worktree"):
        git_worktree.require_clean_worktree(repository)


@pytest.mark.integration
def test_local_config_cannot_hide_executable_mode_change(repository: Path) -> None:
    _git(repository, "config", "core.fileMode", "false")
    (repository / "tracked.txt").chmod(0o755)

    with pytest.raises(ValueError, match="clean worktree"):
        git_worktree.require_clean_worktree(repository)


@pytest.mark.integration
@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_flags_cannot_hide_content_change(repository: Path, flag: str) -> None:
    _git(repository, "update-index", flag, "tracked.txt")
    (repository / "tracked.txt").write_text("unverified\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"clean worktree|index flags"):
        git_worktree.require_clean_worktree(repository)


@pytest.mark.integration
@pytest.mark.parametrize("query", ["head", "index", "status"])
def test_identity_queries_fail_closed_on_output_overflow(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    if query == "status":
        (repository / ("untracked-" + "x" * 100)).write_bytes(b"untracked")
    maximum = 1 if query in {"head", "index"} else 80
    monkeypatch.setattr(git_worktree, "_GIT_OUTPUT_LIMIT_BYTES", maximum, raising=False)
    with pytest.raises(ValueError, match="resource bounds"):
        if query == "head":
            git_worktree.head_revision(repository)
        else:
            git_worktree.require_clean_worktree(repository)
