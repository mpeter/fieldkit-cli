"""Semantic and structural contracts for the public humanizer skill."""

from pathlib import Path

import pytest

from scripts.check_documentation_contract import _block_inventory

pytestmark = pytest.mark.unit

_SKILL = Path(__file__).parents[1] / "src/fieldkit/skills/humanizer/SKILL.md"


def test_humanizer_preserves_facts_uncertainty_and_disclosures() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    for required in (
        "Preserve factual uncertainty, source attributions, safety warnings",
        "never a required or requested authorship disclosure",
        "do not infer authorship",
        "not permission to invent a source",
        "do not call the draft verified",
    ):
        assert required in one_line
    assert "80%" not in content


def test_humanizer_requires_a_draft_and_approval_for_durable_edits() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "If no draft is supplied, ask for it rather than inventing findings" in one_line
    assert "does not send messages, save files, or modify remote drafts" in one_line
    assert "explicit approval, a diff, and read-back" in one_line


def test_humanizer_triage_block_is_structural_content_not_executable_proof() -> None:
    inventory = _block_inventory(_SKILL)

    assert inventory == [
        {
            "language": "",
            "sha256": "e642576428eaebd1af764ebd00ea26ed4089de515261ee529a352ddccae4a2b5",
        }
    ]
