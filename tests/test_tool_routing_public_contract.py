"""Public tool-routing guidance depends only on discoverable capabilities."""

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[1] / "src/fieldkit/skills/tool-routing"
_PRIVATE_ROUTES = (
    "fieldkit-sales",
    "fieldkit-dataverse",
    "backstory__",
    "brave_search__",
    "context7__",
    "gh_grep",
    "mcpjungle",
    "MCP-NECESSITY",
    "Rover employee",
    "Snowflake-backed",
)
_OBSOLETE_FILES = (
    "references/browser-cli.md",
    "references/fieldkit-dataverse.md",
    "references/fieldkit-sales.md",
    "references/non-mcp-tools.md",
    "references/server-options.md",
)


def _public_text() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(_ROOT.rglob("*"))
        if path.is_file() and path.suffix in {".json", ".md"}
    }


def test_tool_routing_tree_has_no_private_or_harness_specific_routes() -> None:
    combined = "\n".join(_public_text().values()).lower()

    for route in _PRIVATE_ROUTES:
        assert route.lower() not in combined
    for relative_path in _OBSOLETE_FILES:
        assert not (_ROOT / relative_path).exists()


def test_tool_routing_markdown_has_owned_structure_and_resolved_links() -> None:
    link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")

    for path, content in _public_text().items():
        if path.suffix != ".md":
            continue
        assert "```" not in content, path
        assert "|---" not in content, path
        for target in link_pattern.findall(content):
            if "://" in target or target.startswith("#"):
                continue
            assert (path.parent / target).is_file(), f"{path}: unresolved link {target}"


def test_workspace_catalog_uses_working_fail_closed_discovery() -> None:
    content = (_ROOT / "ops/workspace-tool-catalog.md").read_text(encoding="utf-8")

    for required in (
        "`gws` is optional and is not installed by fieldkit",
        "`gws schema SERVICE.RESOURCE.METHOD`",
        "`gws auth status`",
        "`gws auth login --services SERVICE`",
        "`fieldkit gtask create`",
        "preview by default",
        "leave the operation pending",
        "read the affected resource back",
        "`--resolve-refs` is not a required or approved discovery step",
    ):
        assert required in content


def test_root_routes_shipped_fieldkit_first_and_optional_tools_by_capability() -> None:
    content = (_ROOT / "SKILL.md").read_text(encoding="utf-8")

    for required in (
        "Start with the installed `fieldkit` command registry",
        "`fieldkit commands --json`",
        "Optional tools are capabilities, not fieldkit prerequisites",
        "Do not invent a command, endpoint, plugin, server, or compatibility route",
        "Read-only access does not authorize a write",
    ):
        assert required in content
    for target in (
        "ops/workspace-tool-catalog.md",
        "references/developer-search.md",
        "references/failure-scenarios.md",
        "references/cli-routes.md",
        "references/vault.md",
        "references/web-search.md",
        "workflows/first-time-setup.md",
    ):
        assert (_ROOT / target).is_file()
        assert f"]({target})" in content


def test_tool_routing_evals_are_fictional_and_fail_closed() -> None:
    evaluations = json.loads((_ROOT / "evals/evals.json").read_text(encoding="utf-8"))
    serialized = json.dumps(evaluations)
    scenarios = {scenario["id"]: scenario for scenario in evaluations["evals"]}

    assert "{{" not in serialized
    assert {0, 1, 2, 3, 4} <= scenarios.keys()
    assert "Acme Corp" in scenarios[0]["prompt"]
    assert "unavailable" in scenarios[3]["expected_behavior"].lower()
    assert "read-back" in " ".join(scenarios[4]["assertions"]).lower()
