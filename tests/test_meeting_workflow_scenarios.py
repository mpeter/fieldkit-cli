"""Fixed local scenarios and structural contracts for meeting report guides."""

from __future__ import annotations

import json
import os
import shlex
import socket
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.check_documentation_contract import fenced_blocks
from scripts.markdown_tables import markdown_tables
from tests.documentation_workflow_support import (
    DOCUMENTED_DATE,
    invoke_workflow,
    prepare_documented_gmail_cache,
    snapshot_workflow,
    write_documented_pursuit_data,
    write_second_documented_account,
)

pytestmark = pytest.mark.integration

_ROOT = Path("src/fieldkit/skills/meeting/ops")
_COMMANDS = {
    "projects-account": "fieldkit pursuit projects --account <account> --json",
    "health-account": "fieldkit pursuit health --account <account> --json",
    "gmail-blindspots": "fieldkit gmail query blindspots <account> --since <YYYY-MM-DD> --limit 10 --json",
    "crm-candidates": "fieldkit gmail backstory-gap --account <account> --limit 10 --json",
    "contact": "fieldkit contact find <email> --affiliations --json",
    "contact-basic": "fieldkit contact find <email> --json",
    "gmail-account": "fieldkit gmail query account <account> --since <YYYY-MM-DD> --limit 10 --json",
    "gmail-decay": "fieldkit gmail decay --account <account> --limit 10 --json",
    "forecast": "fieldkit pursuit forecast --json",
    "health": "fieldkit pursuit health --json",
    "projects": "fieldkit pursuit projects --json",
    "opportunity-help": "fieldkit sf opportunity --help",
}
_PAGE_COMMANDS = {
    "account-pulse.md": ("projects-account", "health-account", "gmail-blindspots", "crm-candidates"),
    "stakeholder-map.md": ("contact", "gmail-account", "opportunity-help"),
    "one-on-one.md": ("forecast", "health", "projects"),
}
_TABLE_HEADERS = {
    "account-pulse.md": ("| Account | Source | As of / scope | Status | Note |",),
    "stakeholder-map.md": (
        "| Source | As of / scope | Status | Note |",
        "| Name | Title | Role | Basis | Influence | Support | Last contact | Owner | Gap? |",
    ),
    "one-on-one.md": ("| Source | As of / scope | Status | Note |",),
}
_PENDING_COMMAND = "fieldkit sf opportunity <opportunity_id> --no-write"
_TEMPLATE_ROWS = {
    "account-pulse.md": (
        (
            ("[account]", "pursuit projects", "[date]", "verified", "[coverage]"),
            ("[account]", "Gmail cache", "[range or unknown]", "unavailable", "[reason]"),
        ),
    ),
    "stakeholder-map.md": (
        (
            ("account record", "[date or unknown]", "verified", "[scope]"),
            ("Gmail cache", "[range]", "unavailable", "[reason]"),
        ),
        (("[name]", "[title]", "Unknown", "insufficient evidence", "Unknown", "Unknown", "[date]", "Unknown", "Y"),),
    ),
    "one-on-one.md": (
        (
            ("pursuit forecast", "[report date]", "verified", "current local scenarios"),
            ("transition history", "[lookback]", "unavailable", "[reason]"),
        ),
    ),
}
_TEMPLATE_SECTIONS = {
    "account-pulse.md": (
        "## Source status",
        "## Needs action",
        "## Active",
        "## Triggers to review",
        "## Delivery alerts",
        "## Contact review candidates",
        "## Insufficient evidence",
        "## Suggested priority",
    ),
    "stakeholder-map.md": ("## Source status", "## Stakeholders", "## Evidence notes", "## Coverage gaps"),
    "one-on-one.md": (
        "### Source status",
        "### Closed / won in the lookback",
        "### Wins and positive signals",
        "### At risk / needs attention",
        "### Pipeline numbers",
        "### This week's focus",
        "### Asks / escalations",
    ),
}


