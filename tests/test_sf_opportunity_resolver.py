"""Tests for implementation change: opportunity number to Salesforce record Id resolution.

Unit tests for the shared domain resolver and the CLI numeric-argument
detection path in fieldkit.commands.sf.opportunity.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.opportunity import cli
from fieldkit.sf.opportunities import OPPORTUNITY_NUMBER_RE, resolve_opportunity_reference

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# OPPORTUNITY_NUMBER_RE pattern tests
# ---------------------------------------------------------------------------


def test_opp_number_re_matches_8_digit_number() -> None:
    """Typical SF opportunity number (8 digits) must match."""
    assert OPPORTUNITY_NUMBER_RE.match("71721820") is not None


def test_opp_number_re_matches_5_digit_number() -> None:
    assert OPPORTUNITY_NUMBER_RE.match("12345") is not None


def test_opp_number_re_matches_12_digit_number() -> None:
    assert OPPORTUNITY_NUMBER_RE.match("123456789012") is not None


def test_opp_number_re_rejects_18_char_id() -> None:
    """A standard 18-char SF record Id must not match the number pattern."""
    assert OPPORTUNITY_NUMBER_RE.match("006Pe000012n2GkIAI") is None


def test_opp_number_re_rejects_alphanumeric() -> None:
    assert OPPORTUNITY_NUMBER_RE.match("006ABC123") is None


def test_opp_number_re_rejects_empty() -> None:
    assert OPPORTUNITY_NUMBER_RE.match("") is None


def test_opp_number_re_rejects_fewer_than_5_digits() -> None:
    assert OPPORTUNITY_NUMBER_RE.match("1234") is None


def test_opp_number_re_rejects_more_than_12_digits() -> None:
    assert OPPORTUNITY_NUMBER_RE.match("1234567890123") is None


def test_opp_number_re_rejects_trailing_newline() -> None:
    r"""\Z (not $) must reject a value with a trailing newline."""
    assert OPPORTUNITY_NUMBER_RE.match("71721820\n") is None


def test_opp_number_re_rejects_non_ascii_digits() -> None:
    """[0-9] (not \\d) must reject non-ASCII Unicode digits (e.g. Arabic-Indic)."""
    arabic_indic_55555 = "\u0665" * 5  # ARABIC-INDIC DIGIT FIVE: a Unicode \d that is not [0-9]
    assert OPPORTUNITY_NUMBER_RE.match(arabic_indic_55555) is None


# ---------------------------------------------------------------------------
# resolve_opportunity_reference
# ---------------------------------------------------------------------------


def _make_sosl_response(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"searchRecords": records}


def test_resolve_opp_id_from_number_returns_id_on_match() -> None:
    """Successful SOSL lookup returns the 18-char record Id."""
    expected_id = "006Pe000012n2GkIAI"
    sosl_response = _make_sosl_response([{"Id": expected_id, "attributes": {"type": "Opportunity"}}])

    client = MagicMock()
    client.sosl_search.return_value = sosl_response["searchRecords"]

    result = resolve_opportunity_reference(client, "71721820")

    assert result == expected_id


def test_resolve_opp_id_from_number_returns_none_on_empty_results() -> None:
    """Empty SOSL results (number not found) returns None."""
    client = MagicMock()
    client.sosl_search.return_value = []

    result = resolve_opportunity_reference(client, "71721820")

    assert result is None


def test_resolve_opportunity_reference_validates_direct_id_without_sosl() -> None:
    client = MagicMock()
    result = resolve_opportunity_reference(client, "006Pe000012n2GkIAI")
    assert result == "006Pe000012n2GkIAI"
    client.fetch_record.assert_called_once_with("006Pe000012n2GkIAI", fields="Id")
    client.sosl_search.assert_not_called()


@pytest.mark.parametrize(
    "bad_input",
    [
        "71721820') OR (Id != '",  # SOSL injection attempt
        "abc12345",  # alphanumeric
        "ABCD1234",  # uppercase + digits
        "",  # empty string
        "1234",  # too short (< 5 digits)
        "1234567890123",  # too long (> 12 digits)
    ],
)
def test_resolve_opp_id_from_number_rejects_non_digit_input(bad_input: str) -> None:
    """Defense-in-depth guard: non-digit strings must return None without touching SOSL.

    Regression guard for the review finding: the function must validate opp_number
    internally, not only rely on the _OPP_NUMBER_RE check in cli(). A direct caller
    passing "71721820') OR (Id != '" must not reach the SOSL f-string.

    Parametrized (not a for-loop) so each input is an independent case — a failure
    names the exact input and does not abort the remaining cases.
    """
    client = MagicMock()
    result = resolve_opportunity_reference(client, bad_input)
    assert result is None
    client.sosl_search.assert_not_called()


def test_resolve_opp_id_from_number_returns_none_on_api_error() -> None:
    """SFAPIError during SOSL lookup returns None (non-fatal)."""
    from fieldkit.sf.client import SFAPIError

    client = MagicMock()
    client.sosl_search.side_effect = SFAPIError("timeout")

    result = resolve_opportunity_reference(client, "71721820")

    assert result is None


# ---------------------------------------------------------------------------
# CLI integration: numeric argument detection
# ---------------------------------------------------------------------------


def test_cli_resolves_numeric_opp_number_to_id() -> None:
    """When OPP_ID is all-digits, the CLI resolves it via SOSL before fetching.

    Verifies by patching the public config boundary (get_sf_session_id,
    get_sf_rest_base_url) and the SFDirectClient, and checking that
    run_opportunity receives the resolved 18-char Id, not the bare number.
    """
    resolved_id = "006Pe000012n2GkIAI"

    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value="sid123"),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value="https://org.salesforce.com"),
        patch("fieldkit.commands.sf.opportunity.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.opportunity.run_opportunity", return_value=0) as mock_run,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.sosl_search.return_value = [{"Id": resolved_id, "attributes": {"type": "Opportunity"}}]
        runner = CliRunner()
        result = runner.invoke(cli, ["71721820", "--no-write"])

    # Resolution must succeed (exit 0) and forward the 18-char Id — asserted
    # unconditionally so a regressed resolution path (exit 3, run_opportunity
    # never reached) fails the test instead of passing vacuously.
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    args, _ = mock_run.call_args
    assert args[0] == resolved_id


def test_cli_exits_3_when_number_cannot_be_resolved() -> None:
    """When a numeric OPP_ID cannot be resolved, CLI exits with code 3.

    Verifies by patching the public config boundary and SFDirectClient to
    return no records — the CLI must exit 3 without touching the SF REST API.
    """
    with (
        patch("fieldkit.commands.sf.opportunity.get_sf_session_id", return_value="sid123"),
        patch("fieldkit.commands.sf.opportunity.get_sf_rest_base_url", return_value="https://org.salesforce.com"),
        patch("fieldkit.commands.sf.opportunity.SFDirectClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.sosl_search.return_value = []  # no match for this number
        runner = CliRunner()
        result = runner.invoke(cli, ["71721820"])

    assert result.exit_code == 3
