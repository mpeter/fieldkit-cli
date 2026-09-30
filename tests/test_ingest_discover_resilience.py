"""Tests for ingest discover resilience — historic regression (missing messages table)."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.__main__ import main
from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page, initialize_gmail_publication
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit


def _make_malformed_gmail_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(db_path)
    conn.commit()
    conn.close()
    return db_path


# ── TestIngestDiscoverMissingMessages (flattened) ───────────────────────────


def test_ingest_discover_missing_messages_clean_error_when_messages_table_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unmanaged malformed cache is rejected before registry creation."""
    db_path = _make_malformed_gmail_db(tmp_path)
    pipeline_db = tmp_path / "pipeline.db"

    with (
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
    ):
        result = main(["ingest", "discover", "--pipeline", "transcript-ingest"])

    assert result == 3
    assert not pipeline_db.exists()
    captured = capsys.readouterr()
    assert "SQLite snapshot could not be verified" in captured.err
    assert str(tmp_path) not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


def test_ingest_discover_missing_messages_succeeds_with_messages_table_present(tmp_path: Path) -> None:
    """A query-ready managed publication supports an empty dry-run."""
    db_path = tmp_path / "gmail.db"
    initialize_gmail_publication(db_path)

    def mark_ready(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(db_path, mark_ready)

    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=db_path):
        result = main(["ingest", "discover", "--pipeline", "transcript-ingest", "--dry-run"])

    assert result == 0


def test_ingest_discover_invalid_pipeline_database_is_data_error_without_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pipeline_db = tmp_path / "pipeline.db"
    private_marker = "fictional-private-database-payload"
    pipeline_db.write_text(private_marker, encoding="utf-8")

    with (
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
    ):
        result = main(["ingest", "discover", "--pipeline", "transcript-ingest"])

    captured = capsys.readouterr()
    assert result == 3
    assert private_marker not in captured.out + captured.err
    assert str(tmp_path) not in captured.out + captured.err
    assert "SQLite snapshot could not be verified" in captured.err
