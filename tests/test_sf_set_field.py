"""Regression tests for fieldkit sf set-field argument parsing.

Covers historic regression / historic regression: Click crashed when VALUE started with '-'.
The fix adds type=click.UNPROCESSED to the value argument so Click
passes the raw token through without flag-sniffing.

These tests exercise only the Click argument-parsing layer — they mock
out the SF client so no network calls are made.
"""

from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner, Result

from fieldkit.commands.sf.set_field import cli

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OPP_ID = "006000000000000AAA"
_FIELD = "Next_Steps__c"
_IDENTIFY_PAIN_FIELD = "Identify_Pain_Long__c"
_IDENTIFY_PAIN_VALUE = "Manual renewal steps are slowing the approval cycle."
_FAKE_RECORD = {
    "Id": _OPP_ID,
    "Name": "Test Opportunity",
    _FIELD: "Old value",
}


def _invoke_preview(value: str, field: str = _FIELD) -> Result:
    """Invoke the CLI in preview (no --confirm) mode with the given VALUE."""
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.set_field.SFDirectClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.fetch_record.return_value = _FAKE_RECORD
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        return runner.invoke(cli, [_OPP_ID, field, value])


# ---------------------------------------------------------------------------
# Parametrised: dash-prefixed and normal VALUE strings are accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected_in_output",
    [
        # historic regression / historic regression: single-dash prefix
        ("- Key point", "- Key point"),
        # Double-dash prefix (flag-like)
        ("--pending review", "--pending review"),
        # Negative number prefix
        ("-1", "-1"),
        # Normal value — must still work identically
        ("Validate", "Validate"),
        # Value with spaces (quoted by caller)
        ("Some normal text", "Some normal text"),
    ],
)
def test_value_accepted_without_crash(value: str, expected_in_output: str) -> None:
    """VALUE strings (including dash-prefixed) are accepted and shown in preview."""
    result = _invoke_preview(value)
    assert result.exit_code == 0, f"Unexpected exit {result.exit_code}:\n{result.output}"
    assert expected_in_output in result.output
    assert "[preview]" in result.output


# ---------------------------------------------------------------------------
# Missing VALUE → shows help (existing behaviour, not changed)
# ---------------------------------------------------------------------------


def test_missing_value_shows_help() -> None:
    """Omitting VALUE shows help and exits non-zero (existing behaviour)."""
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
    ):
        result = runner.invoke(cli, [_OPP_ID, _FIELD])
    # historic regression: exits 2 (usage error) when any positional arg is missing
    assert result.exit_code == 2
    assert "Usage:" in result.output


# ---------------------------------------------------------------------------
# --list-fields flag works independently of VALUE
# ---------------------------------------------------------------------------


