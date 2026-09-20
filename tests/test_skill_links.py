"""Unit tests for hooks/skill_links.py — symlink surface checker/linker."""

import os
from pathlib import Path

import pytest

from hooks import skill_links


def _relative_symlink(link: Path, target: Path) -> None:
    """Create a relative symlink at `link` pointing at `target`."""
    rel = os.path.relpath(target, start=link.parent)
    link.symlink_to(Path(rel), target_is_directory=target.is_dir())


def _scaffold(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build empty global_skills/agents_skills/repo dirs under tmp_path."""
    global_skills = tmp_path / "claude-skills"
    agents_skills = tmp_path / "agents-skills"
    repo = tmp_path / "repo"
    global_skills.mkdir()
    agents_skills.mkdir()
    (repo / ".opencode" / "skills").mkdir(parents=True)
    return global_skills, agents_skills, repo


@pytest.mark.unit
def test_check_clean_setup_passes(tmp_path: Path) -> None:
    """A correctly-wired relative link into an existing physical skill is clean."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / "skillA").mkdir()
    _relative_symlink(global_skills / "skillA", agents_skills / "skillA")

    violations = skill_links.check(global_skills, agents_skills, repo)

    assert violations == []


@pytest.mark.unit
def test_check_ignores_enclosing_checkout(tmp_path: Path) -> None:
    """A test sandbox inside a checkout does not make its local links leaks."""
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / "skillA").mkdir()
    _relative_symlink(global_skills / "skillA", agents_skills / "skillA")

    assert skill_links.check(global_skills, agents_skills, repo) == []


@pytest.mark.unit
def test_check_detects_dangling_link(tmp_path: Path) -> None:
    """A link whose target does not exist is flagged as dangling."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (global_skills / "ghost").symlink_to(Path("../../agents-skills/ghost"))

    violations = skill_links.check(global_skills, agents_skills, repo)

    kinds = [v.kind for v in violations]
    assert kinds == ["dangling"]


@pytest.mark.unit
def test_check_detects_absolute_link(tmp_path: Path) -> None:
    """A link using an absolute target path is flagged, even if it resolves fine."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / "skillB").mkdir()
    (global_skills / "skillB").symlink_to(agents_skills / "skillB", target_is_directory=True)

    violations = skill_links.check(global_skills, agents_skills, repo)

    kinds = [v.kind for v in violations]
    assert "absolute" in kinds


@pytest.mark.unit
def test_check_detects_leak_link_into_git_repo(tmp_path: Path) -> None:
    """A link resolving into a directory with a .git ancestor is a leak link."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    leaky_repo = tmp_path / "some-project"
    (leaky_repo / ".git").mkdir(parents=True)
    (leaky_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (leaky_repo / ".git" / "objects").mkdir()
    (leaky_repo / "skills" / "leaky").mkdir(parents=True)
    _relative_symlink(global_skills / "leaky", leaky_repo / "skills" / "leaky")

    violations = skill_links.check(global_skills, agents_skills, repo)

    kinds = [v.kind for v in violations]
    assert "leak" in kinds


@pytest.mark.unit
def test_check_ignores_empty_gitdir_marker(tmp_path: Path) -> None:
    """An incomplete linked-worktree marker is not a Git repository."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    incomplete_worktree = tmp_path / "incomplete-worktree"
    incomplete_worktree.mkdir()
    (incomplete_worktree / ".git").write_text("gitdir: \n", encoding="utf-8")
    (incomplete_worktree / "skills" / "safe").mkdir(parents=True)
    _relative_symlink(global_skills / "safe", incomplete_worktree / "skills" / "safe")

    assert skill_links.check(global_skills, agents_skills, repo) == []


@pytest.mark.unit
def test_check_ignores_non_gitdir_marker(tmp_path: Path) -> None:
    """An arbitrary marker file is not a linked-worktree pointer."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    not_a_worktree = tmp_path / "not-a-worktree"
    not_a_worktree.mkdir()
    (not_a_worktree / ".git").write_text("metadata\n", encoding="utf-8")
    (not_a_worktree / "metadata").mkdir()
    (not_a_worktree / "metadata" / "HEAD").write_text("not Git\n", encoding="utf-8")
    (not_a_worktree / "skills" / "safe").mkdir(parents=True)
    _relative_symlink(global_skills / "safe", not_a_worktree / "skills" / "safe")

    assert skill_links.check(global_skills, agents_skills, repo) == []


@pytest.mark.unit
def test_check_detects_leak_link_into_linked_worktree(tmp_path: Path) -> None:
    """A valid linked-worktree marker is a Git repository leak."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    gitdir = tmp_path / "git-admin" / "worktrees" / "branch"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/branch\n", encoding="utf-8")
    linked_worktree = tmp_path / "linked-worktree"
    linked_worktree.mkdir()
    (linked_worktree / ".git").write_text("gitdir: ../git-admin/worktrees/branch\n", encoding="utf-8")
    (linked_worktree / "skills" / "leaky").mkdir(parents=True)
    _relative_symlink(global_skills / "leaky", linked_worktree / "skills" / "leaky")

    assert [violation.kind for violation in skill_links.check(global_skills, agents_skills, repo)] == ["leak"]


