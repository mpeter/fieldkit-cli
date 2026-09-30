"""Public contact workflows use shipped commands and attributed evidence."""

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[1] / "src/fieldkit/skills/contact"
_PRIVATE_ROUTES = (
    "fieldkit-sales",
    "fieldkit-dataverse",
    "backstory__",
    "mcpjungle",
    "peopleai_account_id",
    "Rover enrichment",
    "tvly search",
    "slackcli",
)


def _instruction_text() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(_ROOT.rglob("*"))
        if path.is_file() and path.suffix in {".json", ".md"}
    }


def test_contact_tree_has_no_private_routes_or_unowned_blocks() -> None:
    contents = _instruction_text()
    combined = "\n".join(contents.values()).lower()

    for route in _PRIVATE_ROUTES:
        assert route.lower() not in combined
    for path, content in contents.items():
        if path.suffix == ".md":
            assert "```" not in content, path
            assert "|---" not in content, path


def test_contact_root_routes_to_each_bounded_workflow() -> None:
    content = (_ROOT / "SKILL.md").read_text(encoding="utf-8")

    for target in (
        "ops/contact-lookup.md",
        "ops/competitive-intel.md",
        "ops/contact-enrich.md",
    ):
        assert (_ROOT / target).is_file()
        assert f"]({target})" in content
    for required in (
        "`fieldkit contact find QUERY --affiliations --json`",
        "`fieldkit contact list --account ACCOUNT --limit N --json`",
        "No external service is required",
        "Do not write by default",
    ):
        assert required in content


def test_contact_lookup_handles_cli_outcomes_and_optional_context_safely() -> None:
    content = (_ROOT / "ops/contact-lookup.md").read_text(encoding="utf-8")

    for required in (
        "`resolved`, `ambiguous`, and `not_found`",
        "initiation and engagement heuristics",
        "Do not describe either signal as verified influence",
        "it has no account\nfilter",
        "Slack is optional",
        "No contact lookup writes a workspace or external resource",
        "No matches in a completed scope",
    ):
        assert required in content
    assert "Default lookback window: 12 months" not in content


def test_competitive_workflow_and_template_require_attributed_evidence() -> None:
    workflow = (_ROOT / "ops/competitive-intel.md").read_text(encoding="utf-8")
    template = (_ROOT / "ops/competitive-intel-battlecard-template.md").read_text(encoding="utf-8")

    for required in (
        "Public research is optional",
        "source URL, publication date, and access date",
        "The default result is a draft in chat",
        "Ask before creating or replacing the file",
    ):
        assert required in workflow
    for required in (
        "Source and date",
        "Unavailable evidence",
        "Do not fill a claim from brand memory",
    ):
        assert required in template
    for unsupported in ("IBM-backed", "RH / Them / Tie", "open source, no lock-in"):
        assert unsupported.lower() not in template.lower()


def test_contact_links_resolve_and_evals_are_complete_fictional_cases() -> None:
    link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
    for path, content in _instruction_text().items():
        if path.suffix != ".md":
            continue
        for target in link_pattern.findall(content):
            if "://" in target or target.startswith("#"):
                continue
            assert (path.parent / target).is_file(), f"{path}: unresolved link {target}"

    evaluations = json.loads((_ROOT / "evals/evals.json").read_text(encoding="utf-8"))
    serialized = json.dumps(evaluations)
    scenarios = {scenario["id"]: scenario for scenario in evaluations["evals"]}
    assert "{{" not in serialized
    assert {0, 1, 2, 3, 4, 5} <= scenarios.keys()
    assert "Acme Corp" in serialized
    assert "unavailable" in " ".join(scenarios[4]["assertions"]).lower()
