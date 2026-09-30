"""Contract tests for the shipped CLI-first tool-routing instruction tree."""

import json
import re
from pathlib import Path

import pytest

from scripts.check_documentation_contract import fenced_blocks

pytestmark = pytest.mark.unit

_SKILLS_ROOT = Path(__file__).parents[1] / "src" / "fieldkit" / "skills"
_TOOL_ROUTING_ROOT = _SKILLS_ROOT / "tool-routing"
_ROOT_SKILL = _TOOL_ROUTING_ROOT / "SKILL.md"
_APPROVED_MCP_GROUPS: frozenset[str] = frozenset()
_APPROVED_DIRECT_MCP_SERVERS: frozenset[str] = frozenset()
_GROUP_PATTERN = re.compile(r"\bfieldkit-[a-z][a-z0-9-]*\b")
_MARKER_PATTERN = re.compile(r"^- MCP-NECESSITY (fieldkit-[a-z][a-z0-9-]*):", re.MULTILINE)
_DIRECT_MARKER_PATTERN = re.compile(r"^- MCP-NECESSITY direct global `([a-z][a-z0-9_]*)`:", re.MULTILINE)
_DIRECT_ROUTE_PATTERN = re.compile(r"direct global `([a-z][a-z0-9_]*)`")
_RETIRED_CLI_COVERED_ROUTES = (
    "fieldkit-dataverse",
    "fieldkit-browser",
    "fieldkit-calendar",
    "fieldkit-contacts",
    "fieldkit-docs",
    "fieldkit-drive",
    "fieldkit-dev",
    "fieldkit-jira",
    "fieldkit-mail",
    "fieldkit-markdown",
    "fieldkit-sheets",
    "fieldkit-search",
    "fieldkit-sales",
    "fieldkit-slides",
    "fieldkit-tasks",
    "google_workspace__",
    "tavily__",
    "Google Workspace MCP",
    "Gmail MCP",
    "Tavily MCP",
    "MCP gateway must be running",
    "mcpjungle",
    "Do not call external services without first checking gateway status",
    "Wrong tool group",
)
_GATEWAY_CHECK_FILES: frozenset[str] = frozenset()
_APPROVED_MCP_REFERENCES: dict[str, frozenset[str]] = {}


def _load_instruction_text() -> dict[Path, str]:
    instruction_files = [
        path for path in sorted(_TOOL_ROUTING_ROOT.rglob("*")) if path.is_file() and path.suffix in {".md", ".json"}
    ]
    return {path: path.read_text(encoding="utf-8") for path in instruction_files}


def _assert_route_inventory(instruction_text: dict[Path, str]) -> None:
    routed_text = "\n".join(instruction_text.values())
    retired_routes = {route for route in _RETIRED_CLI_COVERED_ROUTES if route in routed_text}
    assert not retired_routes, f"retired MCP routes remain: {sorted(retired_routes)}"

    referenced_direct_servers = frozenset(
        server for content in instruction_text.values() for server in _DIRECT_ROUTE_PATTERN.findall(content)
    )
    assert referenced_direct_servers == _APPROVED_DIRECT_MCP_SERVERS, (
        f"direct MCP route inventory mismatch: {sorted(referenced_direct_servers)}"
    )

    actual_references = {
        group: frozenset(
            path.relative_to(_SKILLS_ROOT).as_posix() for path, content in instruction_text.items() if group in content
        )
        for group in _APPROVED_MCP_GROUPS
    }
    assert actual_references == _APPROVED_MCP_REFERENCES
    direct_references = {
        server: frozenset(
            path.relative_to(_SKILLS_ROOT).as_posix() for path, content in instruction_text.items() if server in content
        )
        for server in _APPROVED_DIRECT_MCP_SERVERS
    }
    assert direct_references == _APPROVED_DIRECT_MCP_REFERENCES


_APPROVED_DIRECT_MCP_REFERENCES: dict[str, frozenset[str]] = {}


def test_all_mcp_groups_have_necessity_markers() -> None:
    skill_text = _ROOT_SKILL.read_text(encoding="utf-8")
    referenced_groups = frozenset(_GROUP_PATTERN.findall(skill_text))
    necessity_markers = _MARKER_PATTERN.findall(skill_text)
    direct_necessity_markers = _DIRECT_MARKER_PATTERN.findall(skill_text)

    assert referenced_groups == _APPROVED_MCP_GROUPS
    assert frozenset(necessity_markers) == _APPROVED_MCP_GROUPS
    assert len(necessity_markers) == len(_APPROVED_MCP_GROUPS)
    assert frozenset(direct_necessity_markers) == _APPROVED_DIRECT_MCP_SERVERS
    assert len(direct_necessity_markers) == len(_APPROVED_DIRECT_MCP_SERVERS)


def test_cli_covered_instruction_tree_has_no_mcp_routes() -> None:
    instruction_text = _load_instruction_text()

    _assert_route_inventory(instruction_text)
    gateway_check_files = frozenset(
        path.relative_to(_SKILLS_ROOT).as_posix()
        for path, content in instruction_text.items()
        if "systemctl --user status mcpjungle" in content
    )
    assert gateway_check_files == _GATEWAY_CHECK_FILES


@pytest.mark.parametrize("relative_path", ["meeting/SKILL.md", "meeting/ops/qbr-prep.md"])
def test_meeting_workflows_do_not_require_private_harness_or_knowledge_service(relative_path: str) -> None:
    content = (_SKILLS_ROOT / relative_path).read_text(encoding="utf-8")

    assert "thesource" not in content.lower()
    assert "CLAUDE.md" not in content
    assert re.search(r"No internal knowledge service is\s+bundled with\s+fieldkit\.", content)


