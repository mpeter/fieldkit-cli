"""Semantic ownership for the public documentation entry points."""

import json
import re
import tomllib
from pathlib import Path

import pytest

from scripts.check_documentation_contract import fenced_blocks
from scripts.documentation_commands import DOCUMENT_COMMANDS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PAGES = ("docs/index.md", "docs/user-guide.md")


@pytest.mark.parametrize("page", PAGES)
def test_overview_navigation_resolves_within_public_docs(page: str) -> None:
    document = (ROOT / page).read_text(encoding="utf-8")
    links = re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", document)

    assert links
    for target in links:
        if target.startswith("https://"):
            assert target.startswith("https://github.com/mpeter/fieldkit-cli/")
            continue
        path = target.split("#", 1)[0]
        resolved = (ROOT / page).parent.joinpath(path).resolve()
        assert resolved.is_relative_to(ROOT)
        assert resolved.is_file(), f"{page}: missing {target}"


def test_index_profiles_match_package_extras() -> None:
    document = (ROOT / "docs/index.md").read_text(encoding="utf-8")
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = set(package["project"]["optional-dependencies"])

    assert extras == {"google", "llm", "web", "chrome-auth", "all"}
    assert document.count("## Choose what you need") == 1
    assert document.count("## Find an answer") == 1
    for profile in extras:
        assert f"`{profile}`" in document
    assert "base installation works without\ncredentials or network-backed integrations after installation" in document


def test_user_guide_has_one_local_first_action_and_pending_provider_example() -> None:
    page = ROOT / "docs/user-guide.md"
    document = page.read_text(encoding="utf-8")
    blocks = fenced_blocks(page)
    contract = json.loads((ROOT / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    records = contract["documents"]["docs/user-guide.md"]["fenced_blocks"]

    assert document.index("## Start with local workflows") < document.index("## Add context deliberately")
    assert document.index("## Add context deliberately") < document.index("## Build a daily workflow")
    assert document.index("## Build a daily workflow") < document.index("## Understand generated state")
    assert len(blocks) == len(records) == 3
    assert [record["classification"] for record in records] == [
        "safe_automated_command",
        "credentialed_manual_integration",
        "safe_automated_command",
    ]
    assert "fieldkit init --minimal ./fieldkit-workspace" in blocks[0].body
    assert "fieldkit sf meddpicc <opportunity-id> --json" in blocks[1].body
    assert "fieldkit brief generate --pipeline-only --no-llm --dry-run" in blocks[2].body
    assert "A pending preview exits `1` and does not advance." in " ".join(document.split())


def test_overview_pages_have_one_fixed_page_owner() -> None:
    contract = json.loads((ROOT / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    paths = contract["verification"]["overview_page_contract"]["paths"]
    assert paths == list(PAGES)
    assert DOCUMENT_COMMANDS["overview_page_contract"] == (
        (
            "uv",
            "run",
            "pytest",
            "tests/test_overview_page_contract.py",
            "tests/test_documentation_integration_profiles.py",
            "tests/test_documentation_pursuit_workflow.py",
            "tests/test_brief_documentation_scenarios.py::test_documented_morning_brief_previews_are_offline_and_read_only",
            "-q",
            "-n",
            "0",
        ),
    )
