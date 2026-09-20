"""Tests for fieldkit.commands.sf.quote — CPQ Quote + quote lines reader (implementation note)."""

import json
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.quote import (
    _build_payload,
    _fetch_quote_lines,
    _parse_line,
    cli,
    run_quote,
)

pytestmark = pytest.mark.unit

_QUOTE_ID = "a0Q000000000000AAA"


# ── Sample data ─────────────────────────────────────────────────────────────


def _sample_quote_rec() -> dict[str, Any]:
    return {
        "Id": _QUOTE_ID,
        "Name": "Q-00042",
        "SBQQ__Status__c": "Draft",
        "SBQQ__NetAmount__c": 250000.0,
        "SBQQ__ListAmount__c": 300000.0,
        "SBQQ__CustomerAmount__c": 250000.0,
        "SBQQ__AverageCustomerDiscount__c": 16.67,
        "SBQQ__Opportunity2__c": "006GPAY00000000AAA",
    }


def _ui_line_record(
    *,
    rec_id: str = "a0R000000000000AAA",
    product: str = "OpenShift Platform Plus",
    quantity: float = 100.0,
    list_total: float = 200000.0,
    net_total: float = 160000.0,
    discount: float = 20.0,
) -> dict[str, Any]:
    """Build a raw UI-API related-list-records line record."""
    return {
        "apiName": "SBQQ__QuoteLine__c",
        "id": rec_id,
        "fields": {
            "SBQQ__ProductName__c": {"value": product, "displayValue": None},
            "SBQQ__Quantity__c": {"value": quantity, "displayValue": None},
            "SBQQ__ListTotal__c": {"value": list_total, "displayValue": None},
            "SBQQ__NetTotal__c": {"value": net_total, "displayValue": None},
            "SBQQ__Discount__c": {"value": discount, "displayValue": None},
        },
    }


def _base_session_patches() -> tuple:
    return (
        patch("fieldkit.commands.sf.quote.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.quote.get_sf_rest_base_url", return_value="https://test.my.salesforce.com"),
    )


def _make_sf_client_mock(mock_cls: Any, *, quote_rec: dict[str, Any] | None = None, lines: list | None = None) -> Any:
    """Configure a SFDirectClient context-manager mock with optional return values."""
    m = mock_cls.return_value.__enter__.return_value
    if quote_rec is not None:
        m.fetch_sobject.return_value = quote_rec
    if lines is not None:
        m.fetch_related_list_records.return_value = lines
    return m


# ── _parse_line: line parsing ───────────────────────────────────────────────


def test_parse_line_extracts_all_fields() -> None:
    line = _parse_line(_ui_line_record())
    assert line["id"] == "a0R000000000000AAA"
    assert line["product"] == "OpenShift Platform Plus"
    assert line["quantity"] == 100.0
    assert line["list_total"] == 200000.0
    assert line["net_total"] == 160000.0
    assert line["discount"] == 20.0


def test_parse_line_net_total_fallback_key() -> None:
    """net_total falls back to SBQQ__NetPrice__c when SBQQ__NetTotal__c absent."""
    rec = {"id": "x", "fields": {"SBQQ__NetPrice__c": {"value": 999.0}}}
    line = _parse_line(rec)
    assert line["net_total"] == 999.0


def test_parse_line_uses_display_value_when_value_none() -> None:
    """When value is None but displayValue is set, displayValue is used."""
    rec = {"id": "x", "fields": {"SBQQ__ProductName__c": {"value": None, "displayValue": "Formula Product"}}}
    line = _parse_line(rec)
    assert line["product"] == "Formula Product"


def test_parse_line_missing_fields_yield_none() -> None:
    line = _parse_line({"id": "x", "fields": {}})
    assert line["product"] is None
    assert line["quantity"] is None
    assert line["net_total"] is None


# ── _build_payload: header parsing + JSON shape ─────────────────────────────


