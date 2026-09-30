"""JSON contract tests for ``fieldkit gmail sync``."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.sync_command import cli
from fieldkit.gmail.batch import SyncSummary
from fieldkit.gmail.sync_engine import PublishedSyncResult

pytestmark = pytest.mark.unit


def test_json_reports_incremental_partial_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "gmail.db"
    with (
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        patch(
            "fieldkit.gmail.sync_engine.run_published_sync",
            return_value=PublishedSyncResult(
                "incremental", SyncSummary(added=4, unresolved=1), "last_history_id", "99999"
            ),
        ),
    ):
        result = CliRunner().invoke(cli, ["--db", str(db_path), "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload == {
        "added": 4,
        "failed": 1,
        "mode": "incremental",
        "not_found": 0,
        "partial": True,
        "retry": {"checkpoint": "99999", "checkpoint_key": "last_history_id", "required": True},
        "unresolved": 1,
    }
    assert str(tmp_path) not in result.output


def test_success_output_and_logs_do_not_expose_database_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db_path = tmp_path / "gmail.db"
    caplog.set_level(logging.INFO, logger="gmail-sync")
    with (
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        patch(
            "fieldkit.gmail.sync_engine.run_published_sync",
            return_value=PublishedSyncResult("full", SyncSummary(added=1)),
        ),
    ):
        result = CliRunner().invoke(cli, ["--db", str(db_path), "--json"])

    assert result.exit_code == 0
    assert str(tmp_path) not in result.output
    assert str(tmp_path) not in caplog.text
    assert json.loads(result.output)["partial"] is False