def test_list_fields_flag_exits_0() -> None:
    """--list-fields prints the allowlist and exits 0 without needing VALUE."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--list-fields"])
    assert result.exit_code == 0
    assert "Opportunity" in result.output
    assert "Next_Steps__c" in result.output


def test_identify_pain_field_is_listed() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--list-fields"])
    assert result.exit_code == 0
    assert _IDENTIFY_PAIN_FIELD in result.output
    assert "Identify Pain Long" in result.output


def test_identify_pain_field_appears_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert _IDENTIFY_PAIN_FIELD in result.output
    assert "Identify Pain Long" in result.output


def test_identify_pain_preview_does_not_write() -> None:
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.fetch_record.return_value = {**_FAKE_RECORD, _IDENTIFY_PAIN_FIELD: "Old narrative"}

    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.set_field.SFDirectClient") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(cli, [_OPP_ID, _IDENTIFY_PAIN_FIELD, _IDENTIFY_PAIN_VALUE])

    assert result.exit_code == 0
    assert "[preview]" in result.output
    assert _IDENTIFY_PAIN_VALUE in result.output
    mock_client.update_opportunity_fields.assert_not_called()


def test_identify_pain_confirmed_write_uses_exact_payload() -> None:
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.fetch_record.return_value = {**_FAKE_RECORD, _IDENTIFY_PAIN_FIELD: "Old narrative"}

    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.set_field.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.set_field.stdin_is_interactive", return_value=False),
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(cli, [_OPP_ID, _IDENTIFY_PAIN_FIELD, _IDENTIFY_PAIN_VALUE, "--confirm"])

    assert result.exit_code == 0
    mock_client.update_opportunity_fields.assert_called_once_with(
        _OPP_ID,
        {_IDENTIFY_PAIN_FIELD: _IDENTIFY_PAIN_VALUE},
    )


# ---------------------------------------------------------------------------
# Disallowed field is rejected before SF call
# ---------------------------------------------------------------------------


def test_disallowed_field_exits_1() -> None:
    """A field not in the allowlist is rejected with exit code 1."""
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
    ):
        result = runner.invoke(cli, [_OPP_ID, "StageName", "Validate"])
    assert result.exit_code == 1
    assert "not allowed" in result.output


# ---------------------------------------------------------------------------
# No SF session → exits 2 before any SF call
# ---------------------------------------------------------------------------


def test_no_sf_session_exits_2() -> None:
    """Missing SF session exits with code 2 (auth failure)."""
    runner = CliRunner()
    with patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value=None):
        result = runner.invoke(cli, [_OPP_ID, _FIELD, "some value"])
    assert result.exit_code == 2
    assert "No SF session" in result.output


# ---------------------------------------------------------------------------
# Non-TTY: --confirm writes without interactive prompt (implementation note)
# ---------------------------------------------------------------------------


def test_confirm_non_tty_writes_without_prompt() -> None:
    """In non-TTY mode, --confirm skips the interactive prompt and writes."""
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.fetch_record.return_value = _FAKE_RECORD

    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.set_field.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.set_field.stdin_is_interactive", return_value=False),
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(cli, [_OPP_ID, _FIELD, "New value", "--confirm"], catch_exceptions=False)

    assert result.exit_code == 0
    mock_client.update_opportunity_fields.assert_called_once()
    assert "Type" not in result.output


def test_confirm_tty_yes_writes() -> None:
    """In TTY mode, typing 'yes' at the prompt triggers the write."""
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.fetch_record.return_value = _FAKE_RECORD

    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.set_field.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.set_field.stdin_is_interactive", return_value=True),
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(
            cli,
            [_OPP_ID, _FIELD, "New value", "--confirm"],
            input="yes\n",
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    mock_client.update_opportunity_fields.assert_called_once()


def test_confirm_tty_no_aborts() -> None:
    """In TTY mode, typing 'no' at the prompt aborts the write."""
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.fetch_record.return_value = _FAKE_RECORD

    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.set_field.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.set_field.stdin_is_interactive", return_value=True),
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(
            cli,
            [_OPP_ID, _FIELD, "New value", "--confirm"],
            input="no\n",
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    mock_client.update_opportunity_fields.assert_not_called()
    assert "Aborted" in result.output


# ---------------------------------------------------------------------------
# CloseDate validation (implementation note)
# ---------------------------------------------------------------------------

_CLOSE_DATE_RECORD = {
    "Id": _OPP_ID,
    "Name": "Test Opportunity",
    "CloseDate": "2026-06-01",
}


def test_closedate_valid_iso_date_accepted() -> None:
    """A valid ISO-8601 date passes validation and reaches the SF API."""
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.fetch_record.return_value = _CLOSE_DATE_RECORD

    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.set_field.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.set_field.stdin_is_interactive", return_value=False),
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(cli, [_OPP_ID, "CloseDate", "2026-07-26", "--confirm"], catch_exceptions=False)

    assert result.exit_code == 0
    mock_client.update_opportunity_fields.assert_called_once_with(_OPP_ID, {"CloseDate": "2026-07-26"})


def test_closedate_invalid_format_rejected() -> None:
    """An invalid date format (MM/DD/YYYY) raises BadParameter before touching SF.

    Invoked with standalone_mode=False (matching __main__.main()'s own invocation)
    so the raised exception is exactly what __main__.py's UsageError handler maps
    to EXIT_DATA (3) — CliRunner.invoke()'s default standalone_mode=True would
    instead swallow it into Click's own exit code 2.
    """
    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        pytest.raises(click.exceptions.BadParameter, match="not a valid ISO-8601 date"),
    ):
        cli.main([_OPP_ID, "CloseDate", "07/26/2026"], standalone_mode=False)


# ---------------------------------------------------------------------------
# Gross_Margin__c numeric validation (implementation note)
# ---------------------------------------------------------------------------

_QUOTE_ID = "a0e000000000000AAA"
_GROSS_MARGIN_RECORD = {
    "Id": _QUOTE_ID,
    "Name": "Test Quote",
    "Gross_Margin__c": 30.0,
}


def test_gross_margin_valid_number_accepted() -> None:
    """A valid number passes validation and reaches the SF API."""
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.fetch_sobject.return_value = _GROSS_MARGIN_RECORD

    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.set_field.SFDirectClient") as mock_client_cls,
        patch("fieldkit.commands.sf.set_field.stdin_is_interactive", return_value=False),
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = runner.invoke(
            cli,
            [_QUOTE_ID, "Gross_Margin__c", "35.87", "--sobject", "SBQQ__Quote__c", "--confirm"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    mock_client.update_sobject_fields.assert_called_once_with("SBQQ__Quote__c", _QUOTE_ID, {"Gross_Margin__c": "35.87"})


def test_gross_margin_non_numeric_rejected() -> None:
    """A non-numeric string raises BadParameter before touching SF.

    See test_closedate_invalid_format_rejected for why standalone_mode=False
    is used instead of CliRunner.invoke()'s default exit-code assertion.
    """
    with (
        patch("fieldkit.commands.sf.set_field.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.set_field.get_sf_rest_base_url", return_value="https://sf.example.com"),
        pytest.raises(click.exceptions.BadParameter, match="not a valid number"),
    ):
        cli.main(
            [_QUOTE_ID, "Gross_Margin__c", "thirty-five", "--sobject", "SBQQ__Quote__c"],
            standalone_mode=False,
        )


# ---------------------------------------------------------------------------
# Unvalidated fields continue to work unchanged
# ---------------------------------------------------------------------------


def test_unvalidated_field_not_affected() -> None:
    """Fields without a registered validator work as before (no extra checks)."""
    result = _invoke_preview("Some text", field="Next_Steps__c")
    assert result.exit_code == 0
    assert "[preview]" in result.output
