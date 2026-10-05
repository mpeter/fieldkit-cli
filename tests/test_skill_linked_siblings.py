"""Tests for reporting sibling skills that an install leaves unresolved."""

from pathlib import Path

import pytest

from fieldkit.skill.install import linked_sibling_skills, missing_linked_skills
from fieldkit.skill.targets import InstallTarget

pytestmark = pytest.mark.unit


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    for name in ("start", "tool-routing", "solo"):
        (root / name).mkdir(parents=True)
    (root / "start" / "SKILL.md").write_text(
        "See [routing](../tool-routing/SKILL.md), [own](references/x.md) and [gone](../absent/SKILL.md).\n",
        encoding="utf-8",
    )
    (root / "start" / "references").mkdir()
    (root / "start" / "references" / "x.md").write_text("[back](../SKILL.md)\n", encoding="utf-8")
    (root / "tool-routing" / "SKILL.md").write_text("# Routing\n", encoding="utf-8")
    (root / "solo" / "SKILL.md").write_text("# Solo\n", encoding="utf-8")
    return root


def _target(tmp_path: Path, fmt: str) -> InstallTarget:
    root = tmp_path / "dest"
    root.mkdir()
    path = "{name}.md" if fmt == "flat" else "{name}"
    return InstallTarget("t", "Tool", root, root / ".manifest.json", path, fmt)  # type: ignore[arg-type]


def test_linked_sibling_skills_ignores_own_files_and_missing_skills(tmp_path: Path) -> None:
    root = _skills(tmp_path)

    assert linked_sibling_skills(root / "start") == {"tool-routing"}
    assert linked_sibling_skills(root / "solo") == set()


def test_missing_linked_skills_reports_sibling_left_out_of_selection(tmp_path: Path) -> None:
    root = _skills(tmp_path)

    assert missing_linked_skills(_target(tmp_path, "flat"), ["start"], root) == {"start": {"tool-routing"}}


def test_missing_linked_skills_accepts_sibling_selected_or_already_installed(tmp_path: Path) -> None:
    root = _skills(tmp_path)
    target = _target(tmp_path, "directory")

    assert missing_linked_skills(target, ["start", "tool-routing"], root) == {}
    (target.skill_root / "tool-routing").mkdir()
    (target.skill_root / "tool-routing" / "SKILL.md").write_text("# Routing\n", encoding="utf-8")
    assert missing_linked_skills(target, ["start"], root) == {}