def test_build_payload_header_fields_mapped() -> None:
    payload = _build_payload(_sample_quote_rec(), [])
    assert payload["status"] == "ok"
    assert payload["quote_id"] == _QUOTE_ID
    assert payload["quote_number"] == "Q-00042"
    assert payload["quote_status"] == "Draft"
    assert payload["net_amount"] == 250000.0
    assert payload["list_amount"] == 300000.0
    assert payload["average_discount"] == 16.67
    assert payload["opportunity_id"] == "006GPAY00000000AAA"


def test_build_payload_line_count_and_lines() -> None:
    lines = [_parse_line(_ui_line_record())]
    payload = _build_payload(_sample_quote_rec(), lines)
    assert payload["line_count"] == 1
    assert payload["lines"][0]["product"] == "OpenShift Platform Plus"
    assert "pulled_at" in payload and "T" in payload["pulled_at"]


# ── _fetch_quote_lines: related-list parsing + graceful degradation ─────────


def test_fetch_quote_lines_parses_related_list_records() -> None:
    raw = [_ui_line_record(), _ui_line_record(rec_id="a0R000000000001AAA", product="Ansible", net_total=90000.0)]
    p1, p2 = _base_session_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        mock_client = mock_cls.return_value.__enter__.return_value
        mock_client.fetch_related_list_records.return_value = raw
        result = _fetch_quote_lines(_QUOTE_ID)

    assert len(result) == 2
    assert result[0]["product"] == "OpenShift Platform Plus"
    assert result[1]["product"] == "Ansible"
    assert result[1]["net_total"] == 90000.0
    # Verify the correct relatedListId is requested.
    mock_client.fetch_related_list_records.assert_called_once_with(_QUOTE_ID, "SBQQ__LineItems__r")


def test_fetch_quote_lines_empty_when_no_lines() -> None:
    p1, p2 = _base_session_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        mock_cls.return_value.__enter__.return_value.fetch_related_list_records.return_value = []
        assert _fetch_quote_lines(_QUOTE_ID) == []


def test_fetch_quote_lines_empty_on_no_sid() -> None:
    with patch("fieldkit.commands.sf.quote.get_sf_session_id", return_value=None):
        assert _fetch_quote_lines(_QUOTE_ID) == []


def test_fetch_quote_lines_empty_on_api_error(caplog: pytest.LogCaptureFixture) -> None:
    from fieldkit.sf.client import SFAPIError

    p1, p2 = _base_session_patches()
    with caplog.at_level("WARNING"), p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        mock_cls.return_value.__enter__.return_value.fetch_related_list_records.side_effect = SFAPIError("boom")
        assert _fetch_quote_lines(_QUOTE_ID) == []
    # WARNING must be emitted so the operator knows lines were unavailable.
    assert any("Quote lines unavailable" in r.message for r in caplog.records)


def test_fetch_quote_lines_empty_on_auth_error() -> None:
    from fieldkit.sf.client import SFAuthError

    p1, p2 = _base_session_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        mock_cls.return_value.__enter__.return_value.fetch_related_list_records.side_effect = SFAuthError("expired")
        assert _fetch_quote_lines(_QUOTE_ID) == []


# ── run_quote / CLI: human output ───────────────────────────────────────────
# Patch at the SFDirectClient boundary (public API) to avoid private-symbol coupling.


def test_run_quote_prints_header_and_lines(capsys: pytest.CaptureFixture[str]) -> None:
    p1, p2 = _base_session_patches()
    raw_lines = [_ui_line_record()]
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        _make_sf_client_mock(mock_cls, quote_rec=_sample_quote_rec(), lines=raw_lines)
        rc = run_quote(_QUOTE_ID)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Q-00042" in out
    assert "Draft" in out
    assert "$250,000" in out
    assert "OpenShift Platform Plus" in out
    assert "Quote Lines (1)" in out


def test_run_quote_no_lines_message(capsys: pytest.CaptureFixture[str]) -> None:
    p1, p2 = _base_session_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        _make_sf_client_mock(mock_cls, quote_rec=_sample_quote_rec(), lines=[])
        rc = run_quote(_QUOTE_ID)

    out = capsys.readouterr().out
    assert rc == 0
    assert "no quote lines found" in out.lower()


def test_run_quote_invalid_id_returns_3() -> None:
    assert run_quote("TBD") == 3
    assert run_quote("short") == 3


