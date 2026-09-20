"""Tests for the `fieldkit watch status` command."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.watch.cli import cli

pytestmark = pytest.mark.unit

runner = CliRunner()


def test_status_no_runs_recorded_yet() -> None:
    with patch("fieldkit.watch.status.load_all_statuses", return_value={}):
        result = runner.invoke(cli, ["status"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No watcher runs recorded yet" in result.output


def test_status_renders_table_of_watchers() -> None:
    statuses = {
        "backstory-health": {"outcome": "ok", "last_run": "2026-07-20T06:00:00Z"},
        "pursuit-stalls": {"outcome": "fatal", "last_run": "2026-07-19T06:00:00Z"},
    }
    with patch("fieldkit.watch.status.load_all_statuses", return_value=statuses):
        result = runner.invoke(cli, ["status"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "backstory-health" in result.output
    assert "ok" in result.output
    assert "pursuit-stalls" in result.output
    assert "fatal" in result.output


def test_status_help_exits_0() -> None:
    result = runner.invoke(cli, ["status", "--help"], catch_exceptions=False)
    assert result.exit_code == 0


def test_watch_help_lists_run_and_status(tmp_path: Path) -> None:
    result = runner.invoke(cli, ["--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "run" in result.output
    assert "status" in result.output
    assert "logs" in result.output


def test_run_help_lists_watchers() -> None:
    result = runner.invoke(cli, ["run", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    for name in [
        "backstory-health",
        "close-date-countdown",
        "contract-expiry",
        "draft-queue",
        "pursuit-stalls",
        "slack-threads",
        "waiting-on-tracker",
    ]:
        assert name in result.output
