"""Contracts for configured routes and the shipped tool-routing instruction tree."""

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from fieldkit.commands.skill.eval_runner import eval_skill

pytestmark = pytest.mark.unit

_SKILLS_ROOT = Path(__file__).parents[1] / "src" / "fieldkit" / "skills"
_TOOL_ROUTING_ROOT = _SKILLS_ROOT / "tool-routing"
_ROOT_SKILL = _TOOL_ROUTING_ROOT / "SKILL.md"
# The approved MCP exception set: each capability and the file that scopes it.
_APPROVED_MCP_EXCEPTIONS = {
    "account-intelligence": "references/account-intelligence.md",
    "enterprise-data": "references/enterprise-data.md",
    "code-pattern-index": "references/developer-search.md",
    "curated-library-docs": "references/developer-search.md",
    "independent-search-index": "references/web-search.md",
}
_MARKER_PATTERN = re.compile(r"^- MCP-NECESSITY ([a-z][a-z-]*): (.+)$", re.MULTILINE)
_STALE_ROUTES = (
    "fieldkit-sales",
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
    "fieldkit-slides",
    "fieldkit-tasks",
    "google_workspace__",
    "tavily__",
    "MCP gateway must be running",
    "Do not call external services without first checking gateway status",
)
_DIRECT_ROUTE_PATTERN = re.compile(r"\bdirect(?: global\s+`[a-z][a-z0-9_-]*`|\s+`(?:context7|gh_grep|brave_search)`)")
_BACKSTORY_ACCOUNT_TOOLS = frozenset(
    {
        "find_account",
        "get_account_status",
        "get_recent_account_activity",
        "account_company_news",
        "find_record_by_crm_id",
    }
)


def _load_instruction_text() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(_SKILLS_ROOT.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".json"}
    }


def _assert_route_inventory(instruction_text: dict[Path, str]) -> None:
    routed_text = "\n".join(instruction_text.values())
    stale_routes = {route for route in _STALE_ROUTES if route in routed_text}
    assert not stale_routes, f"stale MCP routes remain: {sorted(stale_routes)}"
    direct_routes = set(_DIRECT_ROUTE_PATTERN.findall(routed_text))
    assert not direct_routes, f"assumed direct MCP routes remain: {sorted(direct_routes)}"
    backstory_tools = set(re.findall(r"\bbackstory__(\w+)", routed_text))
    bare_opportunity_calls = set(
        re.findall(r"\b((?:find|get|search|list)_[a-z_]*opportunity[a-z_]*)\s*\(", routed_text)
    )
    unsupported_tools = (backstory_tools - _BACKSTORY_ACCOUNT_TOOLS) | bare_opportunity_calls
    assert not unsupported_tools, f"unsupported Backstory calls remain: {sorted(unsupported_tools)}"


def _assert_mcp_exceptions(instruction_text: dict[Path, str]) -> None:
    markers = _MARKER_PATTERN.findall(instruction_text.get(_ROOT_SKILL, ""))
    names = [name for name, _ in markers]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"duplicate MCP-NECESSITY markers: {duplicates}"
    unexpected = sorted(set(names) ^ set(_APPROVED_MCP_EXCEPTIONS))
    assert not unexpected, f"MCP exception inventory differs from the approved set: {unexpected}"
    for name, rationale in markers:
        assert f"]({_APPROVED_MCP_EXCEPTIONS[name]})" in rationale, (
            f"MCP-NECESSITY {name} must link its capability file"
        )
    stray = sorted(
        path.name for path, text in instruction_text.items() if path != _ROOT_SKILL and "MCP-NECESSITY" in text
    )
    assert not stray, f"MCP-NECESSITY markers outside the root skill: {stray}"


def test_shipped_mcp_exceptions_match_the_approved_set() -> None:
    _assert_mcp_exceptions(_load_instruction_text())


def _root_markers() -> str:
    return "\n".join(line for line in _ROOT_SKILL.read_text(encoding="utf-8").splitlines() if "MCP-NECESSITY" in line)


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (lambda text: text.split("\n", 1)[1], "inventory differs"),
        (lambda text: text + "\n" + text.split("\n", 1)[0], "duplicate MCP-NECESSITY"),
        (lambda text: text + "\n- MCP-NECESSITY calendar: Use MCP. See [x](references/x.md).", "inventory differs"),
        (
            lambda text: text.replace("references/web-search.md", "references/non-mcp-tools.md"),
            "must link its capability file",
        ),
    ],
)
def test_mcp_exception_inventory_rejects_drift(mutate: Callable[[str], str], expected_error: str) -> None:
    with pytest.raises(AssertionError, match=expected_error):
        _assert_mcp_exceptions({_ROOT_SKILL: mutate(_root_markers())})


