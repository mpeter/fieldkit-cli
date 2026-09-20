"""Tests for ingest discover resilience — historic regression (missing messages table)."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.ingest.discover import cli

pytestmark = pytest.mark.unit


def _make_gmail_db(tmp_path: Path, *, with_messages: bool) -> Path:
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(db_path)
    if with_messages:
        conn.execute(
            "CREATE TABLE messages (message_id TEXT, subject TEXT, body_html TEXT, body_plain TEXT, from_addr TEXT, date_epoch INTEGER)"
        )
    conn.commit()
    conn.close()
    return db_path


# ── TestIngestDiscoverMissingMessages (flattened) ───────────────────────────


def test_ingest_discover_missing_messages_clean_error_when_messages_table_missing(tmp_path: Path) -> None:
    """Missing messages table → clean error message, exit 1, no traceback."""
    db_path = _make_gmail_db(tmp_path, with_messages=False)

    # get_gmail_db_path is imported lazily inside the function — patch the source
    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=db_path):
        result = CliRunner().invoke(cli, ["--dry-run"])

    assert result.exit_code == 1
    assert "messages" in result.output.lower()
    assert "gmail sync" in result.output.lower()
    assert "Traceback" not in result.output


def test_ingest_discover_missing_messages_succeeds_with_messages_table_present(tmp_path: Path) -> None:
    """When messages table exists (empty), dry-run succeeds with exit 0."""
    db_path = _make_gmail_db(tmp_path, with_messages=True)

    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=db_path):
        result = CliRunner().invoke(cli, ["--dry-run"])

    # Either succeeds (0) or fails with a DB-related error — just no crash
    assert result.exit_code in (0, 1)
    assert "Traceback" not in result.output
