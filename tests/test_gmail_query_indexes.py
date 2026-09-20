"""Tests that schema.sql, _ensure_indexes(), and _ensure_schema() work correctly."""

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


def test_ensure_indexes_idempotent_on_fresh_db() -> None:
    """_ensure_indexes() must be a no-op (no error) on a freshly-created DB."""
    from fieldkit.commands.gmail.query import _ensure_indexes

    conn = sqlite3.connect(":memory:")
    _apply_schema(conn)
    # calling twice must not raise
    _ensure_indexes(conn)
    _ensure_indexes(conn)
    indexes = _get_index_names(conn)
    assert EXPECTED_INDEXES.issubset(indexes)


def test_ensure_indexes_adds_missing_indexes() -> None:
    """_ensure_indexes() must create indexes that schema.sql didn't apply (legacy DB)."""
    from fieldkit.commands.gmail.query import _ensure_indexes

    # Build a DB with only the original schema (no new indexes)
    conn = sqlite3.connect(":memory:")
    # Minimal tables needed to satisfy the FK/index constraints
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id  TEXT PRIMARY KEY,
            thread_id   TEXT,
            date_epoch  INTEGER,
            from_addr   TEXT,
            to_addr     TEXT,
            cc_addr     TEXT,
            subject     TEXT,
            snippet     TEXT,
            synced_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS people (
            email        TEXT PRIMARY KEY,
            display_name TEXT
        );
        """
    )
    # Confirm none of the new indexes exist yet
    before = _get_index_names(conn)
    assert not EXPECTED_INDEXES.intersection(before), f"Indexes already present before _ensure_indexes: {before}"

    _ensure_indexes(conn)

    after = _get_index_names(conn)
    missing = EXPECTED_INDEXES - after
    assert not missing, f"Missing indexes after _ensure_indexes: {missing}"


# ---------------------------------------------------------------------------
# _ensure_schema — body_plain column migration (historic regression)
# ---------------------------------------------------------------------------

_LEGACY_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT,
    date_epoch  INTEGER,
    from_addr   TEXT,
    to_addr     TEXT,
    cc_addr     TEXT,
    subject     TEXT,
    snippet     TEXT,
    synced_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for *table* in *conn*."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def test_ensure_schema_adds_body_plain_to_legacy_db() -> None:
    """_ensure_schema() must add body_plain to a DB that lacks it (historic regression)."""
    from fieldkit.commands.gmail.query import _ensure_schema

    conn = sqlite3.connect(":memory:")
    conn.executescript(_LEGACY_MESSAGES_DDL)

    # Confirm body_plain is absent before migration
    before = _column_names(conn, "messages")
    assert "body_plain" not in before, f"body_plain already present before migration: {before}"

    _ensure_schema(conn)

    after = _column_names(conn, "messages")
    assert "body_plain" in after, f"body_plain missing after _ensure_schema: {after}"


def test_ensure_schema_idempotent_when_column_exists() -> None:
    """_ensure_schema() must not raise when body_plain already exists."""
    from fieldkit.commands.gmail.query import _ensure_schema

    conn = sqlite3.connect(":memory:")
    # Create messages table WITH body_plain already present (modern schema)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id  TEXT PRIMARY KEY,
            thread_id   TEXT,
            date_epoch  INTEGER,
            from_addr   TEXT,
            to_addr     TEXT,
            cc_addr     TEXT,
            subject     TEXT,
            snippet     TEXT,
            body_plain  TEXT,
            synced_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )

    # Must not raise even though the column already exists
    _ensure_schema(conn)
    _ensure_schema(conn)

    after = _column_names(conn, "messages")
    assert "body_plain" in after


def test_connect_sets_busy_timeout(tmp_path: Path) -> None:
    """connect() must set a 5s busy_timeout on the returned connection (historic regression)."""
    from fieldkit.gmail.query_domain import connect

    db_path = tmp_path / "gmail.db"
    setup = sqlite3.connect(str(db_path))
    _apply_schema(setup)
    setup.commit()
    setup.close()

    conn = connect(db_path)
    assert conn.row_factory is sqlite3.Row
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert timeout == 5000


def test_connect_read_only_skips_schema_and_index_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent query callers must never run migrations on their worker threads."""
    from fieldkit.gmail import query_domain

    db_path = tmp_path / "gmail.db"
    setup = sqlite3.connect(str(db_path))
    _apply_schema(setup)
    setup.commit()
    setup.close()
    monkeypatch.setattr(query_domain, "_ensure_indexes", lambda _conn: pytest.fail("index update attempted"))
    monkeypatch.setattr(query_domain, "_ensure_schema", lambda _conn: pytest.fail("schema update attempted"))

    conn = query_domain.connect_read_only(db_path)
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()

    assert timeout == 5000
