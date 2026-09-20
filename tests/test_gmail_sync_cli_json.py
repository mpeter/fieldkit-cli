"""JSON contract tests for ``fieldkit gmail sync``."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.sync_command import cli
from fieldkit.gmail.batch import SyncSummary

pytestmark = pytest.mark.unit


def test_json_reports_incremental_partial_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "gmail.db"
    with (
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        patch("fieldkit.gmail.sync_engine.sync_labels"),
        patch("fieldkit.gmail.sync_engine.incremental_sync", return_value=SyncSummary(added=4, unresolved=1)),
        patch("fieldkit.gmail.sync_engine.db_init", return_value=MagicMock()),
        patch(
            "fieldkit.gmail.sync_engine._sync_get",
            side_effect=lambda conn, key: "true" if key == "initial_sync_complete" else "99999",
        ),
    ):
        result = CliRunner().invoke(cli, ["--db", str(db_path), "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload == {
        "added": 4,
        "database": str(db_path.resolve()),
        "failed": 1,
        "mode": "incremental",
        "not_found": 0,
        "partial": True,
        "retry": {"checkpoint": "99999", "checkpoint_key": "last_history_id", "required": True},
        "unresolved": 1,
    }
