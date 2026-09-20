"""CLI tests for local driver retry observability and recovery."""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.driver.cli import cli
from fieldkit.driver.retry_state import ResetResult, RetryStatus

pytestmark = pytest.mark.unit


def test_retry_status_json_outputs_local_entries() -> None:
    entries = (RetryStatus("owner/repo#42", "exhausted", 3, 0, "failed", "2026-09-03T00:00:00Z"),)
    with patch("fieldkit.driver.retry_state.retry_status", return_value=entries):
        result = CliRunner().invoke(cli, ["retry", "status", "--json"])

    assert result.exit_code == 0
    assert '"issue_key": "owner/repo#42"' in result.output
    assert '"started_attempts": 3' in result.output


def test_retry_reset_refuses_running_entry() -> None:
    reset = ResetResult(False, "owner/repo#42", "cannot reset a running reservation")
    with (
        patch("fieldkit.commands.driver.cli.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.retry_state.reset_retry", return_value=reset),
    ):
        result = CliRunner().invoke(cli, ["retry", "reset", "--issue", "42", "--reason", "operator review"])

    assert result.exit_code == 3
    assert "running reservation" in result.output


def test_retry_reset_requires_reason_and_returns_audit_result() -> None:
    reset = ResetResult(True, "owner/repo#42", "retry state reset")
    with (
        patch("fieldkit.commands.driver.cli.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.retry_state.reset_retry", return_value=reset) as mock_reset,
    ):
        missing = CliRunner().invoke(cli, ["retry", "reset", "--issue", "42"])
        result = CliRunner().invoke(cli, ["retry", "reset", "--issue", "42", "--reason", "operator review"])

    assert missing.exit_code == 2
    assert result.exit_code == 0
    assert "Reset owner/repo#42" in result.output
    assert mock_reset.call_args.args == ("owner/repo", 42, "operator review")


@pytest.mark.unit
def test_retry_reset_dry_run_reports_intent_without_resetting() -> None:
    with (
        patch("fieldkit.commands.driver.cli.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.retry_state.reset_retry") as mock_reset,
    ):
        result = CliRunner().invoke(
            cli,
            ["retry", "reset", "--issue", "42", "--reason", "operator review", "--dry-run"],
        )

    assert result.exit_code == 0
    assert result.output == "Would attempt to reset retry state for owner/repo#42: operator review\n"
    mock_reset.assert_not_called()


def test_retry_reset_json_reports_applied_result() -> None:
    reset = ResetResult(True, "owner/repo#42", "retry state reset")
    with (
        patch("fieldkit.commands.driver.cli.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.retry_state.reset_retry", return_value=reset),
    ):
        result = CliRunner().invoke(cli, ["retry", "reset", "--issue", "42", "--reason", "operator review", "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload == {"detail": "retry state reset", "dry_run": False, "issue_key": "owner/repo#42", "reset": True}


@pytest.mark.unit
def test_retry_reset_dry_run_rejects_blank_reason() -> None:
    with patch("fieldkit.driver.retry_state.reset_retry") as mock_reset:
        result = CliRunner().invoke(
            cli,
            ["retry", "reset", "--issue", "42", "--reason", "   ", "--dry-run"],
        )

    assert result.exit_code == 3
    assert "Reset reason must not be empty" in result.output
    mock_reset.assert_not_called()