@pytest.mark.unit
def test_check_detects_name_collision(tmp_path: Path) -> None:
    """The same skill name existing in both global-physical and repo scopes collides."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / "dup").mkdir()
    (repo / ".opencode" / "skills" / "dup").mkdir()

    violations = skill_links.check(global_skills, agents_skills, repo)

    kinds = [v.kind for v in violations]
    assert kinds == ["collision"]


@pytest.mark.unit
def test_apply_creates_relative_links(tmp_path: Path) -> None:
    """apply(create_missing=True) creates a relative symlink for each un-linked physical skill."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / "skillC").mkdir()

    result = skill_links.apply(global_skills, agents_skills, repo, create_missing=True)

    assert result.violations == []
    created_link = global_skills / "skillC"
    assert created_link.is_symlink()
    assert not created_link.readlink().is_absolute()
    assert created_link.resolve() == (agents_skills / "skillC").resolve()


@pytest.mark.unit
def test_apply_default_never_creates_links(tmp_path: Path) -> None:
    """Default apply() plans no creates — an unlinked skill is curation, not drift."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / "skillC").mkdir()

    result = skill_links.apply(global_skills, agents_skills, repo)

    assert result.actions == []
    assert not (global_skills / "skillC").exists()


@pytest.mark.unit
def test_apply_create_missing_skips_dot_and_file_entries(tmp_path: Path) -> None:
    """create_missing links real skill dirs only — never dot-prefixed archives or plain files."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / ".archived-corpus").mkdir()
    (agents_skills / "README.md").write_text("not a skill\n", encoding="utf-8")

    result = skill_links.apply(global_skills, agents_skills, repo, create_missing=True)

    assert result.actions == []
    assert not (global_skills / ".archived-corpus").exists()
    assert not (global_skills / "README.md").exists()


@pytest.mark.unit
def test_apply_dry_run_makes_no_changes(tmp_path: Path) -> None:
    """--dry-run plans actions but never touches the filesystem."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / "skillD").mkdir()

    result = skill_links.apply(global_skills, agents_skills, repo, dry_run=True, create_missing=True)

    actions = result.actions
    assert len(actions) == 1
    assert not (global_skills / "skillD").exists()


@pytest.mark.unit
def test_apply_repairs_absolute_link_to_relative(tmp_path: Path) -> None:
    """apply() repairs an absolute link into its own physical skill to a relative one."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    (agents_skills / "skillE").mkdir()
    (global_skills / "skillE").symlink_to(agents_skills / "skillE", target_is_directory=True)

    result = skill_links.apply(global_skills, agents_skills, repo)

    assert result.violations == []
    repaired_link = global_skills / "skillE"
    assert repaired_link.is_symlink()
    assert not repaired_link.readlink().is_absolute()
    assert repaired_link.resolve() == (agents_skills / "skillE").resolve()


@pytest.mark.unit
def test_is_skill_dir_excludes_a_genuinely_cyclic_entry(tmp_path: Path) -> None:
    """A physical agents_skills entry that is itself a cyclic symlink can never
    be treated as a repairable skill: _is_skill_dir's is_dir() check silently
    returns False for a symlink it cannot resolve (Python does not raise on
    is_dir() for ELOOP), so the entry never enters physical_names and the
    repair branch in _plan_actions is never reached for it."""
    agents_skills = tmp_path / "agents-skills"
    agents_skills.mkdir()

    (agents_skills / "cyclic").symlink_to(agents_skills / "cyclic", target_is_directory=True)

    assert not skill_links._is_skill_dir(agents_skills / "cyclic")


@pytest.mark.unit
def test_check_and_apply_skip_whole_directory_symlink_scope(tmp_path: Path) -> None:
    """When global_skills IS agents_skills (whole-dir symlink), skip per-entry
    scanning entirely — third-party symlinks living inside agents_skills (e.g.
    from another tool) are not this tool's per-entry consumption links, and
    treating them as such would compute a self-referential repair (ELOOP)."""
    agents_skills = tmp_path / "agents-skills"
    agents_skills.mkdir()
    global_skills = tmp_path / "claude-skills"
    global_skills.symlink_to(agents_skills, target_is_directory=True)
    repo = tmp_path / "repo"
    (repo / ".opencode" / "skills").mkdir(parents=True)

    external_target = tmp_path / "external-skills" / "administering-linux"
    external_target.mkdir(parents=True)
    (agents_skills / "administering-linux").symlink_to(external_target, target_is_directory=True)

    violations = skill_links.check(global_skills, agents_skills, repo)
    assert violations == []

    result = skill_links.apply(global_skills, agents_skills, repo)
    assert result.actions == []
    assert result.violations == []
    # The external symlink must be untouched — no self-loop introduced.
    assert (agents_skills / "administering-linux").resolve() == external_target.resolve()


@pytest.mark.unit
def test_apply_never_touches_leak_links(tmp_path: Path) -> None:
    """apply() reports a leak link as a manual violation instead of pruning it."""
    global_skills, agents_skills, repo = _scaffold(tmp_path)
    leaky_repo = tmp_path / "another-project"
    (leaky_repo / ".git").mkdir(parents=True)
    (leaky_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (leaky_repo / ".git" / "objects").mkdir()
    (leaky_repo / "skills" / "leaky2").mkdir(parents=True)
    _relative_symlink(global_skills / "leaky2", leaky_repo / "skills" / "leaky2")

    result = skill_links.apply(global_skills, agents_skills, repo)

    violations = result.violations
    assert [v.kind for v in violations] == ["leak"]
    assert (global_skills / "leaky2").is_symlink()
