"""Contract tests for the shipped CLI-first tool-routing instruction tree."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SKILLS_ROOT = Path(__file__).parents[1] / "src" / "fieldkit" / "skills"
_TOOL_ROUTING_ROOT = _SKILLS_ROOT / "tool-routing"
_ROOT_SKILL = _TOOL_ROUTING_ROOT / "SKILL.md"
_APPROVED_MCP_GROUPS = frozenset(
    {
        "fieldkit-dataverse",
        "fieldkit-sales",
    }
)
_APPROVED_DIRECT_MCP_SERVERS = frozenset({"brave_search", "context7", "gh_grep"})
_GROUP_PATTERN = re.compile(r"\bfieldkit-[a-z][a-z0-9-]*\b")
_MARKER_PATTERN = re.compile(r"^- MCP-NECESSITY (fieldkit-[a-z][a-z0-9-]*):", re.MULTILINE)
_DIRECT_MARKER_PATTERN = re.compile(r"^- MCP-NECESSITY direct global `([a-z][a-z0-9_]*)`:", re.MULTILINE)
_DIRECT_ROUTE_PATTERN = re.compile(r"direct global `([a-z][a-z0-9_]*)`")
_RETIRED_CLI_COVERED_ROUTES = (
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
    "fieldkit-slides",
    "fieldkit-tasks",
    "google_workspace__",
    "tavily__",
    "Google Workspace MCP",
    "Gmail MCP",
    "Tavily MCP",
    "MCP gateway must be running",
    "Do not call external services without first checking gateway status",
    "Wrong tool group",
)
_GATEWAY_CHECK_FILES = frozenset(
    {
        "tool-routing/SKILL.md",
        "tool-routing/references/failure-scenarios.md",
        "tool-routing/references/server-options.md",
        "tool-routing/workflows/first-time-setup.md",
    }
)
_APPROVED_MCP_REFERENCES = {
    "fieldkit-sales": frozenset(
        {
            "brief/SKILL.md",
            "brief/evals/evals.json",
            "brief/ops/update.md",
            "brief/ops/week-start.md",
            "contact/SKILL.md",
            "contact/evals/evals.json",
            "contact/ops/competitive-intel.md",
            "meeting/SKILL.md",
            "meeting/evals/evals.json",
            "meeting/ops/account-pulse.md",
            "meeting/ops/account-snapshot.md",
            "meeting/ops/one-on-one.md",
            "meeting/ops/qbr-prep.md",
            "meeting/ops/stakeholder-map.md",
            "start/SKILL.md",
            "start/evals/evals.json",
            "tool-routing/SKILL.md",
            "tool-routing/evals/evals.json",
            "tool-routing/references/failure-scenarios.md",
            "tool-routing/references/fieldkit-sales.md",
            "tool-routing/references/server-options.md",
            "tool-routing/workflows/first-time-setup.md",
            "workstream-discover/SKILL.md",
            "workstream-discover/evals/evals.json",
        }
    ),
    "fieldkit-dataverse": frozenset(
        {
            "contact/SKILL.md",
            "contact/ops/contact-enrich.md",
            "meeting/SKILL.md",
            "tool-routing/SKILL.md",
            "tool-routing/evals/evals.json",
            "tool-routing/references/failure-scenarios.md",
            "tool-routing/references/fieldkit-dataverse.md",
            "tool-routing/references/server-options.md",
            "tool-routing/workflows/first-time-setup.md",
        }
    ),
}


def _load_instruction_text() -> dict[Path, str]:
    instruction_files = [
        path for path in sorted(_SKILLS_ROOT.rglob("*")) if path.is_file() and path.suffix in {".md", ".json"}
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


_APPROVED_DIRECT_MCP_REFERENCES = {
    "brave_search": frozenset(
        {
            "tool-routing/SKILL.md",
            "tool-routing/evals/evals.json",
            "tool-routing/references/failure-scenarios.md",
            "tool-routing/references/web-search.md",
            "tool-routing/references/server-options.md",
            "tool-routing/workflows/first-time-setup.md",
        }
    ),
    "context7": frozenset(
        {
            "tool-routing/SKILL.md",
            "tool-routing/evals/evals.json",
            "tool-routing/references/developer-search.md",
            "tool-routing/references/failure-scenarios.md",
            "tool-routing/references/server-options.md",
            "tool-routing/workflows/first-time-setup.md",
        }
    ),
    "gh_grep": frozenset(
        {
            "tool-routing/SKILL.md",
            "tool-routing/evals/evals.json",
            "tool-routing/references/developer-search.md",
            "tool-routing/references/failure-scenarios.md",
            "tool-routing/references/non-mcp-tools.md",
            "tool-routing/references/server-options.md",
            "tool-routing/workflows/first-time-setup.md",
        }
    ),
}


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
