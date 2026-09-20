"""Tests for fieldkit.skill.graph -- skill reference graph utilities."""

from pathlib import Path

import pytest

from fieldkit.skill.graph import build_related_graph, extract_related_skill_names

# ---------------------------------------------------------------------------
# extract_related_skill_names
# ---------------------------------------------------------------------------

_BOLD_SKILL_MD = """\
# My Skill

Some intro text.

## Related Skills

- **account-pulse** — Get a quick health check across your active accounts.
- **meeting-prep** — Build a pre-meeting brief.

## Other Section

Unrelated content.
"""

_BACKTICK_SKILL_MD = """\
# My Skill

## Related Skills

- `/meeting-prep` — Build a pre-meeting brief.
- `/account-pulse` — Get a quick health check.
"""

_NO_SECTION_SKILL_MD = """\
# My Skill

Some content with no Related Skills section at all.

## Other Section

More content.
"""

_MIXED_SKILL_MD = """\
# My Skill

## Related Skills

- **account-pulse** — Bold format entry.
- `/meeting-prep` — Backtick-slash format entry.
- **forecast** — Another bold entry.
"""

_EMPTY_SECTION_SKILL_MD = """\
# My Skill

## Related Skills

See the skills directory for more information. No list entries here.

## Next Section

Done.
"""


@pytest.mark.unit
def test_extract_related_skill_names_bold_format() -> None:
    """Bold format ``- **skill-name** -- desc`` yields the skill name."""
    # Only the first bold entry is present; meeting-prep is also bold so both
    # appear, but this fixture only tests the bold path is recognised at all.
    result = extract_related_skill_names(_BOLD_SKILL_MD)
    assert "account-pulse" in result


@pytest.mark.unit
def test_extract_related_skill_names_backtick_format() -> None:
    """Backtick-slash format ``- `/skill-name` -- desc`` yields the skill name."""
    result = extract_related_skill_names(_BACKTICK_SKILL_MD)
    assert result == ["meeting-prep", "account-pulse"]


@pytest.mark.unit
def test_extract_related_skill_names_no_section() -> None:
    """Text with no ## Related Skills section returns an empty list."""
    result = extract_related_skill_names(_NO_SECTION_SKILL_MD)
    assert result == []


@pytest.mark.unit
def test_extract_related_skill_names_mixed_formats() -> None:
    """Section with both bold and backtick entries returns all names in order."""
    result = extract_related_skill_names(_MIXED_SKILL_MD)
    assert result == ["account-pulse", "meeting-prep", "forecast"]


@pytest.mark.unit
def test_extract_related_skill_names_empty_section() -> None:
    """Section exists but contains only prose — no list entries — returns []."""
    result = extract_related_skill_names(_EMPTY_SECTION_SKILL_MD)
    assert result == []


# ---------------------------------------------------------------------------
# build_related_graph
# ---------------------------------------------------------------------------


def _make_skill(skills_dir: Path, name: str, related: list[str]) -> None:
    """Write a minimal SKILL.md for *name* with a Related Skills section."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# {name}\n", "\n", "## Related Skills\n", "\n"]
    for ref in related:
        lines.append(f"- **{ref}** — description\n")
    (skill_dir / "SKILL.md").write_text("".join(lines), encoding="utf-8")


@pytest.mark.unit
def test_build_related_graph_basic(tmp_path: Path) -> None:
    """Two skills each referencing the other produce a symmetric adjacency."""
    _make_skill(tmp_path, "skill-a", ["skill-b"])
    _make_skill(tmp_path, "skill-b", ["skill-a"])

    graph = build_related_graph(tmp_path)

    assert set(graph.keys()) == {"skill-a", "skill-b"}
    assert graph["skill-a"] == {"skill-b"}
    assert graph["skill-b"] == {"skill-a"}


@pytest.mark.unit
def test_build_related_graph_empty_dir(tmp_path: Path) -> None:
    """An existing but empty directory returns an empty adjacency dict."""
    graph = build_related_graph(tmp_path)
    assert graph == {}


@pytest.mark.unit
def test_build_related_graph_missing_dir(tmp_path: Path) -> None:
    """A non-existent path returns an empty adjacency dict (no exception)."""
    missing = tmp_path / "does-not-exist"
    graph = build_related_graph(missing)
    assert graph == {}


@pytest.mark.unit
def test_build_related_graph_drops_dangling_refs(tmp_path: Path) -> None:
    """References to skills not in the corpus are silently dropped."""
    _make_skill(tmp_path, "skill-a", ["ghost-skill"])

    graph = build_related_graph(tmp_path)

    # skill-a is in the corpus; ghost-skill is not
    assert "skill-a" in graph
    assert "ghost-skill" not in graph
    # The dangling ref is dropped from skill-a's adjacency set
    assert graph["skill-a"] == set()
