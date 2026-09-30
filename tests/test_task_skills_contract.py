"""Public safety contract for local task management and Google Tasks sync."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SKILLS = Path(__file__).parents[1] / "src" / "fieldkit" / "skills"


def _skill(name: str) -> str:
    return (_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _prose(name: str) -> str:
    return " ".join(_skill(name).split())


def test_task_management_uses_the_configured_workspace_and_requires_write_approval() -> None:
    content = _skill("task-management")
    prose = _prose("task-management")

    assert "configured fieldkit workspace root" in prose
    assert "not the fieldkit source checkout" in prose
    assert "Show the exact proposed diff" in prose
    assert "Do not write until the operator approves" in prose
    assert "<!-- task-sync:start -->" in content
    assert "<!-- task-sync:end -->" in content
    assert "Backstory" not in content
    assert "Jira" not in content
    assert "Linear" not in content


def test_task_management_does_not_claim_unverified_source_facts() -> None:
    prose = _prose("task-management")

    assert "A subject line or summary is not proof of a commitment" in prose
    assert "Unknown owners and due dates remain unknown" in prose
    assert "Do not infer qualification gaps" in prose
    assert "memory/system/lessons-learned.md" not in prose


def test_task_sync_uses_shipped_preview_first_mutations() -> None:
    content = _skill("task-sync")

    assert "fieldkit gtask create" in content
    assert "fieldkit gtask complete" in content
    assert "Preview is the default" in content
    assert "--confirm" in content
    assert "gws tasks tasks insert" not in content
    assert "gws tasks tasks patch" not in content


def test_task_sync_is_fail_closed_and_does_not_claim_full_automation() -> None:
    content = _skill("task-sync")
    prose = _prose("task-sync")

    assert "fieldkit does not ship a one-command bidirectional sync" in prose
    assert "A partial pull is not authoritative" in prose
    assert "separate approval for remote and local writes" in prose
    assert "Read back Google Tasks after every confirmed remote write" in prose
    assert "Never change content outside the two task-sync markers" in prose
    assert "ask whether it belongs in `Today` or `Active`" in prose
    assert "do not claim Google stores it" in prose
    assert "import unrelated completed history with no local anchor" in prose
    assert "caller-enforced 30-second process timeout" in prose
    assert "Do not retain authentication output" in prose
    assert "```" not in content


@pytest.mark.parametrize("name", ["task-management", "task-sync"])
def test_task_skill_evals_require_preview_and_confirmation(name: str) -> None:
    payload = json.loads((_SKILLS / name / "evals" / "evals.json").read_text(encoding="utf-8"))
    encoded = json.dumps(payload)

    assert "preview" in encoded.lower()
    assert "confirm" in encoded.lower()
