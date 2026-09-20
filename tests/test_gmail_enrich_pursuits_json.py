"""JSON contract tests for ``fieldkit gmail enrich-pursuits``."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_json_reports_written_and_skipped_accounts(tmp_path: Path) -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts_root = tmp_path / "accounts"
    (accounts_root / "acme-corp").mkdir(parents=True)
    with (
        patch.object(ep, "_get_accounts", return_value=["acme-corp", "example-org"]),
        patch.object(ep, "get_accounts_root", return_value=accounts_root),
        patch.object(ep, "build_account_report", side_effect=["report", None]),
    ):
        result = CliRunner().invoke(ep.cli, ["--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload == {
        "accounts": [
            {
                "account": "acme-corp",
                "path": str(accounts_root / "acme-corp" / "gmail-intel.md"),
                "status": "written",
            },
            {"account": "example-org", "path": None, "status": "skipped"},
        ],
        "error": None,
        "requested": None,
    }


def test_json_unknown_account_is_single_error_document() -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    with patch.object(ep, "_get_accounts", return_value=["acme-corp"]):
        result = CliRunner().invoke(ep.cli, ["--account", "missing", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload == {"accounts": [], "error": "account_not_found", "requested": "missing"}


def test_json_failure_identifies_failed_account_after_completed_accounts(tmp_path: Path) -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts_root = tmp_path / "accounts"
    (accounts_root / "acme-corp").mkdir(parents=True)
    with (
        patch.object(ep, "_get_accounts", return_value=["acme-corp", "example-org"]),
        patch.object(ep, "get_accounts_root", return_value=accounts_root),
        patch.object(ep, "build_account_report", side_effect=["report", 3]),
    ):
        result = CliRunner().invoke(ep.cli, ["--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 3
    assert payload["accounts"][-1] == {"account": "example-org", "path": None, "status": "failed"}


def test_report_write_cleans_up_temporary_when_replace_fails(tmp_path: Path) -> None:
    from fieldkit.commands.gmail import enrich_pursuits as ep

    account_root = tmp_path / "acme-corp"
    account_root.mkdir()
    target = account_root / "gmail-intel.md"
    with (
        patch.object(ep, "_get_accounts", return_value=["acme-corp"]),
        patch.object(ep, "get_accounts_root", return_value=tmp_path),
        patch.object(ep, "build_account_report", return_value="report"),
        patch.object(Path, "replace", MagicMock(side_effect=PermissionError("denied"))),
        pytest.raises(PermissionError, match="denied"),
    ):
        CliRunner().invoke(ep.cli, ["--json"], catch_exceptions=False)

    assert not target.exists()
    assert list(account_root.iterdir()) == []
