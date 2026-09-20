"""Unit tests for the skill-install selection helpers.

Covers `fieldkit.skill.install` directly. The interactive flow, the writes and
the output live in commands/skill/_runner.py and are covered by
tests/test_skill_install*.py.
"""

import pytest

from fieldkit.skill.install import detect_tools, discover_skill_names, expand_all_skills
from fieldkit.skill.targets import SKILL_CATEGORIES, TOOL_TARGETS

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# expand_all_skills
# ---------------------------------------------------------------------------


def test_expand_all_skills_returns_every_categorised_skill():
    """--all must reach every skill in every category, not just the first."""
    result = expand_all_skills()

    expected = [name for category_skills in SKILL_CATEGORIES.values() for name in category_skills]
    assert result == expected
    assert len(result) == sum(len(names) for names in SKILL_CATEGORIES.values())


def test_expand_all_skills_is_deterministic():
    """Repeat calls give the same order, so the install summary is stable."""
    result = expand_all_skills()

    assert result == expand_all_skills()


# ---------------------------------------------------------------------------
# detect_tools
# ---------------------------------------------------------------------------


def test_detect_tools_finds_a_present_marker_directory(tmp_path):
    """A checkout carrying .claude is detected as claude-code."""
    (tmp_path / ".claude").mkdir()

    result = detect_tools(tmp_path)

    assert result == ["claude-code"]


def test_detect_tools_finds_several_markers(tmp_path):
    """Every present marker is reported, in TOOL_TARGETS order."""
    (tmp_path / ".opencode").mkdir()
    (tmp_path / ".cursor").mkdir()

    result = detect_tools(tmp_path)

    assert result == ["opencode", "cursor"]


def test_detect_tools_returns_empty_for_a_bare_directory(tmp_path):
    """No markers → nothing detected, and no exception."""
    result = detect_tools(tmp_path)

    assert result == []


def test_detect_tools_ignores_a_marker_that_is_a_file(tmp_path):
    """The marker must be a directory — a file of the same name is not a tool."""
    (tmp_path / ".claude").write_text("not a directory", encoding="utf-8")

    result = detect_tools(tmp_path)

    assert result == []


def test_detect_tools_only_reports_known_tools(tmp_path):
    """An unrelated dotted directory is not mistaken for a tool."""
    (tmp_path / ".github").mkdir()

    result = detect_tools(tmp_path)

    assert result == []
    assert set(TOOL_TARGETS) >= set(result)


# ---------------------------------------------------------------------------
# discover_skill_names
# ---------------------------------------------------------------------------


def test_discover_skill_names_lists_skill_directories(tmp_path):
    """Each child directory is one installable skill name."""
    (tmp_path / "xlsx").mkdir()
    (tmp_path / "pdf").mkdir()

    result = discover_skill_names(tmp_path)

    assert result == {"xlsx", "pdf"}


def test_discover_skill_names_ignores_loose_files(tmp_path):
    """Files beside the skill directories are not skills."""
    (tmp_path / "xlsx").mkdir()
    (tmp_path / "README.md").write_text("notes", encoding="utf-8")

    result = discover_skill_names(tmp_path)

    assert result == {"xlsx"}


def test_discover_skill_names_returns_empty_set_when_absent(tmp_path):
    """A missing skills dir is a normal state, not an error."""
    result = discover_skill_names(tmp_path / "does-not-exist")

    assert result == set()


def test_discover_skill_names_returns_empty_set_for_a_file(tmp_path):
    """A path that is a file, not a directory, yields nothing rather than raising."""
    not_a_dir = tmp_path / "skills"
    not_a_dir.write_text("oops", encoding="utf-8")

    result = discover_skill_names(not_a_dir)

    assert result == set()
