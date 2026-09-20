"""Tests for the `--json` branch of `cli` in fieldkit.commands.sf.account.

Targets the machine-readable output path (lines ~561-594), which is not
exercised by tests/test_sf_account.py (that file covers the human-dashboard
path only). CRAP=54.12, complexity=8 on `cli`.
"""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.account import cli
from fieldkit.sf.client import SFAPIError, SFAuthError

pytestmark = pytest.mark.unit


def _make_account_record() -> dict[str, Any]:
    return {
        "Id": "001JSON1",
        "Name": "Acme Corp",
        "Industry": "Manufacturing",
        "Account_Segment__c": "Enterprise",
        "Owner": {"Name": "Pat AE", "Email": "pat@example.com"},
    }


def _make_open_opp() -> dict[str, Any]:
    return {
        "opportunity_id": "006JSON1",
        "stage": "Propose",
        "consulting_acv": 250000.0,
        "training_acv": 10000.0,
        "name": "Acme Renewal",
    }


def _output_text(result: Any) -> str:
    """Combine stdout and stderr defensively across CliRunner mix_stderr modes."""
    stderr = result.stderr if hasattr(result, "stderr") else ""
    return (result.output + stderr).lower()


# ── guard order: unknown account is checked first ──────────────────────────


def test_json_unknown_account_exits_3_and_lists_valid_names() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme", "globalpay"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
    ):
        result = runner.invoke(cli, ["nope", "--json"])

    assert result.exit_code == 3
    text = _output_text(result)
    assert "unknown account 'nope'" in text
    assert "acme" in text
    assert "globalpay" in text


# ── guard order: missing session is checked before base url ────────────────


def test_json_no_session_exits_2_with_auth_hint() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value=None),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 2
    text = _output_text(result)
    assert "no salesforce session" in text
    assert "fieldkit auth sf" in text
    assert "--sid" not in text


def test_json_no_base_url_exits_2() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value=None),
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 2
    text = _output_text(result)
    assert "no salesforce base url configured" in text


# ── SFAuthError inside the SFDirectClient context manager ──────────────────


def test_json_sfautherror_logged_and_reraised() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch(
            "fieldkit.commands.sf.account._sf_direct.SFDirectClient",
            side_effect=SFAuthError("session expired"),
        ),
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert isinstance(result.exception, SFAuthError)
    text = _output_text(result)
    assert "authentication failed" in text
    assert "401" in text


def test_json_primary_opportunity_failure_emits_no_success_payload() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", _patch_client_context()),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value="001JSON1"),
        patch("fieldkit.commands.sf.account._fetch_account_record", return_value=_make_account_record()),
        patch("fieldkit.commands.sf.account._fetch_live_opportunities", side_effect=SFAPIError("network timeout")),
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert isinstance(result.exception, SFAPIError)
    assert '"status": "ok"' not in result.output


# ── happy path ──────────────────────────────────────────────────────────────


def _patch_client_context() -> Any:
    """Patch SFDirectClient so `with ... as client:` yields a plain MagicMock."""
    mock_cls = MagicMock()
    instance = mock_cls.return_value
    instance.__enter__.return_value = instance
    instance.__exit__.return_value = False
    return mock_cls


def test_json_happy_path_emits_expected_payload() -> None:
    acct_rec = _make_account_record()
    open_opps = [_make_open_opp()]

    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", _patch_client_context()),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value="001JSON1"),
        patch("fieldkit.commands.sf.account._fetch_account_record", return_value=acct_rec) as mock_fetch_rec,
        patch("fieldkit.commands.sf.account._fetch_live_opportunities", return_value=open_opps),
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 0, result.output
    mock_fetch_rec.assert_called_once()
    call_args = mock_fetch_rec.call_args[0]
    assert call_args[0] == "001JSON1"
    assert call_args[2] == "https://sf.example.com"

    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["account_id"] == "001JSON1"
    assert payload["account_name"] == "Acme Corp"
    assert payload["opportunities"] == open_opps
    assert payload["open_opportunity_count"] == 1


def test_json_happy_path_no_sf_account_id_skips_account_record_fetch() -> None:
    """When _resolve_sf_account_id returns falsy, _fetch_account_record must not run."""
    open_opps = [_make_open_opp()]

    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", _patch_client_context()),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value=None),
        patch("fieldkit.commands.sf.account._fetch_account_record") as mock_fetch_rec,
        patch("fieldkit.commands.sf.account._fetch_live_opportunities", return_value=open_opps),
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 0, result.output
    mock_fetch_rec.assert_not_called()

    payload = json.loads(result.output)
    assert payload["account_id"] is None
    assert payload["account_name"] == "acme"
    assert payload["opportunities"] == open_opps