def _validate_report_template(path: Path) -> None:
    page = path.name
    templates = [block for block in fenced_blocks(path) if block.language == "markdown"]
    assert len(templates) == 1, "report requires one template"
    tables = markdown_tables(path)
    assert tuple(table.header for table in tables) == tuple(
        tuple(cell.strip() for cell in header.strip("|").split("|")) for header in _TABLE_HEADERS[page]
    ), "report table headers or separators changed"
    assert tuple(table.rows for table in tables) == _TEMPLATE_ROWS[page], (
        "report source status or unknown semantics changed"
    )
    sections = tuple(line for line in templates[0].body.splitlines() if line.startswith(("## ", "### ")))
    assert all(section in sections for section in _TEMPLATE_SECTIONS[page]), "report required sections changed"
    assert "Replace sample statuses" in path.read_text(encoding="utf-8"), "report needs an observed-status safeguard"


def _argv(identifier: str) -> list[str]:
    command = (
        _COMMANDS[identifier]
        .replace("<account>", "acme-corp")
        .replace("<email>", "alex@acme-corp.example.com")
        .replace("<YYYY-MM-DD>", DOCUMENTED_DATE)
    )
    return shlex.split(command)[1:]


def test_workflow_fixture_confines_every_configured_state_root(documented_workspace: Path) -> None:
    trial_root = documented_workspace.parent

    assert Path(os.environ["HOME"]).is_relative_to(trial_root)
    assert Path(os.environ["XDG_CONFIG_HOME"]).is_relative_to(trial_root)
    assert Path(os.environ["FIELDKIT_DATA_DIR"]).is_relative_to(trial_root)


def test_meeting_fixture_uses_the_documentation_network_denial(documented_workspace: Path) -> None:
    assert documented_workspace.is_dir()
    with (
        socket.socket() as connection,
        pytest.raises(AssertionError, match="documentation scenario attempted network access"),
    ):
        connection.connect(("example.invalid", 443))


def test_account_pulse_preserves_incomplete_candidate_scan_status() -> None:
    text = (_ROOT / "account-pulse.md").read_text(encoding="utf-8")

    assert "`scan_truncated: true`" in text
    assert "partial exit `1`" in text
    assert "`scanned_rows`" in text
    assert "not a complete inventory or an absence finding" in text


def test_meeting_preparation_binds_local_contact_and_pending_credentialed_read() -> None:
    page = _ROOT.parent / "SKILL.md"
    blocks = fenced_blocks(page)

    assert tuple(block.body.strip() for block in blocks) == (
        _COMMANDS["contact-basic"],
        "fieldkit sf meddpicc <opp_id> --json",
    )
    assert all(block.language == "console" for block in blocks)
    assert markdown_tables(page) == ()
    text = page.read_text(encoding="utf-8")
    assert "configured and authorized" in text
    assert "same-candidate" not in text
    assert "release revision" not in text
    assert "Do not" in text and "help output" in text


def test_account_snapshot_binds_four_fixed_local_commands_without_unowned_tables() -> None:
    page = _ROOT / "account-snapshot.md"
    blocks = fenced_blocks(page)

    assert tuple(block.body.strip() for block in blocks) == tuple(
        _COMMANDS[identifier] for identifier in ("projects-account", "health-account", "gmail-account", "gmail-decay")
    )
    assert all(block.language == "console" for block in blocks)
    assert markdown_tables(page) == ()


def test_account_snapshot_discloses_decay_age_cutoff() -> None:
    text = (_ROOT / "account-snapshot.md").read_text(encoding="utf-8")

    assert "`--max-age-days 365`" in text
    assert "older stale contacts" in text


def test_account_snapshot_discloses_independent_cache_bounds() -> None:
    text = (_ROOT / "account-snapshot.md").read_text(encoding="utf-8")

    assert "up to 10" in text
    assert "not the total matching threads" in text
    assert "contacts found by this scan" in text
    assert "both flags can be true" in text
    assert "complete scan found" not in text


