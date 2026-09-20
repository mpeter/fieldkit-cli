"""Unit tests for fieldkit.commands.contact.enrich_cmd — the enrich CLI adapter.

Pins the three mutually-exclusive branches (--discover, --apply-web, default
pipeline), the JSON/human output split, the early-return guards for
"nothing to do" states, and account/json flag forwarding to the domain layer.
All domain calls (discover, apply_web, enrich_records) are mocked so no real
filesystem or network I/O occurs.
"""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.contact.enrich_cmd import cli
from fieldkit.contact.enrich import ApplyWebResult, DiscoverResult, EnrichRecordsResult

pytestmark = pytest.mark.unit

_MOD = "fieldkit.commands.contact.enrich_cmd"


def _discover_result(
    *,
    total: int = 5,
    with_email: int = 3,
    with_linkedin: int = 2,
    new_since: int | None = None,
    by_source: dict | None = None,
) -> DiscoverResult:
    return DiscoverResult(
        total=total,
        with_email=with_email,
        with_linkedin=with_linkedin,
        new_since_last_run=new_since,
        by_source=by_source if by_source is not None else {"gmail": 1},
    )


def _apply_web_result(
    *, updated_fields: int = 7, web_results_applied: int = 3, total_raw_contacts: int = 10
) -> ApplyWebResult:
    return ApplyWebResult(
        updated_fields=updated_fields, web_results_applied=web_results_applied, total_raw_contacts=total_raw_contacts
    )


def _enrich_result(
    *, total_enriched: int = 8, total_failed: int = 1, migrated_legacy_files: int = 0, total_raw_contacts: int = 9
) -> EnrichRecordsResult:
    return EnrichRecordsResult(
        total_enriched=total_enriched,
        total_failed=total_failed,
        migrated_legacy_files=migrated_legacy_files,
        total_raw_contacts=total_raw_contacts,
    )


# ---------------------------------------------------------------------------
# --discover and --apply-web are mutually exclusive
# ---------------------------------------------------------------------------


def test_discover_and_apply_web_together_exits_data_error() -> None:
    runner = CliRunner()
    with (
        patch(f"{_MOD}.discover") as mock_discover,
        patch(f"{_MOD}.apply_web") as mock_apply_web,
        patch(f"{_MOD}.enrich_records") as mock_enrich,
    ):
        result = runner.invoke(cli, ["--discover", "--apply-web"])

    assert result.exit_code == 3
    assert "mutually exclusive" in (result.output + str(result.exception))
    mock_discover.assert_not_called()
    mock_apply_web.assert_not_called()
    mock_enrich.assert_not_called()


# ---------------------------------------------------------------------------
# --discover
# ---------------------------------------------------------------------------


def test_discover_json_output_is_exact_json() -> None:
    runner = CliRunner()
    sample = _discover_result()
    with patch(f"{_MOD}.discover", return_value=sample):
        result = runner.invoke(cli, ["--discover", "--json"])

    assert result.exit_code == 0
    assert result.output.strip() == json.dumps(
        {
            "total": 5,
            "with_email": 3,
            "with_linkedin": 2,
            "new_since_last_run": None,
            "by_source": {"gmail": 1},
            "by_account": {},
        },
        indent=2,
    )


def test_discover_human_output_shows_counts() -> None:
    runner = CliRunner()
    sample = _discover_result(total=5, with_email=3, with_linkedin=2)
    with patch(f"{_MOD}.discover", return_value=sample):
        result = runner.invoke(cli, ["--discover"])

    assert result.exit_code == 0
    assert "Discovered 5 contacts (email=3, linkedin=2)" in result.output


def test_discover_human_output_with_new_since_last_run_shown() -> None:
    runner = CliRunner()
    sample = _discover_result(new_since=4)
    with patch(f"{_MOD}.discover", return_value=sample):
        result = runner.invoke(cli, ["--discover"])

    assert "New since last run: 4" in result.output


def test_discover_human_output_no_new_since_last_run_hidden() -> None:
    runner = CliRunner()
    sample = _discover_result(new_since=None)
    with patch(f"{_MOD}.discover", return_value=sample):
        result = runner.invoke(cli, ["--discover"])

    assert "New since last run" not in result.output


def test_discover_human_output_zero_new_since_last_run_hidden() -> None:
    runner = CliRunner()
    sample = _discover_result(new_since=0)
    with patch(f"{_MOD}.discover", return_value=sample):
        result = runner.invoke(cli, ["--discover"])

    assert "New since last run" not in result.output


def test_discover_by_source_sorted_alphabetically() -> None:
    runner = CliRunner()
    sample = _discover_result(by_source={"web": 1, "gmail": 2})
    with patch(f"{_MOD}.discover", return_value=sample):
        result = runner.invoke(cli, ["--discover"])

    assert "By source: gmail=2, web=1" in result.output


def test_discover_returns_before_apply_web_or_enrich() -> None:
    runner = CliRunner()
    with (
        patch(f"{_MOD}.discover", return_value=_discover_result()),
        patch(f"{_MOD}.apply_web") as mock_apply_web,
        patch(f"{_MOD}.enrich_records") as mock_enrich,
    ):
        result = runner.invoke(cli, ["--discover"])

    assert result.exit_code == 0
    mock_apply_web.assert_not_called()
    mock_enrich.assert_not_called()


def test_discover_account_flag_forwarded() -> None:
    runner = CliRunner()
    with patch(f"{_MOD}.discover", return_value=_discover_result()) as mock_discover:
        runner.invoke(cli, ["--discover", "--account", "acme-corp"])

    mock_discover.assert_called_once_with(account="acme-corp")


