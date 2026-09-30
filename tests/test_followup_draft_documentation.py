"""Public follow-up drafting instructions stay bounded and portable."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[1] / "src/fieldkit/skills/followup-draft"


def test_followup_skill_defaults_to_a_reviewable_draft_without_side_effects() -> None:
    content = (_ROOT / "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    for required in (
        "The default result is text for review",
        "Do not invent commitments, recipients, dates, or next steps",
        "Do not send email",
        "does not write meeting notes, pursuit files, tasks, or Salesforce",
        "`gws` is not bundled with fieldkit",
        "explicit approval",
        "read the created draft back",
    ):
        assert required in normalized
    for unsupported in (
        "People.AI",
        "run `/brief` first",
        "write accounts/<account>",
        "grep -i",
        "slackcli search",
    ):
        assert unsupported not in content


def test_followup_skill_links_resolve_and_avoids_runnable_looking_blocks() -> None:
    content = (_ROOT / "SKILL.md").read_text(encoding="utf-8")

    for target in (
        "email-template.md",
        "../post-meeting/SKILL.md",
        "../tool-routing/ops/workspace-tool-catalog.md",
    ):
        assert (_ROOT / target).is_file()
        assert f"]({target})" in content
    assert "```" not in content
    assert "|---" not in content


def test_followup_template_is_plain_reviewable_content() -> None:
    content = (_ROOT / "email-template.md").read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    assert "Include a section only when the source supports it" in normalized
    assert "Do not invent a deadline" in normalized
    assert "```" not in content
    assert "|---" not in content


def test_followup_evals_are_complete_fictional_fail_closed_scenarios() -> None:
    evaluations = json.loads((_ROOT / "evals/evals.json").read_text(encoding="utf-8"))
    serialized = json.dumps(evaluations)
    scenarios = {scenario["id"]: scenario for scenario in evaluations["evals"]}

    assert "{{" not in serialized
    assert {0, 1, 2} <= scenarios.keys()
    assert "Acme Corp" in scenarios[0]["prompt"]
    assert "asks for the missing meeting source" in " ".join(scenarios[1]["assertions"])
    assert "does not create a gmail draft" in " ".join(scenarios[2]["assertions"]).lower()