def test_meeting_root_keeps_optional_sources_and_batch_identity_fail_closed() -> None:
    skill = _SKILLS_ROOT / "meeting/SKILL.md"
    content = skill.read_text(encoding="utf-8")

    for target in (
        "brief-template.md",
        "ops/qbr-prep.md",
        "ops/account-snapshot.md",
        "ops/account-pulse.md",
        "ops/stakeholder-map.md",
        "ops/one-on-one.md",
        "../followup-draft/SKILL.md",
        "../tool-routing/references/sf-next-steps-protocol.md",
    ):
        assert (skill.parent / target).is_file()
    assert "No internal knowledge service is bundled with fieldkit." in content
    assert "Never choose\nthe first match by list order" in content
    assert "Obtain confirmation before writing" in content
    commands = [block.body.strip() for block in fenced_blocks(skill) if block.language == "console"]
    assert commands == [
        "fieldkit contact find <email> --json",
        "fieldkit sf meddpicc <opp_id> --json",
    ]
    normalized = " ".join(content.split())
    assert "configured and authorized Salesforce identity" in normalized
    assert "failed or incomplete read is unavailable" in normalized
    assert "same-candidate" not in normalized
    assert content.count("```") == 4
    assert "|---" not in content


def test_qbr_workflow_uses_one_linked_output_template() -> None:
    workflow = _SKILLS_ROOT / "meeting/ops/qbr-prep.md"
    content = workflow.read_text(encoding="utf-8")

    assert "[QBR output template](qbr-prep-output-template.md)" in content
    assert (workflow.parent / "qbr-prep-output-template.md").is_file()
    assert "```" not in content
    for target in ("account-pulse.md", "../SKILL.md", "../../grill/SKILL.md", "../../tool-routing/SKILL.md"):
        assert (workflow.parent / target).is_file()


def test_qbr_output_template_keeps_evidence_labels_and_four_reviewed_tables() -> None:
    content = (_SKILLS_ROOT / "meeting/ops/qbr-prep-output-template.md").read_text(encoding="utf-8")

    assert "Fill sections from inspected, dated sources" in content
    assert "Mark missing signals unavailable" in content
    assert "[Presenter]" in content
    assert "```" not in content
    for header in (
        "| Initiative | Outcome | Business Impact |",
        "| Stakeholder | Role | Engagement Signal | Notes |",
        "| Commitment | Owner | Status |",
        "| Opportunity | Their Need | Our Capability | Proposed Next Step |",
    ):
        assert header in content


def test_meeting_brief_template_treats_optional_intelligence_as_unverified() -> None:
    content = (_SKILLS_ROOT / "meeting/brief-template.md").read_text(encoding="utf-8")

    assert "Optional Account Intelligence" in content
    assert "unavailable" in content
    assert "source and date" in content
    assert "Do not infer" in content
    assert "```" not in content
    assert "| Name | Title | Role in Deal | Our History with Them | Style Notes |" in content
    assert "| Objection | Response |" in content


def test_account_snapshot_uses_local_reports_without_invented_signals() -> None:
    workflow = _SKILLS_ROOT / "meeting/ops/account-snapshot.md"
    content = workflow.read_text(encoding="utf-8")

    commands = [block.body.strip() for block in fenced_blocks(workflow) if block.language == "console"]
    assert commands == [
        "fieldkit pursuit projects --account <account> --json",
        "fieldkit pursuit health --account <account> --json",
        "fieldkit gmail query account <account> --since <YYYY-MM-DD> --limit 10 --json",
        "fieldkit gmail decay --account <account> --limit 10 --json",
    ]
    normalized = " ".join(content.split()).lower()
    assert "configured workspace and published cache" in normalized
    assert "configured and authorized identity" in normalized
    assert "candidate revision" not in normalized
    assert "`ZOMBIE`, `EXPIRING`, `SOON`, `ACTIVE`, and `UNKNOWN`" in content
    assert "does not establish sender direction" in content
    assert "Backstory Signal: unavailable" in content
    assert "Show\nthe draft and ask for confirmation before saving" in content
    assert content.count("```") == 8


def test_account_snapshot_evals_require_available_evidence_and_write_confirmation() -> None:
    evals = json.loads((_SKILLS_ROOT / "meeting/evals/evals.json").read_text(encoding="utf-8"))
    scenarios = {scenario["id"]: scenario for scenario in evals["evals"]}
    snapshot = " ".join(scenarios[5]["assertions"])

    assert "unknown account" in snapshot
    assert "cache evidence unavailable" in snapshot
    assert "asks before saving" in snapshot
    assert "Skips Backstory API calls" in scenarios[7]["assertions"]
    assert "{{" not in scenarios[5]["prompt"] + scenarios[7]["prompt"]


@pytest.mark.parametrize(
    ("relative_path", "injected_text", "expected_error"),
    [
        ("tool-routing/references/web-search.md", "Use fieldkit-search.\n", "retired MCP routes remain"),
        ("tool-routing/references/developer-search.md", "Use fieldkit-dev.\n", "retired MCP routes remain"),
        ("tool-routing/SKILL.md", "Use direct global `unknown_search`.\n", "direct MCP route inventory mismatch"),
    ],
)
def test_route_inventory_rejects_retired_and_unknown_routes(
    relative_path: str, injected_text: str, expected_error: str
) -> None:
    instruction_text = _load_instruction_text()
    target = _SKILLS_ROOT / relative_path
    instruction_text[target] += injected_text

    with pytest.raises(AssertionError, match=expected_error):
        _assert_route_inventory(instruction_text)