# ---------------------------------------------------------------------------
# --apply-web
# ---------------------------------------------------------------------------


def test_apply_web_json_output_is_exact_json() -> None:
    runner = CliRunner()
    sample = _apply_web_result()
    with patch(f"{_MOD}.apply_web", return_value=sample):
        result = runner.invoke(cli, ["--apply-web", "--json"])

    assert result.exit_code == 0
    assert result.output.strip() == json.dumps(
        {"updated_fields": 7, "web_results_applied": 3, "total_raw_contacts": 10}, indent=2
    )


def test_apply_web_zero_results_message_and_no_applied_line() -> None:
    runner = CliRunner()
    sample = _apply_web_result(web_results_applied=0, updated_fields=0, total_raw_contacts=10)
    with patch(f"{_MOD}.apply_web", return_value=sample):
        result = runner.invoke(cli, ["--apply-web"])

    assert result.exit_code == 0
    assert "No web search results found" in result.output
    assert "Applied" not in result.output


def test_apply_web_nonzero_results_message() -> None:
    runner = CliRunner()
    sample = _apply_web_result(web_results_applied=3, updated_fields=7, total_raw_contacts=10)
    with patch(f"{_MOD}.apply_web", return_value=sample):
        result = runner.invoke(cli, ["--apply-web"])

    assert "Applied 3 web result(s), updated 7 field(s) across 10 contacts" in result.output


def test_apply_web_returns_before_enrich() -> None:
    runner = CliRunner()
    with (
        patch(f"{_MOD}.apply_web", return_value=_apply_web_result(web_results_applied=3)),
        patch(f"{_MOD}.enrich_records") as mock_enrich,
    ):
        result = runner.invoke(cli, ["--apply-web"])

    assert result.exit_code == 0
    mock_enrich.assert_not_called()


def test_apply_web_zero_results_returns_before_enrich() -> None:
    runner = CliRunner()
    with (
        patch(f"{_MOD}.apply_web", return_value=_apply_web_result(web_results_applied=0)),
        patch(f"{_MOD}.enrich_records") as mock_enrich,
    ):
        runner.invoke(cli, ["--apply-web"])

    mock_enrich.assert_not_called()


def test_apply_web_account_flag_forwarded() -> None:
    runner = CliRunner()
    with patch(f"{_MOD}.apply_web", return_value=_apply_web_result()) as mock_apply_web:
        runner.invoke(cli, ["--apply-web", "--account", "acme-corp"])

    mock_apply_web.assert_called_once_with(account="acme-corp")


# ---------------------------------------------------------------------------
# default pipeline (no flags) — enrich_records
# ---------------------------------------------------------------------------


def test_default_path_does_not_call_discover_or_apply_web() -> None:
    runner = CliRunner()
    with (
        patch(f"{_MOD}.discover") as mock_discover,
        patch(f"{_MOD}.apply_web") as mock_apply_web,
        patch(f"{_MOD}.enrich_records", return_value=_enrich_result()),
    ):
        result = runner.invoke(cli, [])

    assert result.exit_code == 0
    mock_discover.assert_not_called()
    mock_apply_web.assert_not_called()


def test_enrich_records_json_output_is_exact_json() -> None:
    runner = CliRunner()
    sample = _enrich_result(total_enriched=8, total_failed=1, migrated_legacy_files=0, total_raw_contacts=9)
    with patch(f"{_MOD}.enrich_records", return_value=sample):
        result = runner.invoke(cli, ["--json"])

    assert result.exit_code == 0
    assert result.output.strip() == json.dumps(
        {"total_enriched": 8, "total_failed": 1, "migrated_legacy_files": 0, "total_raw_contacts": 9}, indent=2
    )


def test_enrich_records_no_contacts_message_and_returns() -> None:
    runner = CliRunner()
    sample = _enrich_result(total_raw_contacts=0, migrated_legacy_files=0, total_enriched=0, total_failed=0)
    with patch(f"{_MOD}.enrich_records", return_value=sample):
        result = runner.invoke(cli, [])

    assert result.exit_code == 0
    assert "No contacts to enrich. Run 'fieldkit contact enrich --discover' first." in result.output
    assert "Enriched" not in result.output
    assert "Migrated" not in result.output


def test_enrich_records_migrated_legacy_files_shown_when_nonzero() -> None:
    runner = CliRunner()
    sample = _enrich_result(migrated_legacy_files=2, total_raw_contacts=9)
    with patch(f"{_MOD}.enrich_records", return_value=sample):
        result = runner.invoke(cli, [])

    assert "Migrated 2 contact file(s) from legacy memory location." in result.output


def test_enrich_records_migrated_legacy_files_hidden_when_zero() -> None:
    runner = CliRunner()
    sample = _enrich_result(migrated_legacy_files=0, total_raw_contacts=9)
    with patch(f"{_MOD}.enrich_records", return_value=sample):
        result = runner.invoke(cli, [])

    assert "Migrated" not in result.output


def test_enrich_records_final_summary_line() -> None:
    runner = CliRunner()
    sample = _enrich_result(total_enriched=8, total_failed=1, total_raw_contacts=9)
    with patch(f"{_MOD}.enrich_records", return_value=sample):
        result = runner.invoke(cli, [])

    assert "Enriched 8 contact(s); 1 failed." in result.output


def test_enrich_records_account_flag_forwarded() -> None:
    runner = CliRunner()
    with patch(f"{_MOD}.enrich_records", return_value=_enrich_result()) as mock_enrich:
        runner.invoke(cli, ["--account", "acme-corp"])

    mock_enrich.assert_called_once_with(account="acme-corp")