# ── CLI --json ──────────────────────────────────────────────────────────────


def test_cli_json_shape() -> None:
    raw_lines = [_ui_line_record()]
    runner = CliRunner()
    p1, p2 = _base_session_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        _make_sf_client_mock(mock_cls, quote_rec=_sample_quote_rec(), lines=raw_lines)
        result = runner.invoke(cli, [_QUOTE_ID, "--json"])

    assert result.exit_code == 0
    # Strip any leading log lines (written to stderr, mixed by CliRunner); parse the JSON block.
    json_text = "\n".join(line for line in result.output.splitlines() if not line.startswith("[sf-quote]"))
    payload = json.loads(json_text)
    assert payload["quote_id"] == _QUOTE_ID
    assert payload["quote_number"] == "Q-00042"
    assert payload["quote_status"] == "Draft"
    assert payload["line_count"] == 1
    assert payload["lines"][0]["net_total"] == 160000.0


def test_cli_invalid_id_exits_3() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["not-an-id"])
    assert result.exit_code == 3
    assert "Invalid quote ID" in result.output


def test_cli_json_no_sid_exits_2() -> None:
    """--json path exits 2 when no session ID is configured."""
    runner = CliRunner()
    with patch("fieldkit.commands.sf.quote.get_sf_session_id", return_value=None):
        result = runner.invoke(cli, [_QUOTE_ID, "--json"])
    assert result.exit_code == 2


def test_cli_human_output_default() -> None:
    raw_lines = [_ui_line_record()]
    runner = CliRunner()
    p1, p2 = _base_session_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        _make_sf_client_mock(mock_cls, quote_rec=_sample_quote_rec(), lines=raw_lines)
        result = runner.invoke(cli, [_QUOTE_ID])

    assert result.exit_code == 0
    assert "Q-00042" in result.output
    assert "OpenShift Platform Plus" in result.output


# ── _fetch_quote: session / error branches ──────────────────────────────────
# These tests exercise error paths on _fetch_quote via run_quote (public API)
# to avoid importing the private helper directly.


def test_fetch_quote_no_sid_exits_2() -> None:
    """No configured session ID → run_quote propagates SystemExit(2)."""
    with (
        patch("fieldkit.commands.sf.quote.get_sf_session_id", return_value=None),
        pytest.raises(SystemExit) as exc_info,
    ):
        run_quote(_QUOTE_ID)
    assert exc_info.value.code == 2


def test_fetch_quote_not_found_exits_3() -> None:
    """Quote ID not found in SF → run_quote propagates SystemExit(3)."""
    from fieldkit.sf.client import SFNotFoundError

    p1, p2 = _base_session_patches()
    with (
        p1,
        p2,
        patch("fieldkit.sf.client.SFDirectClient") as mock_cls,
        pytest.raises(SystemExit) as exc_info,
    ):
        mock_cls.return_value.__enter__.return_value.fetch_sobject.side_effect = SFNotFoundError("nope")
        run_quote(_QUOTE_ID)
    assert exc_info.value.code == 3


def test_fetch_quote_auth_error_reraised() -> None:
    """SF auth failure → run_quote re-raises SFAuthError."""
    from fieldkit.sf.client import SFAuthError

    p1, p2 = _base_session_patches()
    with (
        p1,
        p2,
        patch("fieldkit.sf.client.SFDirectClient") as mock_cls,
        pytest.raises(SFAuthError, match=r"expired"),
    ):
        mock_cls.return_value.__enter__.return_value.fetch_sobject.side_effect = SFAuthError("expired")
        run_quote(_QUOTE_ID)


def test_fetch_quote_success_returns_record(capsys: pytest.CaptureFixture[str]) -> None:
    """Successful fetch renders the quote number in human output."""
    p1, p2 = _base_session_patches()
    with p1, p2, patch("fieldkit.sf.client.SFDirectClient") as mock_cls:
        _make_sf_client_mock(mock_cls, quote_rec=_sample_quote_rec(), lines=[])
        rc = run_quote(_QUOTE_ID)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Q-00042" in out
