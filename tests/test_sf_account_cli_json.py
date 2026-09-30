"""Tests for the `--json` branch of `cli` in fieldkit.commands.sf.account.

Targets the machine-readable output path (lines ~561-594), which is not
exercised by tests/test_sf_account.py (that file covers the human-dashboard
path only). CRAP=54.12, complexity=8 on `cli`.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from fieldkit.commands.sf.account import cli
from fieldkit.sf.client import SFDirectClient
from fieldkit.sf.errors import SFAPIError, SFAuthError, SFNotFoundError

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


def _output_text(result: Result) -> str:
    """Combine stdout and stderr defensively across CliRunner mix_stderr modes."""
    stderr = result.stderr if hasattr(result, "stderr") else ""
    return (result.output + stderr).lower()


# ── guard order: unknown account is checked first ──────────────────────────


def test_json_unknown_account_exits_3_without_echoing_configuration() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme", "globalpay"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
    ):
        result = runner.invoke(cli, ["nope", "--json"])

    assert result.exit_code == 3
    text = _output_text(result)
    assert "unknown account" in text
    assert "nope" not in text
    assert "acme" not in text
    assert "globalpay" not in text


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

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    assert "retry" in payload["error"].lower()


@pytest.mark.parametrize(
    ("failure", "expected_exit", "expected_status"),
    [
        (SFNotFoundError("404"), 3, "error"),
        (SFAPIError("timeout"), 1, "partial"),
    ],
)
def test_json_account_record_failure_is_non_success(
    failure: Exception, expected_exit: int, expected_status: str
) -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", _patch_client_context()),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", return_value="001JSON1"),
        patch("fieldkit.commands.sf.account._fetch_account_record", side_effect=failure),
        patch("fieldkit.commands.sf.account._fetch_live_opportunities") as mock_fetch_opps,
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == expected_exit
    payload = json.loads(result.stdout)
    assert payload["status"] == expected_status
    assert '"status": "ok"' not in result.stdout
    mock_fetch_opps.assert_not_called()


def test_json_resolution_api_failure_is_partial_not_no_match() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", _patch_client_context()),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", side_effect=SFAPIError("timeout")),
        patch("fieldkit.commands.sf.account._fetch_account_record") as mock_fetch_record,
        patch("fieldkit.commands.sf.account._fetch_live_opportunities") as mock_fetch_opps,
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "partial"
    mock_fetch_record.assert_not_called()
    mock_fetch_opps.assert_not_called()


def test_json_local_resolution_failure_is_partial_not_no_match() -> None:
    """An incomplete local fallback scan emits a bounded retryable result."""
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", _patch_client_context()),
        patch("fieldkit.commands.sf.account._resolve_sf_account_id", side_effect=OSError("/private/root")),
        patch("fieldkit.commands.sf.account._fetch_account_record") as mock_fetch_record,
        patch("fieldkit.commands.sf.account._fetch_live_opportunities") as mock_fetch_opps,
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "status": "partial",
        "error": "Local pursuit scan failed; retry may help.",
    }
    assert "/private/root" not in result.output
    mock_fetch_record.assert_not_called()
    mock_fetch_opps.assert_not_called()


def test_json_malformed_pursuit_yaml_is_bounded_invalid_data(tmp_path: Path) -> None:
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    broken = pursuit_dir / "broken.md"
    content = "---\nsf_opportunity_id: [private-invalid\n---\nBody.\n"
    broken.write_text(content, encoding="utf-8")
    config = {"accounts": {"acme": {"keywords": [], "pursuit_dir": "accounts/acme/pursuits"}}}
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=config),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fakesid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", _patch_client_context()),
        patch("fieldkit.commands.sf.account._fetch_account_record") as fetch_record,
        patch("fieldkit.commands.sf.account._fetch_live_opportunities") as fetch_opportunities,
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 3
    assert json.loads(result.stdout) == {
        "status": "error",
        "error": "Local pursuit metadata is invalid.",
    }
    assert "private-invalid" not in result.output
    assert str(tmp_path) not in result.output
    fetch_record.assert_not_called()
    fetch_opportunities.assert_not_called()
    assert broken.read_text(encoding="utf-8") == content
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == [broken]


def test_json_malformed_sosl_collection_is_partial_without_dependent_reads(tmp_path: Path) -> None:
    response = MagicMock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = {"searchRecords": ["private-provider-member"]}
    http_client = MagicMock()
    http_client.request.return_value = response
    direct_client = SFDirectClient(session_id="fake-sid", base_url="https://sf.example.com")
    config = {"accounts": {"acme": {"keywords": ["Acme"], "pursuit_dir": "accounts/acme/pursuits"}}}
    runner = CliRunner()
    with (
        patch("fieldkit.commands.sf.account.get_account_names", return_value=["acme"]),
        patch("fieldkit.commands.sf.account.get_accounts_config", return_value=config),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.sf.account.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.account.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.account._sf_direct.SFDirectClient", return_value=direct_client),
        patch("fieldkit.sf.client.httpx.Client", return_value=http_client),
        patch("fieldkit.commands.sf.account._fetch_account_record") as fetch_record,
        patch("fieldkit.commands.sf.account._fetch_live_opportunities") as fetch_opportunities,
    ):
        result = runner.invoke(cli, ["acme", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "partial"
    assert "private-provider-member" not in result.output
    fetch_record.assert_not_called()
    fetch_opportunities.assert_not_called()


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
    assert len(call_args) == 2

    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["account_id"] == "001JSON1"
    assert payload["account_name"] == "Acme Corp"
    assert payload["opportunities"] == open_opps
    assert payload["open_opportunity_count"] == 1


def test_json_no_sf_account_id_is_error_and_skips_dependent_reads() -> None:
    """A completed no-match is invalid data, never a successful empty payload."""
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

    assert result.exit_code == 3, result.output
    mock_fetch_rec.assert_not_called()
    assert json.loads(result.output) == {
        "status": "error",
        "error": "Salesforce account record was not found.",
    }

    assert open_opps
