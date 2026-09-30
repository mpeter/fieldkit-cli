"""Semantic and structural contracts for the public win-loss skill."""

from pathlib import Path

import pytest

from scripts.check_documentation_contract import _block_inventory

pytestmark = pytest.mark.unit

_SKILL = Path(__file__).parents[1] / "src/fieldkit/skills/win-loss/SKILL.md"


def test_win_loss_uses_canonical_preview_approval_apply_and_readback() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "fieldkit pursuit advance PURSUIT --to closed-won --dry-run --json" in one_line
    assert "obtain separate approval before invoking the same command without `--dry-run`" in one_line
    assert "Verify the returned `advanced` result and reread the stage and appended history" in one_line
    assert "do not blindly retry a completed write" in one_line
    assert "There is no automatic project conversion" in one_line


def test_win_loss_keeps_lessons_confined_atomic_and_optional() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "inside the configured workspace" in content
    assert "Use a confined atomic write and check for intervening edits" in one_line
    assert "Ask before creating the file and directories, or leave the debrief on screen" in one_line
    assert "supplemental lessons note only" in content


def test_win_loss_templates_are_bounded_and_do_not_claim_runtime_health() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    inventory = _block_inventory(_SKILL)

    assert len(inventory) == 5
    assert [block["language"] for block in inventory] == ["markdown", "", "markdown", "markdown", "markdown"]
    assert "sf_stage" in content
    assert "sf_contract_end" in content
    assert "Missing or\nunparseable contract-end dates produce unknown health" in content
    assert "[DATA NEEDED]" in content
    assert "Deciding qualification evidence" in content
    assert "No Salesforce record,\nemail, calendar invitation, or Slack message is changed" in content
