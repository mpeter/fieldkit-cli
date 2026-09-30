"""Semantic contract for the public create-cli contributor skill."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SKILL_ROOT = Path(__file__).parents[1] / "src/fieldkit/skills/create-cli"


def test_create_cli_requires_the_authorized_source_candidate_contract() -> None:
    content = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    for required in (
        "authorized source checkout of the revision being changed",
        "A packaged skill installation alone is not that checkout",
        "An independently installed command may run a different revision",
        "report that prerequisite as pending rather than inventing the current interface",
        "top-level handler maps Click usage errors to data error status 3",
    ):
        assert required in one_line


def test_create_cli_stops_at_a_reviewable_spec_without_side_effects() -> None:
    content = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "Secrets do not belong in argv" in content
    assert "Stop after the interface contract unless implementation was also requested" in one_line
    assert "Do not edit code, stage changes, create issues, publish a package, or alter repository settings" in one_line
    assert "A proposal is not an implemented feature or a passing acceptance test" in one_line
    assert "```" not in content
    assert "|---" not in content


def test_create_cli_upstream_license_is_packaged() -> None:
    content = (_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "[upstream license](references/UPSTREAM-LICENSE.txt)" in content
    assert (_SKILL_ROOT / "references/UPSTREAM-LICENSE.txt").is_file()