@pytest.mark.parametrize(
    ("original", "replacement", "diagnostic"),
    [
        ("|---|---|---|---|", "|bad|---|---|---|", "Malformed Markdown table separator"),
        (
            "| transition history | [lookback] | unavailable | [reason] |",
            "| transition history | [lookback] | verified | [reason] |",
            "source status",
        ),
        ("### Asks / escalations", "### Hidden obligations", "required sections"),
    ],
)
def test_report_template_mutations_fail_closed(
    tmp_path: Path, original: str, replacement: str, diagnostic: str
) -> None:
    source = _ROOT / "one-on-one.md"
    content = source.read_text(encoding="utf-8")
    assert original in content
    path = tmp_path / source.name
    path.write_text(content.replace(original, replacement), encoding="utf-8")

    with pytest.raises((AssertionError, ValueError), match=diagnostic):
        _validate_report_template(path)


@pytest.mark.parametrize("page", _PAGE_COMMANDS)
def test_meeting_page_fences_bind_exactly_to_fixed_argv_and_one_template(page: str) -> None:
    blocks = fenced_blocks(_ROOT / page)
    commands = [
        line
        for block in blocks
        if block.language in {"bash", "console"}
        for line in block.body.splitlines()
        if line != _PENDING_COMMAND
    ]
    templates = [block for block in blocks if block.language == "markdown"]

    assert commands == [_COMMANDS[identifier] for identifier in _PAGE_COMMANDS[page]]
    assert len(templates) == 1
    assert len(blocks) == (5 if page == "stakeholder-map.md" else 2)
    headers = [line for line in templates[0].body.splitlines() if line in _TABLE_HEADERS[page]]
    assert tuple(headers) == _TABLE_HEADERS[page]
    table_rows = [line for line in templates[0].body.splitlines() if line.startswith("|")]
    assert len(table_rows) == {"account-pulse.md": 4, "stakeholder-map.md": 7, "one-on-one.md": 4}[page]
    result = _validate_report_template(_ROOT / page)
    assert result is None


@pytest.mark.parametrize("identifier", [identifier for identifier in _COMMANDS if identifier != "opportunity-help"])
def test_documented_meeting_commands_execute_read_only_on_fictional_data(
    documented_workspace: Path, identifier: str
) -> None:
    write_documented_pursuit_data(documented_workspace)
    write_second_documented_account(documented_workspace)
    prepare_documented_gmail_cache(documented_workspace)
    before = snapshot_workflow(documented_workspace.parent)

    with patch("fieldkit.gmail.decay_domain.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 10, 15, tzinfo=UTC)
        result = invoke_workflow(_argv(identifier))

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, (dict, list))
    assert payload
    if identifier in {"gmail-account", "gmail-blindspots"}:
        assert isinstance(payload, dict)
        assert payload["count"] == 1
        assert payload["filters"]["limit"] == 10
    if identifier == "forecast":
        assert isinstance(payload, dict)
        assert {"closed_won", "commit", "weighted", "best_case"} <= payload.keys()
        assert {deal["relative_path"] for deal in payload["deals"]} == {
            "acme-corp/pursuits/platform.md",
            "beta-corp/pursuits/platform.md",
        }
        assert payload["best_case"] == 300000
    if identifier in {"contact", "contact-basic"}:
        assert isinstance(payload, dict)
        assert payload["email"] == "alex@acme-corp.example.com"
        assert payload["type"] == "resolved"
    if identifier == "contact":
        assert isinstance(payload, dict)
        assert {item["pursuit_file"] for item in payload["affiliations"]} == {
            "acme-corp/pursuits/platform.md",
            "beta-corp/pursuits/platform.md",
        }
    if identifier in {"projects-account", "projects"}:
        assert isinstance(payload, list)
        accounts = {row["name"].split("/")[0] for row in payload}
        assert accounts == ({"acme-corp"} if identifier == "projects-account" else {"acme-corp", "beta-corp"})
    if identifier in {"health-account", "health"}:
        assert isinstance(payload, list)
        accounts = {row["relative_path"].split("/")[0] for row in payload}
        assert accounts == ({"acme-corp"} if identifier == "health-account" else {"acme-corp", "beta-corp"})
        assert all(row["qualification_status"] == "unavailable" for row in payload)
    if identifier == "crm-candidates":
        assert isinstance(payload, dict)
        assert payload["comparison"] == {"crm": "not_performed", "result_kind": "review_candidates"}
        assert payload["filters"]["limit"] == 10
        assert payload["count"] == 1
        assert payload["items"][0]["email"] == "alex@acme-corp.example.com"
        assert payload["scan_truncated"] is False
        assert payload["scanned_rows"] == 1
    if identifier == "gmail-decay":
        assert isinstance(payload, dict)
        assert payload["account"] == "acme-corp"
        assert payload["as_of"] == "2026-10-15"
        assert payload["count"] == 1
        assert payload["filters"]["limit"] == 10
        assert payload["truncated"] is False
        assert payload["scan_truncated"] is False
        assert payload["scanned_rows"] == 4
        assert len(payload["contacts"]) == 1
        contact = payload["contacts"][0]
        assert contact["email"] == "alex@acme-corp.example.com"
        assert contact["messages"] == 3
        assert contact["threads"] == 1
        assert contact["days_any"] == 121
        assert contact["signal"] == "COLD"
        assert contact["warmth"] == "cold"
    assert snapshot_workflow(documented_workspace.parent) == before


