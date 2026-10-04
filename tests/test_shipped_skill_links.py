"""Shipped skills must keep working links after installation.

`fieldkit skill install` copies each skill directory into a workspace beside the
other skills and nothing else, so a link that leaves the skills tree points at a
file the install never carries.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "src" / "fieldkit" / "skills"
_LINK_PATTERN = re.compile(r"\]\(([^)#\s]+)")


def _links_outside_skills_tree(skills_root: Path) -> list[str]:
    escaped = []
    for path in sorted(skills_root.rglob("*.md")):
        for link in _LINK_PATTERN.findall(path.read_text(encoding="utf-8")):
            if "://" in link or link.startswith("mailto:"):
                continue
            if not (path.parent / link).resolve().is_relative_to(skills_root.resolve()):
                escaped.append(f"{path.relative_to(skills_root)} -> {link}")
    return escaped


def test_shipped_skill_links_stay_inside_the_skills_tree() -> None:
    assert _links_outside_skills_tree(_SKILLS_ROOT) == []


def test_link_outside_the_skills_tree_is_reported(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "See [sibling](../other/SKILL.md), [web](https://example.com), and [guide](../../../AGENTS.md).\n",
        encoding="utf-8",
    )
    assert _links_outside_skills_tree(tmp_path / "skills") == ["demo/SKILL.md -> ../../../AGENTS.md"]
