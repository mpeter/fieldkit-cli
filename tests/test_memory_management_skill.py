"""Public memory-management instructions use supported workspace storage."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[1] / "src/fieldkit/skills/memory-management"


@pytest.mark.parametrize(
    "required",
    [
        "memory/system/lessons-learned.md",
        "memory/personal/contacts/",
        "Show the proposed destination and text before writing.",
        "`AGENTS.md`, `CLAUDE.md`, and other harness instructions are not working",
    ],
)
def test_memory_skill_uses_portable_workspace_contract(required: str) -> None:
    content = (_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert required in content
    assert "formats.md" not in content
    assert "memory/people/" not in content
    assert "memory/projects/" not in content


def test_memory_skill_setup_link_resolves() -> None:
    assert (_ROOT / "../start/SKILL.md").is_file()


def test_memory_skill_eval_uses_complete_fictional_scenario() -> None:
    evaluations = json.loads((_ROOT / "evals/evals.json").read_text(encoding="utf-8"))
    case = evaluations["evals"][0]

    assert "acme-corp" in case["prompt"]
    assert "Platform Renewal" in case["prompt"]
    assert "{{" not in json.dumps(case)
