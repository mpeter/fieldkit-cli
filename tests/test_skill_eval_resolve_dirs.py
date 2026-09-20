"""Tests for _resolve_skill_dirs() — covering uncovered branches.

cc=9, cov=55%, target: --all, named-skill, not-found, no-args paths.
"""

from pathlib import Path

import pytest

from fieldkit.commands.skill.eval_runner import _resolve_skill_dirs

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skills_dir(tmp_path: Path, names: list[str]) -> Path:
    """Create a skills directory with subdirectory entries for each name."""
    skills = tmp_path / "skills"
    skills.mkdir()
    for name in names:
        (skills / name).mkdir()
    return skills


# ---------------------------------------------------------------------------
# run_all=True branch
# ---------------------------------------------------------------------------


# ── TestResolveSkillDirsRunAll (flattened) ──────────────────────────────────


def test_resolve_skill_dirs_run_all_returns_all_dirs_sorted(tmp_path: Path) -> None:
    """run_all=True returns all non-underscore subdirectories, sorted."""
    skills_dir = _make_skills_dir(tmp_path, ["bravo", "alpha", "charlie"])

    result = _resolve_skill_dirs(skills_dir, run_all=True, skill_names=[])

    assert result is not None
    names = [d.name for d in result]
    assert names == sorted(names), "Directories must be returned in sorted order"
    assert set(names) == {"alpha", "bravo", "charlie"}


def test_resolve_skill_dirs_run_all_excludes_underscore_prefixed_dirs(tmp_path: Path) -> None:
    """Directories starting with '_' are excluded from run_all results."""
    skills_dir = _make_skills_dir(tmp_path, ["good-skill", "_internal", "__pycache__"])

    result = _resolve_skill_dirs(skills_dir, run_all=True, skill_names=[])

    assert result is not None
    names = [d.name for d in result]
    assert "good-skill" in names
    assert "_internal" not in names
    assert "__pycache__" not in names


def test_resolve_skill_dirs_run_all_empty_skills_dir_returns_empty_list(tmp_path: Path) -> None:
    """run_all=True on an empty skills directory returns an empty list."""
    skills_dir = _make_skills_dir(tmp_path, [])

    result = _resolve_skill_dirs(skills_dir, run_all=True, skill_names=[])

    assert result == []


def test_resolve_skill_dirs_run_all_files_are_not_included(tmp_path: Path) -> None:
    """Files (not directories) are excluded from run_all results."""
    skills_dir = _make_skills_dir(tmp_path, ["real-skill"])
    (skills_dir / "README.md").write_text("readme", encoding="utf-8")

    result = _resolve_skill_dirs(skills_dir, run_all=True, skill_names=[])

    assert result is not None
    names = [d.name for d in result]
    assert "README.md" not in names
    assert "real-skill" in names


# ---------------------------------------------------------------------------
# Named skill lookup
# ---------------------------------------------------------------------------


# ── TestResolveSkillDirsNamed (flattened) ───────────────────────────────────


def test_resolve_skill_dirs_named_returns_dir_for_known_skill(tmp_path: Path) -> None:
    """Returns the directory for a named skill that exists."""
    skills_dir = _make_skills_dir(tmp_path, ["my-skill", "other-skill"])

    result = _resolve_skill_dirs(skills_dir, run_all=False, skill_names=["my-skill"])

    assert result is not None
    assert len(result) == 1
    assert result[0].name == "my-skill"


def test_resolve_skill_dirs_named_returns_multiple_named_skills(tmp_path: Path) -> None:
    """Returns directories for each named skill in the list."""
    skills_dir = _make_skills_dir(tmp_path, ["skill-a", "skill-b", "skill-c"])

    result = _resolve_skill_dirs(skills_dir, run_all=False, skill_names=["skill-a", "skill-c"])

    assert result is not None
    names = [d.name for d in result]
    assert "skill-a" in names
    assert "skill-c" in names
    assert "skill-b" not in names


def test_resolve_skill_dirs_named_returns_none_when_skill_not_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Returns None and prints to stderr when a named skill does not exist."""
    skills_dir = _make_skills_dir(tmp_path, ["existing-skill"])

    result = _resolve_skill_dirs(skills_dir, run_all=False, skill_names=["nonexistent-skill"])

    assert result is None
    captured = capsys.readouterr()
    assert "nonexistent-skill" in captured.err
    assert "not found" in captured.err.lower()


def test_resolve_skill_dirs_named_returns_none_on_first_missing_skill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Returns None immediately when any named skill is missing."""
    skills_dir = _make_skills_dir(tmp_path, ["good-skill"])

    result = _resolve_skill_dirs(skills_dir, run_all=False, skill_names=["good-skill", "bad-skill"])

    assert result is None


# ---------------------------------------------------------------------------
# No args → prints usage, returns None
# ---------------------------------------------------------------------------


# ── TestResolveSkillDirsNoArgs (flattened) ──────────────────────────────────


def test_resolve_skill_dirs_no_args_no_args_returns_none_and_prints_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Returns None and prints usage when neither run_all nor skill_names given."""
    skills_dir = _make_skills_dir(tmp_path, ["some-skill"])

    result = _resolve_skill_dirs(skills_dir, run_all=False, skill_names=[])

    assert result is None
    captured = capsys.readouterr()
    assert "Usage" in captured.err or "fieldkit skill eval" in captured.err
