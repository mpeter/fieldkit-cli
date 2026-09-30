"""Semantic and structural contract for the public workstream-discover skill."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SKILL = Path(__file__).parents[1] / "src/fieldkit/skills/workstream-discover/SKILL.md"


def test_workstream_discovery_uses_authorized_bounded_sources() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    for required in (
        "Select specific workspace account, pursuit, and project files",
        "reject path traversal or symlink escapes",
        "bound reads",
        "Live email, document, Slack, CRM, and web research are optional",
        "explicit result/page limits and a finite timeout",
        "Incomplete reads stay partial",
    ):
        assert required in one_line
    assert "depend on a private maintainer service" in one_line


def test_workstream_candidates_remain_attributed_hypotheses() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "Candidates are hypotheses for operator review" in one_line
    assert "not verified customer intent, booked revenue, or qualified deals" in one_line
    assert "unknown, or sourced amount with currency and value basis" in one_line
    assert "sourced candidate and rationale, or unknown" in one_line
    assert "unknown, or evidence-backed timing hypothesis" in one_line
    assert "return no supported candidate" in one_line


def test_workstream_output_template_preserves_source_and_unknown_fields() -> None:
    content = _SKILL.read_text(encoding="utf-8")

    assert "Source: [Slack channel / SOW / delivery team] — Date: [date]" in content
    assert "**Estimated Value:** [unknown, or sourced amount with currency and value basis]" in content
    assert "**Proposed Champion:** [sourced candidate and rationale, or unknown]" in content
    assert "**Readiness:** [unknown, or evidence-backed timing hypothesis]" in content
    assert content.count("```") == 2
    assert "|---" not in content


def test_workstream_discovery_requires_approved_confined_writes() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "does not modify local files or external records" in one_line
    assert "Do not send a message or schedule an event" in one_line
    assert "Obtain approval before creating directories" in one_line
    assert "Preserve unrelated content and Salesforce-owned fields" in one_line
    assert "confined atomic write" in one_line
    assert "check for intervening edits, and reread" in one_line
    assert "A new pursuit requires a separate operator decision" in one_line
    assert "never changes native ClosePlan state" in one_line
