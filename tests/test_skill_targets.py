"""Tests for fieldkit.skill.targets — TOOL_TARGETS and SKILL_CATEGORIES constants."""

from pathlib import Path

import pytest

from fieldkit.skill.targets import SKILL_CATEGORIES, TOOL_TARGETS

# Repo root: tests/ is one level below the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_DIR = _REPO_ROOT / "src" / "fieldkit" / "skills"


# ---------------------------------------------------------------------------
# SKILL_CATEGORIES corpus tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_skills_categorised() -> None:
    """Every skill directory with a SKILL.md appears exactly once in SKILL_CATEGORIES."""
    # Collect all skill names present in the real corpus.
    corpus: set[str] = {d.name for d in _SKILLS_DIR.iterdir() if d.is_dir() and (d / "SKILL.md").exists()}

    # Flatten all values from SKILL_CATEGORIES into a single list.
    categorised: list[str] = [skill for skills in SKILL_CATEGORIES.values() for skill in skills]

    missing = corpus - set(categorised)
    extra = set(categorised) - corpus

    assert not missing, f"Skills in corpus but missing from SKILL_CATEGORIES: {sorted(missing)}"
    assert not extra, f"Skills in SKILL_CATEGORIES but absent from corpus: {sorted(extra)}"

    # Each corpus skill must appear exactly once (no duplicates across categories).
    for skill in corpus:
        count = categorised.count(skill)
        assert count == 1, f"Skill {skill!r} appears {count} times in SKILL_CATEGORIES (expected 1)"


@pytest.mark.unit
def test_skill_categories_no_duplicates() -> None:
    """No skill name appears in more than one category."""
    categorised: list[str] = [skill for skills in SKILL_CATEGORIES.values() for skill in skills]

    seen: set[str] = set()
    duplicates: list[str] = []
    for skill in categorised:
        if skill in seen:
            duplicates.append(skill)
        seen.add(skill)

    assert not duplicates, f"Duplicate skill names across categories: {sorted(set(duplicates))}"


# ---------------------------------------------------------------------------
# TOOL_TARGETS key tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_targets_keys() -> None:
    """All four expected tool keys are present in TOOL_TARGETS."""
    expected = {"opencode", "claude-code", "cursor", "gemini"}
    assert set(TOOL_TARGETS.keys()) == expected


# ---------------------------------------------------------------------------
# ToolTarget field invariant tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_target_format_values() -> None:
    """Every ToolTarget.format is either 'directory' or 'flat'."""
    valid_formats = {"directory", "flat"}
    for key, target in TOOL_TARGETS.items():
        assert target.format in valid_formats, (
            f"TOOL_TARGETS[{key!r}].format = {target.format!r} is not in {valid_formats}"
        )


@pytest.mark.unit
def test_cursor_is_flat() -> None:
    """The 'cursor' tool target uses flat format."""
    assert TOOL_TARGETS["cursor"].format == "flat"


@pytest.mark.unit
def test_tool_target_skill_path_has_name_placeholder() -> None:
    """Every ToolTarget.skill_path contains the '{name}' placeholder."""
    for key, target in TOOL_TARGETS.items():
        assert "{name}" in target.skill_path, (
            f"TOOL_TARGETS[{key!r}].skill_path = {target.skill_path!r} missing '{{name}}' placeholder"
        )


@pytest.mark.unit
def test_tool_target_detect_is_dotdir() -> None:
    """Every ToolTarget.detect starts with '.' (is a hidden dot-directory)."""
    for key, target in TOOL_TARGETS.items():
        assert target.detect.startswith("."), (
            f"TOOL_TARGETS[{key!r}].detect = {target.detect!r} does not start with '.'"
        )