def test_local_opportunity_help_is_safe_but_does_not_prove_the_credentialed_read(
    documented_workspace: Path,
) -> None:
    page = _ROOT / "stakeholder-map.md"
    before = snapshot_workflow(documented_workspace.parent)

    result = invoke_workflow(_argv("opportunity-help"))

    assert result.exit_code == 0
    assert "--no-write" in result.stdout
    assert snapshot_workflow(documented_workspace.parent) == before
    pending = [block.body.strip() for block in fenced_blocks(page) if block.body.strip() == _PENDING_COMMAND]
    assert pending == [_PENDING_COMMAND]
    assert "configured and authorized" in page.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ["SKILL.md", "ops/account-snapshot.md", "ops/stakeholder-map.md"])
def test_meeting_usage_instructions_are_evergreen_not_release_status(page: str) -> None:
    text = (_ROOT.parent / page).read_text(encoding="utf-8")

    assert text
    assert "candidate revision" not in text
    assert "final candidate" not in text
    assert "schema-valid" not in text
    assert "Verification owner:" not in text


@pytest.mark.parametrize(
    ("identifier", "expected_exit"),
    [
        ("gmail-account", 1),
        ("gmail-decay", 1),
        ("gmail-blindspots", 1),
        ("crm-candidates", 1),
        ("contact", 1),
        ("contact-basic", 1),
        ("forecast", 3),
        ("health", 3),
        ("projects", 3),
        ("projects-account", 3),
        ("health-account", 3),
    ],
)
def test_missing_meeting_sources_do_not_become_successful_empty_results(
    documented_workspace: Path, identifier: str, expected_exit: int
) -> None:
    before = snapshot_workflow(documented_workspace.parent)

    result = invoke_workflow(_argv(identifier))

    assert result.exit_code == expected_exit
    assert not result.stdout.strip().startswith(("{", "["))
    assert snapshot_workflow(documented_workspace.parent) == before


def test_ready_cache_unknown_contact_is_not_a_verified_negative_finding(documented_workspace: Path) -> None:
    prepare_documented_gmail_cache(documented_workspace)
    before = snapshot_workflow(documented_workspace.parent)

    result = invoke_workflow(["contact", "find", "unknown@example.com", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["type"] == "not_found"
    assert "unknown contact" in (_ROOT / "stakeholder-map.md").read_text(encoding="utf-8")
    assert snapshot_workflow(documented_workspace.parent) == before


def test_ready_cache_valid_empty_query_is_distinct_from_unavailable_source(documented_workspace: Path) -> None:
    prepare_documented_gmail_cache(documented_workspace)
    before = snapshot_workflow(documented_workspace.parent)

    result = invoke_workflow(
        ["gmail", "query", "account", "acme-corp", "--since", "2099-01-01", "--limit", "10", "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["items"] == []
    assert snapshot_workflow(documented_workspace.parent) == before


@pytest.mark.parametrize("page", _PAGE_COMMANDS)
def test_meeting_guides_preserve_missing_evidence_and_separate_save_approval(page: str) -> None:
    content = (_ROOT / page).read_text(encoding="utf-8")

    assert "unavailable" in content
    assert "pending" in content
    assert "Present the draft for review." in content
    assert "overwrite approval" in content
    assert "Do not" in content
