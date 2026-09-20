"""Tests for fieldkit.commands.sf.opportunity — single-opportunity sync."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.opportunity import (
    _build_write_payload,
    _fetch_deal_splits,
    _fmt_currency,
    _print_summary,
    cli,
    run_opportunity,
)
from tests.conftest import OPP_GPAY

pytestmark = pytest.mark.unit

# ── _fmt_currency ─────────────────────────────────────────────────────────────


# ── TestFmtCurrency (flattened) ─────────────────────────────────────────────


def test_fmt_currency_none_returns_empty_string() -> None:
    # historic regression: None (SF null) must produce "" not None so callers always get str
    assert _fmt_currency(None) == ""


def test_fmt_currency_zero_returns_zero_string() -> None:
    assert _fmt_currency(0.0) == "$0"


def test_fmt_currency_positive_value_formatted() -> None:
    assert _fmt_currency(828495.0) == "$828,495"


def test_fmt_currency_large_value() -> None:
    assert _fmt_currency(1154492.0) == "$1,154,492"


def test_fmt_currency_return_type_is_always_str() -> None:
    # Regression guard: return value must be str in all branches
    assert isinstance(_fmt_currency(None), str)
    assert isinstance(_fmt_currency(0.0), str)
    assert isinstance(_fmt_currency(50000.0), str)


# ── _build_write_payload ──────────────────────────────────────────────────────


# ── TestBuildWritePayload (flattened) ───────────────────────────────────────


def _build_write_payload_sample_rec() -> dict:
    return {
        "Id": "006TESTID",
        "Name": "Test Opportunity",
        "StageName": "Propose",
        "CloseDate": "2026-09-30",
        "IsClosed": False,
        "Owner": {"Name": "Jane Doe", "Email": "jdoe@internal.example.com"},  # pii-guard: ignore
        "ACV_Opportunity_USD__c": 100000.0,
        "ARR_Opportunity_USD__c": 0.0,
        "Consulting_Total_USD__c": 90000.0,
        "Training_Total_USD__c": 10000.0,
        "Application_Services_Total_USD__c": 0.0,
        "Services_Total_USD__c": 100000.0,
        "Next_Steps__c": "Follow up with sponsor",
        "Customer_Pain_Point__c": "Manual process takes 3 days",
        "Identify_Pain_Long__c": "Automation gap",
        "Decision_Criteria__c": None,
        "Main_Competitor__c": "IBM",
        "Closed_Lost_Reason__c": None,
        "Account": {"Id": "001ACCTID", "Name": "Acme Corp", "Industry": "Technology"},
    }


def test_build_write_payload_status_is_ok() -> None:
    payload = _build_write_payload(_build_write_payload_sample_rec())
    assert payload["status"] == "ok"


def test_build_write_payload_core_fields_mapped() -> None:
    payload = _build_write_payload(_build_write_payload_sample_rec())
    assert payload["opportunity_id"] == "006TESTID"
    assert payload["stage"] == "Propose"
    assert payload["close_date"] == "2026-09-30"
    assert payload["owner"] == "Jane Doe"


def test_build_write_payload_services_splits() -> None:
    payload = _build_write_payload(_build_write_payload_sample_rec())
    assert payload["consulting_acv"] == "$90,000"  # implementation change: formatted by _fmt_currency
    assert payload["training_acv"] == "$10,000"  # implementation change: formatted by _fmt_currency
    assert payload["application_services_acv"] == "$0"  # implementation change: _fmt_currency(0.0) returns "$0"
    assert payload["services_total"] == 100000.0


def test_build_write_payload_currency_formatted() -> None:
    payload = _build_write_payload(_build_write_payload_sample_rec())
    assert payload["acv"] == "$100,000"
    assert payload["arr"] == "$0"


def test_build_write_payload_meddpicc_signals() -> None:
    payload = _build_write_payload(_build_write_payload_sample_rec())
    # Identify_Pain_Long__c takes priority over Customer_Pain_Point__c
    assert payload["sf_identify_pain"] == "Automation gap"
    assert payload["sf_decision_criteria"] is None
    assert payload["sf_main_competitor"] == "IBM"


def test_build_write_payload_identify_pain_fallback() -> None:
    rec = _build_write_payload_sample_rec()
    rec["Identify_Pain_Long__c"] = None
    payload = _build_write_payload(rec)
    assert payload["sf_identify_pain"] == "Manual process takes 3 days"


def test_build_write_payload_null_currency_fields_produce_empty_string() -> None:
    """historic regression: When SF returns null for ACV/ARR, payload must hold "" not None."""
    rec = _build_write_payload_sample_rec()
    rec["ACV_Opportunity_USD__c"] = None
    rec["ARR_Opportunity_USD__c"] = None
    payload = _build_write_payload(rec)
    assert payload["acv"] == ""
    assert payload["arr"] == ""
    # Must be str, not None — frontmatter writers must never see None here
    assert isinstance(payload["acv"], str)
    assert isinstance(payload["arr"], str)


def test_build_write_payload_no_account_id_in_payload() -> None:
    """account_id must not appear — it triggers write-account mode in frontmatter.py."""
    payload = _build_write_payload(_build_write_payload_sample_rec())
    assert "account_id" not in payload


def test_build_write_payload_pulled_at_is_set() -> None:
    payload = _build_write_payload(_build_write_payload_sample_rec())
    assert "pulled_at" in payload
    assert "T" in payload["pulled_at"]  # ISO datetime


def test_build_write_payload_includes_opportunity_number_when_present() -> None:
    """implementation change: sf_opportunity_number must appear in the write payload when OpportunityNumber__c is set."""
    rec = _build_write_payload_sample_rec()
    rec["OpportunityNumber__c"] = "71721820"
    payload = _build_write_payload(rec)
    assert payload["opportunity_number"] == "71721820"


def test_build_write_payload_opportunity_number_none_when_absent() -> None:
    """implementation change: opportunity_number must be None when OpportunityNumber__c is not set."""
    rec = _build_write_payload_sample_rec()
    rec["OpportunityNumber__c"] = None
    payload = _build_write_payload(rec)
    assert payload["opportunity_number"] is None


def test_build_write_payload_opportunity_number_omitted_when_field_not_fetched() -> None:
    """implementation change: opportunity_number must be None when the field is missing from the sObject."""
    rec = _build_write_payload_sample_rec()
    # Simulate a record that was fetched without OpportunityNumber__c in the field list
    rec.pop("OpportunityNumber__c", None)
    payload = _build_write_payload(rec)
    assert payload["opportunity_number"] is None


# ── CLI ───────────────────────────────────────────────────────────────────────


# ── TestOpportunityCli (flattened) ──────────────────────────────────────────


def test_run_opportunity_no_write_flag_skips_write(tmp_path: Path) -> None:
    """--no-write prints summary without calling do_write_opp."""
    sample_rec = {
        "Id": "006ABC",
        "Name": "Test Opp",
        "StageName": "Propose",
        "CloseDate": "2026-12-31",
        "IsClosed": False,
        "Owner": {"Name": "Test Owner", "Email": "t@r.example.com"},
        "ACV_Opportunity_USD__c": 100000.0,
        "ARR_Opportunity_USD__c": 0.0,
        "Consulting_Total_USD__c": 80000.0,
        "Training_Total_USD__c": 20000.0,
        "Application_Services_Total_USD__c": 0.0,
        "Services_Total_USD__c": 100000.0,
        "Next_Steps__c": "Call next week",
        "Customer_Pain_Point__c": None,
        "Identify_Pain_Long__c": "Slow deploys",
        "Decision_Criteria__c": None,
        "Main_Competitor__c": None,
        "Closed_Lost_Reason__c": None,
        "Account": {"Id": "001A", "Name": "Acme", "Industry": "Tech"},
    }

    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity", return_value=sample_rec),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
        # Both collaborators open their own SFDirectClient; unpatched they reach
        # the live API. See the conftest _block_outbound_network guard.
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        patch("fieldkit.commands.sf.opportunity._fetch_contract_type", return_value="standard"),
        patch("fieldkit.commands.sf.sync.do_write_opp") as mock_write,
    ):
        result = runner.invoke(cli, [OPP_GPAY, "--no-write"])

    assert result.exit_code == 0
    mock_write.assert_not_called()
    assert "Test Opp" in result.output


def test_run_opportunity_output_shows_services_splits(tmp_path: Path) -> None:
    sample_rec = {
        "Id": "006ABC",
        "Name": "Big Services Deal",
        "StageName": "Negotiate",
        "CloseDate": "2026-06-30",
        "IsClosed": False,
        "Owner": {"Name": "Jane", "Email": "j@r.example.com"},
        "ACV_Opportunity_USD__c": 500000.0,
        "ARR_Opportunity_USD__c": 0.0,
        "Consulting_Total_USD__c": 400000.0,
        "Training_Total_USD__c": 100000.0,
        "Application_Services_Total_USD__c": 0.0,
        "Services_Total_USD__c": 500000.0,
        "Next_Steps__c": None,
        "Customer_Pain_Point__c": None,
        "Identify_Pain_Long__c": None,
        "Decision_Criteria__c": None,
        "Main_Competitor__c": None,
        "Closed_Lost_Reason__c": None,
        "Account": {"Id": "001B", "Name": "Big Corp", "Industry": "Finance"},
    }

    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity", return_value=sample_rec),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
        # Both collaborators open their own SFDirectClient; unpatched they reach
        # the live API. See the conftest _block_outbound_network guard.
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        patch("fieldkit.commands.sf.opportunity._fetch_contract_type", return_value="standard"),
    ):
        result = runner.invoke(cli, [OPP_GPAY, "--no-write"])

    assert result.exit_code == 0
    assert "$400,000" in result.output  # consulting
    assert "$100,000" in result.output  # training
    assert "MEDDPICC" in result.output


# ── _fetch_deal_splits ────────────────────────────────────────────────────────


# ── TestFetchDealSplits (flattened) ─────────────────────────────────────────


def _fetch_deal_splits_base_patches() -> tuple:
    """Common patches: valid sid and base_url."""
    return (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value="https://test.my.salesforce.com"),
    )


def test_fetch_deal_splits_returns_splits_from_client() -> None:
    splits_data = [
        {"offering_group": "Ansible Automation Platform", "services_pct": 50.0},
        {"offering_group": "Example Enterprise Linux", "services_pct": 50.0},
    ]
    p1, p2 = _fetch_deal_splits_base_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        mock_client = mock_cls.return_value.__enter__.return_value
        mock_client.fetch_deal_splits.return_value = splits_data
        result = _fetch_deal_splits("006ABC")
    assert result == splits_data


def test_fetch_deal_splits_returns_empty_on_no_sid() -> None:
    with patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value=None):
        assert _fetch_deal_splits("006ABC") == []


def test_fetch_deal_splits_returns_empty_on_sfapierror() -> None:
    from fieldkit.sf.client import SFAPIError

    p1, p2 = _fetch_deal_splits_base_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        mock_client = mock_cls.return_value.__enter__.return_value
        mock_client.fetch_deal_splits.side_effect = SFAPIError("timeout")
        result = _fetch_deal_splits("006ABC")
    assert result == []


def test_fetch_deal_splits_returns_empty_on_sfautherror() -> None:
    from fieldkit.sf.client import SFAuthError

    p1, p2 = _fetch_deal_splits_base_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        mock_client = mock_cls.return_value.__enter__.return_value
        mock_client.fetch_deal_splits.side_effect = SFAuthError("expired")
        result = _fetch_deal_splits("006ABC")
    assert result == []


# ── _print_summary splits section ────────────────────────────────────────────


# ── TestPrintSummarySplits (flattened) ──────────────────────────────────────


def _print_summary_minimal_rec() -> dict:
    return {
        "Id": "006X",
        "Name": "Test Opp",
        "StageName": "Propose",
        "CloseDate": "2026-12-31",
        "IsClosed": False,
        "Owner": {"Name": "Jane", "Email": "j@r.example.com"},
        "ACV_Opportunity_USD__c": None,
        "ARR_Opportunity_USD__c": None,
        "Consulting_Total_USD__c": None,
        "Training_Total_USD__c": None,
        "Application_Services_Total_USD__c": None,
        "Services_Total_USD__c": None,
        "Next_Steps__c": None,
        "Customer_Pain_Point__c": None,
        "Identify_Pain_Long__c": None,
        "Decision_Criteria__c": None,
        "Main_Competitor__c": None,
        "Closed_Lost_Reason__c": None,
        "Account": {"Id": "001A", "Name": "Acme", "Industry": "Tech"},
    }


def test_print_summary_splits_section_shown_when_present(capsys: pytest.CaptureFixture) -> None:
    splits = [
        {"offering_group": "Ansible Automation Platform", "services_pct": 50.0},
        {"offering_group": "Example Enterprise Linux", "services_pct": 50.0},
    ]
    _print_summary(_print_summary_minimal_rec(), None, splits=splits)
    out = capsys.readouterr().out
    assert "Deal Splits" in out
    assert "Ansible Automation Platform" in out
    assert "50%" in out


def test_print_summary_splits_section_omitted_when_empty(capsys: pytest.CaptureFixture) -> None:
    _print_summary(_print_summary_minimal_rec(), None, splits=[])
    out = capsys.readouterr().out
    assert "Deal Splits" not in out


def test_print_summary_splits_section_omitted_when_none(capsys: pytest.CaptureFixture) -> None:
    _print_summary(_print_summary_minimal_rec(), None, splits=None)
    out = capsys.readouterr().out
    assert "Deal Splits" not in out


# ── CLI integration: splits ───────────────────────────────────────────────────


# ── TestOpportunityCliSplits (flattened) ────────────────────────────────────


def _run_opportunity_sample_rec() -> dict:
    return {
        "Id": "006ABC",
        "Name": "Test Opp",
        "StageName": "Propose",
        "CloseDate": "2026-12-31",
        "IsClosed": False,
        "Owner": {"Name": "Test Owner", "Email": "t@r.example.com"},
        "ACV_Opportunity_USD__c": 100000.0,
        "ARR_Opportunity_USD__c": 0.0,
        "Consulting_Total_USD__c": 80000.0,
        "Training_Total_USD__c": 20000.0,
        "Application_Services_Total_USD__c": 0.0,
        "Services_Total_USD__c": 100000.0,
        "Next_Steps__c": None,
        "Customer_Pain_Point__c": None,
        "Identify_Pain_Long__c": None,
        "Decision_Criteria__c": None,
        "Main_Competitor__c": None,
        "Closed_Lost_Reason__c": None,
        "Account": {"Id": "001A", "Name": "Acme", "Industry": "Tech"},
    }


def test_run_opportunity_cli_shows_splits() -> None:
    splits = [
        {"offering_group": "Ansible Automation Platform", "services_pct": 60.0},
        {"offering_group": "Example Enterprise Linux", "services_pct": 40.0},
    ]
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity", return_value=_run_opportunity_sample_rec()),
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=splits),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
    ):
        result = runner.invoke(cli, [OPP_GPAY, "--no-write"])

    assert result.exit_code == 0
    assert "Deal Splits" in result.output
    assert "Ansible Automation Platform" in result.output
    assert "60%" in result.output
    assert "40%" in result.output


def test_run_opportunity_cli_omits_splits_when_empty() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity", return_value=_run_opportunity_sample_rec()),
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
    ):
        result = runner.invoke(cli, [OPP_GPAY, "--no-write"])

    assert result.exit_code == 0
    assert "Deal Splits" not in result.output


# ── Placeholder guard ─────────────────────────────────────────────────────────


# ── TestRunOpportunityPlaceholderGuard (flattened) ──────────────────────────


@pytest.mark.parametrize("placeholder", ["TBD", "placeholder", "todo", "N/A", "xxx"])
def test_run_opportunity_run_opportunity_placeholder_warns(placeholder: str, caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity") as mock_fetch,
        caplog.at_level("WARNING"),
    ):
        result = run_opportunity(placeholder, None, write=False)

    assert result == 0
    mock_fetch.assert_not_called()
    assert "placeholder" in caplog.text.lower() or "invalid" in caplog.text.lower()


# ── TestRunOpportunityFormatGuard (flattened) ───────────────────────────────


@pytest.mark.parametrize("bad_id", ["006ABC", "short", "123", "ABC123456789012345678"])
def test_run_opportunity_run_opportunity_short_id_warns(bad_id: str, caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity") as mock_fetch,
        caplog.at_level("WARNING"),
    ):
        result = run_opportunity(bad_id, None, write=False)

    assert result == 3, f"Expected exit code 3 for malformed ID {bad_id!r}, got {result}"
    mock_fetch.assert_not_called()
    assert "valid salesforce id" in caplog.text.lower() or "not a valid" in caplog.text.lower()


def test_run_opportunity_run_opportunity_valid_15char_proceeds() -> None:
    """A valid 15-char ID passes the format guard and proceeds to API call."""
    valid_id = "006ABC123456789"
    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity") as mock_fetch,
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
    ):
        mock_fetch.return_value = {"status": "error"}
        run_opportunity(valid_id, None, write=False)

    mock_fetch.assert_called_once_with(valid_id)


def test_run_opportunity_run_opportunity_valid_18char_proceeds() -> None:
    """A valid 18-char ID passes the format guard and proceeds to API call."""
    valid_id = "006GPAY00000000AAA"
    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity") as mock_fetch,
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
    ):
        mock_fetch.return_value = {"status": "error"}
        run_opportunity(valid_id, None, write=False)

    mock_fetch.assert_called_once_with(valid_id)


# ── historic regression: exit 3 with clear message when opp has no matching pursuit file ──


# ── TestBUG031UntrackedOppExitCode (flattened) ──────────────────────────────

_BUG031_UNTRACKED_OPP_EXIT_CODE__VALID_OPP_ID = "006GPAY00000000AAA"


def _bug031_untracked_opp_exit_code_sample_rec() -> dict:
    return {
        "Id": _BUG031_UNTRACKED_OPP_EXIT_CODE__VALID_OPP_ID,
        "Name": "Untracked Opportunity",
        "StageName": "Qualify",
        "CloseDate": "2026-12-31",
        "IsClosed": False,
        "Owner": {"Name": "Jane Doe", "Email": "jdoe@internal.example.com"},  # pii-guard: ignore
        "ACV_Opportunity_USD__c": 0.0,
        "ARR_Opportunity_USD__c": 0.0,
        "Consulting_Total_USD__c": 0.0,
        "Training_Total_USD__c": 0.0,
        "Application_Services_Total_USD__c": 0.0,
        "Services_Total_USD__c": 0.0,
        "Next_Steps__c": "",
        "Next_Steps_Date__c": None,
        "Identify_Pain__c": None,
        "Decision_Criteria__c": None,
        "Main_Competitor__c": None,
        "Probability": 20,
    }


def test_bug031_untracked_opp_exit_code_untracked_opp_returns_exit_3(capsys: pytest.CaptureFixture) -> None:
    """When no pursuit file is found, run_opportunity must return 3."""
    with (
        patch(
            "fieldkit.commands.sf.opportunity._fetch_opportunity",
            return_value=_bug031_untracked_opp_exit_code_sample_rec(),
        ),
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
    ):
        result = run_opportunity(_BUG031_UNTRACKED_OPP_EXIT_CODE__VALID_OPP_ID, None, write=True)

    assert result == 3, f"Expected exit code 3 for untracked opp, got {result}"


def test_bug031_untracked_opp_exit_code_untracked_opp_prints_clear_error_message(capsys: pytest.CaptureFixture) -> None:
    """The error message must clearly indicate the opp is untracked."""
    with (
        patch(
            "fieldkit.commands.sf.opportunity._fetch_opportunity",
            return_value=_bug031_untracked_opp_exit_code_sample_rec(),
        ),
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
    ):
        run_opportunity(_BUG031_UNTRACKED_OPP_EXIT_CODE__VALID_OPP_ID, None, write=True)

    captured = capsys.readouterr()
    assert "not tracked" in captured.err.lower() or "no matching pursuit" in captured.err.lower()
    assert _BUG031_UNTRACKED_OPP_EXIT_CODE__VALID_OPP_ID in captured.err


def test_bug031_untracked_opp_exit_code_untracked_opp_no_write_returns_0() -> None:
    """With write=False (--no-write), untracked opp still returns 0 (print-only mode)."""
    with (
        patch(
            "fieldkit.commands.sf.opportunity._fetch_opportunity",
            return_value=_bug031_untracked_opp_exit_code_sample_rec(),
        ),
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        patch("fieldkit.commands.sf.opportunity._resolve_pursuit_file", return_value=None),
    ):
        result = run_opportunity(_BUG031_UNTRACKED_OPP_EXIT_CODE__VALID_OPP_ID, None, write=False)

    assert result == 0, "No-write mode should still return 0 even for untracked opps"


# ── Phase 8 addition (task 8.6) ───────────────────────────────────────────────


# ── TestPrintSummaryHandlesNoneAmounts (flattened) ──────────────────────────


def _print_summary_none_amounts_rec() -> dict:
    return {
        "Id": "006NULL000000000AA",
        "Name": "Null Amounts Opp",
        "StageName": "Discover",
        "CloseDate": "2026-12-31",
        "IsClosed": False,
        "Owner": {"Name": "Jane", "Email": "j@example.com"},  # pii-guard: ignore
        "ACV_Opportunity_USD__c": None,
        "ARR_Opportunity_USD__c": None,
        "Consulting_Total_USD__c": None,
        "Training_Total_USD__c": None,
        "Application_Services_Total_USD__c": None,
        "Services_Total_USD__c": None,
        "Next_Steps__c": None,
        "Customer_Pain_Point__c": None,
        "Identify_Pain_Long__c": None,
        "Decision_Criteria__c": None,
        "Main_Competitor__c": None,
        "Closed_Lost_Reason__c": None,
        "Account": {"Id": "001A", "Name": "Acme", "Industry": "Tech"},
    }


def test_print_summary_print_summary_handles_none_amounts(capsys: pytest.CaptureFixture[str]) -> None:
    """8.6: _print_summary must not raise when all monetary fields are None."""
    # Must complete without raising any exception.
    _print_summary(_print_summary_none_amounts_rec(), pursuit_file=None, splits=None)
    out = capsys.readouterr().out
    # Minimal sanity: the opp name appears in the output.
    assert "Null Amounts Opp" in out


# ── CRAP-reduction: additional branch coverage for _print_summary ─────────────
# These tests target uncovered branches in _print_summary to reduce CRAP score.


# ── TestPrintSummaryBranchCoverage (flattened) ──────────────────────────────


def _print_summary_base_rec(**overrides: Any) -> dict:
    rec: dict = {
        "Id": "006TEST000000001",
        "Name": "Test Opportunity",
        "StageName": "Propose",
        "CloseDate": "2026-12-31",
        "IsClosed": False,
        "Owner": {"Name": "Jane Doe", "Email": "jdoe@example.com"},  # pii-guard: ignore
        "ACV_Opportunity_USD__c": 100000.0,
        "ARR_Opportunity_USD__c": 0.0,
        "Consulting_Total_USD__c": 80000.0,
        "Training_Total_USD__c": 20000.0,
        "Application_Services_Total_USD__c": 0.0,
        "Services_Total_USD__c": 100000.0,
        "Next_Steps__c": "Follow up",
        "Customer_Pain_Point__c": "Manual process",
        "Identify_Pain_Long__c": "Automation gap",
        "Decision_Criteria__c": "ROI > 3x",
        "Main_Competitor__c": "IBM",
        "Closed_Lost_Reason__c": None,
        "Account": {"Id": "001A", "Name": "Acme Corp", "Industry": "Technology"},
    }
    rec.update(overrides)
    return rec


def test_print_summary_closed_lost_reason_printed_when_present(capsys: pytest.CaptureFixture[str]) -> None:
    """Closed_Lost_Reason__c is printed when non-None."""
    rec = _print_summary_base_rec(**{"Closed_Lost_Reason__c": "Budget cut"})
    _print_summary(rec, None)
    out = capsys.readouterr().out
    assert "Budget cut" in out
    assert "Closed Lost Reason" in out


def test_print_summary_closed_lost_reason_omitted_when_none(capsys: pytest.CaptureFixture[str]) -> None:
    """Closed_Lost_Reason__c section is omitted when None."""
    rec = _print_summary_base_rec()
    _print_summary(rec, None)
    out = capsys.readouterr().out
    assert "Closed Lost Reason" not in out


def test_print_summary_pursuit_file_shown_when_provided(capsys: pytest.CaptureFixture[str]) -> None:
    """Local pursuit file path is shown when provided."""
    rec = _print_summary_base_rec()
    _print_summary(rec, "/path/to/pursuit.md")
    out = capsys.readouterr().out
    assert "/path/to/pursuit.md" in out
    assert "UNTRACKED" not in out


def test_print_summary_untracked_shown_when_no_pursuit_file(capsys: pytest.CaptureFixture[str]) -> None:
    """'UNTRACKED' is shown when pursuit_file is None."""
    rec = _print_summary_base_rec()
    _print_summary(rec, None)
    out = capsys.readouterr().out
    assert "UNTRACKED" in out


def test_print_summary_identify_pain_shown_when_set(capsys: pytest.CaptureFixture[str]) -> None:
    """Identify Pain is printed when Identify_Pain_Long__c is set."""
    rec = _print_summary_base_rec()
    _print_summary(rec, None)
    out = capsys.readouterr().out
    assert "Automation gap" in out


def test_print_summary_fallback_to_customer_pain_point(capsys: pytest.CaptureFixture[str]) -> None:
    """Customer_Pain_Point__c is used when Identify_Pain_Long__c is None."""
    rec = _print_summary_base_rec(**{"Identify_Pain_Long__c": None, "Customer_Pain_Point__c": "Slow deploys"})
    _print_summary(rec, None)
    out = capsys.readouterr().out
    assert "Slow deploys" in out


def test_print_summary_not_set_shown_for_missing_pain(capsys: pytest.CaptureFixture[str]) -> None:
    """'(not set)' is shown when both pain fields are None."""
    rec = _print_summary_base_rec(**{"Identify_Pain_Long__c": None, "Customer_Pain_Point__c": None})
    _print_summary(rec, None)
    out = capsys.readouterr().out
    assert "(not set)" in out


def test_print_summary_next_steps_not_set_when_none(capsys: pytest.CaptureFixture[str]) -> None:
    """'(not set)' is shown for Next_Steps__c when None."""
    rec = _print_summary_base_rec(**{"Next_Steps__c": None})
    _print_summary(rec, None)
    out = capsys.readouterr().out
    assert "(not set)" in out


def test_print_summary_deal_splits_column_width_adapts_to_longest_label(capsys: pytest.CaptureFixture[str]) -> None:
    """Deal splits column width adapts to the longest offering_group label."""
    rec = _print_summary_base_rec()
    splits = [
        {"offering_group": "Short", "services_pct": 30.0},
        {"offering_group": "A Very Long Offering Group Name", "services_pct": 70.0},
    ]
    _print_summary(rec, None, splits=splits)
    out = capsys.readouterr().out
    assert "A Very Long Offering Group Name" in out
    assert "30%" in out
    assert "70%" in out


def test_print_summary_owner_none_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_summary handles Owner=None without raising."""
    rec = _print_summary_base_rec(**{"Owner": None})
    _print_summary(rec, None)  # must not raise
    out = capsys.readouterr().out
    assert "Test Opportunity" in out