def test_mcp_exception_markers_stay_in_the_root_skill() -> None:
    instruction_text = {
        _ROOT_SKILL: _root_markers(),
        _TOOL_ROUTING_ROOT / "references" / "web-search.md": "- MCP-NECESSITY independent-search-index: Brave.",
    }
    with pytest.raises(AssertionError, match="outside the root skill"):
        _assert_mcp_exceptions(instruction_text)


def test_shipped_instruction_tree_has_no_stale_routes_or_opportunity_calls() -> None:
    _assert_route_inventory(_load_instruction_text())


@pytest.mark.parametrize(
    ("injected_text", "expected_error"),
    [
        ("Use fieldkit-search.", "stale MCP routes remain"),
        ("Use fieldkit-sales.", "stale MCP routes remain"),
        ("Use fieldkit-dataverse.", "stale MCP routes remain"),
        ("Use direct global `unknown_search`.", "assumed direct MCP routes remain"),
        ("Use direct `context7`.", "assumed direct MCP routes remain"),
        ("Call backstory__get_opportunity_status.", "unsupported Backstory calls remain"),
        ("Call backstory__find_opportunity.", "unsupported Backstory calls remain"),
        ("Call get_opportunity_status(<account>).", "unsupported Backstory calls remain"),
        ("Call get_recent_opportunity_activity(<account>).", "unsupported Backstory calls remain"),
    ],
)
def test_route_inventory_rejects_unsafe_instructions(injected_text: str, expected_error: str) -> None:
    instruction_text = {_TOOL_ROUTING_ROOT / "SKILL.md": injected_text}
    with pytest.raises(AssertionError, match=expected_error):
        _assert_route_inventory(instruction_text)


def test_tool_routing_shipped_static_checks_pass() -> None:
    result = eval_skill(_TOOL_ROUTING_ROOT)
    assert result["static_fail"] == 0, result["static_results"]
    assert result["static_pass"] > 0
    assert len(result["behavioral_cases"]) == 16


@pytest.mark.parametrize(
    "eval_path", sorted(_SKILLS_ROOT.glob("*/evals/evals.json")), ids=lambda path: path.parent.parent.name
)
def test_all_shipped_behavioral_cases_have_judge_assertions(eval_path: Path) -> None:
    eval_data = json.loads(eval_path.read_text(encoding="utf-8"))
    cases = eval_data.get("evals", []) if isinstance(eval_data, dict) else eval_data
    for case in cases:
        assertions = case.get("assertions")
        assert isinstance(assertions, list) and assertions, f"{eval_path}: case {case.get('id')} needs assertions"
        assert all(isinstance(assertion, str) and assertion.strip() for assertion in assertions)


@pytest.mark.parametrize(
    ("relative_path", "required_text"),
    [
        ("SKILL.md", "read the active harness configuration"),
        ("SKILL.md", "a group is registered, a tool is loaded, or upstream authentication works"),
        ("SKILL.md", "`userId=me` alone does not"),
        ("SKILL.md", "Creating a draft does not authorize"),
        ("SKILL.md", "read back the"),
        ("SKILL.md", "existing user authorization"),
        ("SKILL.md", "Do not widen a"),
        ("SKILL.md", "do not substitute a personal account or another workspace"),
        ("SKILL.md", "report the missing capability"),
        ("SKILL.md", "Do not reset OAuth or run bulk registration"),
        ("references/account-intelligence.md", "Do not call opportunity-level Backstory tools"),
        ("references/account-intelligence.md", "for account records only"),
        ("references/account-intelligence.md", "Salesforce remains the deal system of record"),
        ("references/server-options.md", "does not guarantee a group inventory or direct server"),
        ("references/failure-scenarios.md", "connection-path graph operations have no maintained"),
        ("workflows/first-time-setup.md", "Do not reset"),
    ],
)
def test_routing_identity_authority_and_failure_contracts(relative_path: str, required_text: str) -> None:
    text = (_TOOL_ROUTING_ROOT / relative_path).read_text(encoding="utf-8")
    assert required_text in text


@pytest.mark.parametrize(
    "cli", ["fieldkit sf", "fieldkit gmail query", "gws", "tvly", "slackcli", "gh", "qmd", "ctx", "chrome-use"]
)
def test_cli_capabilities_remain_discoverable(cli: str) -> None:
    text = (_TOOL_ROUTING_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert f"`{cli}" in text


def test_public_routing_does_not_import_private_installation_contracts() -> None:
    text = "\n".join(
        content for path, content in _load_instruction_text().items() if path.is_relative_to(_TOOL_ROUTING_ROOT)
    )
    assert not re.search(r"\bFieldkit-[a-z]+\b", text)
    assert not re.search(r"`(?:work|research|docs|dev)` alias", text)
    assert "Work profile" not in text
    assert "/home/" not in text
