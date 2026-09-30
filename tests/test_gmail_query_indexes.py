"""Tests for the canonical Gmail schema and published query connection."""

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "fieldkit" / "gmail" / "schema.sql"

EXPECTED_INDEXES = {
    "idx_messages_from_addr",
    "idx_messages_to_addr",
    "idx_messages_cc_addr",
    "idx_people_display_name",
}


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


def _get_index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {row[0] for row in rows}


def test_schema_sql_contains_new_indexes() -> None:
    """schema.sql must declare all 4 new address indexes."""
    conn = sqlite3.connect(":memory:")
    _apply_schema(conn)
    indexes = _get_index_names(conn)
    missing = EXPECTED_INDEXES - indexes
    assert not missing, f"Missing indexes after schema.sql apply: {missing}"


def test_connect_sets_busy_timeout_without_mutating_schema(tmp_path: Path) -> None:
    """connect() sets a 5s timeout and never performs query-time migrations."""
    from fieldkit.gmail.query_domain import connect

    db_path = tmp_path / "gmail.db"
    from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page, initialize_gmail_publication
    from fieldkit.sqlite_publication import SQLiteMutationConnection

    initialize_gmail_publication(db_path)

    def mark_ready(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(db_path, mark_ready)
    before = db_path.read_bytes()

    conn = connect(db_path)
    assert conn.row_factory is sqlite3.Row
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert timeout == 5000
    assert db_path.read_bytes() == before


def test_connect_rejects_unpublished_legacy_schema_without_migrating(tmp_path: Path) -> None:
    """A legacy cache needs explicit import; a read must not ALTER it."""
    from fieldkit.errors import SQLiteSnapshotError
    from fieldkit.gmail.query_domain import connect

    db_path = tmp_path / "gmail.db"
    setup = sqlite3.connect(db_path)
    setup.executescript(
        """
        CREATE TABLE messages (message_id TEXT PRIMARY KEY);
        CREATE TABLE threads (thread_id TEXT PRIMARY KEY);
        CREATE TABLE people (email TEXT PRIMARY KEY);
        """
    )
    setup.commit()
    setup.close()
    before = db_path.read_bytes()

    with pytest.raises(SQLiteSnapshotError, match="explicit import"):
        connect(db_path)

    assert db_path.read_bytes() == before


@pytest.mark.parametrize(
    "column",
    [
        "thread_count",
        "meeting_count",
        "slack_message_count",
        "last_seen",
        "is_internal",
        "account",
    ],
)
def test_query_schema_requires_every_backstory_people_column(column: str) -> None:
    from fieldkit.gmail.exceptions import GmailSchemaError
    from fieldkit.gmail.query_domain import _validate_query_schema

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _apply_schema(connection)
    connection.execute(f"ALTER TABLE people DROP COLUMN {column}")

    with pytest.raises(GmailSchemaError, match=column):
        _validate_query_schema(connection)

    connection.close()