def test_print_summary_account_none_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_summary handles Account=None without raising."""
    rec = _print_summary_base_rec(**{"Account": None})
    _print_summary(rec, None)  # must not raise
    out = capsys.readouterr().out
    assert "Test Opportunity" in out


# ── TestFetchOpportunityBranchCoverage (flattened) ──────────────────────────


def test_fetch_opportunity_fetch_opportunity_no_sid_exits_2() -> None:
    """_fetch_opportunity exits 2 when no session ID."""
    from fieldkit.commands.sf.opportunity import _fetch_opportunity

    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value=None),
        pytest.raises(SystemExit) as exc_info,
    ):
        _fetch_opportunity("006ABC123456789")
    assert exc_info.value.code == 2


def test_fetch_opportunity_fetch_opportunity_no_base_url_exits_2() -> None:
    """_fetch_opportunity exits 2 when no base URL."""
    from fieldkit.commands.sf.opportunity import _fetch_opportunity

    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value=""),
        pytest.raises(SystemExit) as exc_info,
    ):
        _fetch_opportunity("006ABC123456789")
    assert exc_info.value.code == 2


def test_fetch_opportunity_fetch_opportunity_sfautherror_exits_2() -> None:
    """_fetch_opportunity re-raises SFAuthError; backstop in __main__.py maps → 2.

    After historic regression migration, _fetch_opportunity re-raises SFAuthError instead of
    calling sys.exit(2). The __main__.py dispatcher backstop converts it to exit 2
    when running via `python -m fieldkit`. Unit tests assert the exception propagates.
    """
    from fieldkit.commands.sf.opportunity import _fetch_opportunity
    from fieldkit.sf.client import SFAuthError

    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.sf.client.SFDirectClient") as mock_cls,
        pytest.raises(SFAuthError, match="expired"),
    ):
        mock_cls.return_value.__enter__.return_value.fetch_record.side_effect = SFAuthError("expired")
        _fetch_opportunity("006ABC123456789")


def test_fetch_opportunity_fetch_opportunity_sfnotfounderror_exits_3() -> None:
    """_fetch_opportunity exits 3 on SFNotFoundError."""
    from fieldkit.commands.sf.opportunity import _fetch_opportunity
    from fieldkit.sf.client import SFNotFoundError

    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.sf.client.SFDirectClient") as mock_cls,
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_cls.return_value.__enter__.return_value.fetch_record.side_effect = SFNotFoundError("not found")
        _fetch_opportunity("006ABC123456789")
    assert exc_info.value.code == 3


def test_fetch_opportunity_fetch_opportunity_sfapierror_exits_1() -> None:
    """_fetch_opportunity exits 1 on SFAPIError."""
    from fieldkit.commands.sf.opportunity import _fetch_opportunity
    from fieldkit.sf.client import SFAPIError

    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.sf.client.SFDirectClient") as mock_cls,
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_cls.return_value.__enter__.return_value.fetch_record.side_effect = SFAPIError("timeout")
        _fetch_opportunity("006ABC123456789")
    assert exc_info.value.code == 1


def test_fetch_opportunity_resolve_pursuit_file_returns_none_when_get_fieldkit_home_raises() -> None:
    """_resolve_pursuit_file returns None when get_fieldkit_home raises."""
    from fieldkit.commands.sf.opportunity import _resolve_pursuit_file

    with patch("fieldkit.commands.sf.opportunity.get_fieldkit_home", side_effect=Exception("no config")):
        result = _resolve_pursuit_file("006ABC123456789")
    assert result is None


def test_fetch_opportunity_resolve_pursuit_file_returns_none_when_no_pursuit_dir() -> None:
    """_resolve_pursuit_file returns None when no pursuit_dir in config."""
    from fieldkit.commands.sf.opportunity import _resolve_pursuit_file

    with (
        patch("fieldkit.commands.sf.opportunity.get_fieldkit_home", return_value=Path("/tmp")),
        patch(
            "fieldkit.commands.sf.opportunity.get_accounts_config",
            return_value={"accounts": {"acme": {"no_pursuit_dir": True}}},
        ),
    ):
        result = _resolve_pursuit_file("006ABC123456789")
    assert result is None


def test_fetch_opportunity_run_opportunity_writes_when_pursuit_file_found(tmp_path: Path) -> None:
    """run_opportunity calls do_write_opp when pursuit_file is found."""
    from fieldkit.commands.sf.opportunity import run_opportunity

    sample_rec = {
        "Id": "006GPAY00000000AAA",
        "Name": "Test Opp",
        "StageName": "Propose",
        "CloseDate": "2026-12-31",
        "IsClosed": False,
        "Owner": {"Name": "Jane", "Email": "j@example.com"},  # pii-guard: ignore
        "ACV_Opportunity_USD__c": 100000.0,
        "ARR_Opportunity_USD__c": 0.0,
        "Consulting_Total_USD__c": 80000.0,
        "Training_Total_USD__c": 20000.0,
        "Application_Services_Total_USD__c": 0.0,
        "Services_Total_USD__c": 100000.0,
        "Next_Steps__c": "Follow up",
        "Customer_Pain_Point__c": None,
        "Identify_Pain_Long__c": None,
        "Decision_Criteria__c": None,
        "Main_Competitor__c": None,
        "Closed_Lost_Reason__c": None,
        "Account": {"Id": "001A", "Name": "Acme", "Industry": "Tech"},
        "Probability": 50,
    }
    pursuit_file = str(tmp_path / "deal.md")
    (tmp_path / "deal.md").write_text("---\ntitle: test\n---\n# Body\n", encoding="utf-8")

    with (
        patch("fieldkit.commands.sf.opportunity._fetch_opportunity", return_value=sample_rec),
        patch("fieldkit.commands.sf.opportunity._fetch_deal_splits", return_value=[]),
        # BUG: without this the test reached the live Salesforce API. run_opportunity
        # also calls _fetch_contract_type, which opens its own SFDirectClient and
        # returns "standard" early *only* when no session is configured. On CI there
        # is no session, so it short-circuits and the test passed; on a developer
        # machine with a stale sid cookie it issued a real request and failed with
        # SFAuthError (HTTP 401). A test whose result depends on whether the operator
        # happens to hold credentials is not testing what it claims to.
        patch("fieldkit.commands.sf.opportunity._fetch_contract_type", return_value="standard"),
        patch("fieldkit.commands.sf.sync.do_write_opp") as mock_write,
    ):
        result = run_opportunity("006GPAY00000000AAA", pursuit_file, write=True)

    assert result == 0
    mock_write.assert_called_once()
