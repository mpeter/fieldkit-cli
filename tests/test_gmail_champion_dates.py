"""Tests for gmail champion date rendering — historic regression (updated_at vs date_epoch)."""

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _make_champion_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a DB where updated_at and MIN(date_epoch) disagree.

    Thread 't1' has:
    - updated_at = '2026-01-15' (sync timestamp, recent)
    - actual first message date_epoch = 2022-03-10 (old email)

    This lets us assert the fix uses epoch not updated_at.
    """
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.executescript(  # pii-guard: ignore
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            message_count INTEGER,
            updated_at TEXT
        );
        CREATE TABLE messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_epoch INTEGER,
            subject TEXT,
            body_plain TEXT,
            body_html TEXT,
            date_str TEXT
        );
        CREATE TABLE thread_accounts (
            thread_id TEXT,
            account TEXT
        );

        -- Thread where sync date (2026) != actual email date (2022)
        INSERT INTO threads VALUES ('t1', 'Old email thread', 1, '2026-01-15 00:00:00');
        INSERT INTO messages VALUES (
            'm1', 't1', 'champion@acme-corp.com', 'me@example.com', '',
            1647129600, 'Old email thread', 'body', '', '2022-03-13'
        );
        INSERT INTO thread_accounts VALUES ('t1', 'acme-corp');
        """
    )
    conn.commit()
    return conn


# ── TestChampionInitiatedDate (flattened) ───────────────────────────────────


def test_champion_initiated_date_initiated_sql_uses_epoch_not_updated_at(tmp_path: Path) -> None:
    """The SQL query for initiated threads must produce epoch-derived date, not updated_at.

    Tests the SQL directly by running the changed query against a controlled DB
    where updated_at (2026) and MIN(date_epoch) (2022) disagree.
    """
    conn = _make_champion_db(tmp_path)
    email = "champion@acme-corp.com"
    batch_like = [f"%{email}%"]
    batch_from = "m.from_addr LIKE ?"

    try:
        # This is the fixed SQL from query.py (historic regression)
        sql = f"""
                SELECT t.subject,
                       datetime(MIN(m2.date_epoch), 'unixepoch') AS first_sent_at,
                       MIN(m2.date_epoch) AS first_epoch
                FROM   threads t
                JOIN   (
                           SELECT thread_id, from_addr, MIN(date_epoch) AS min_epoch
                           FROM   messages
                           GROUP  BY thread_id
                       ) m ON m.thread_id = t.thread_id
                JOIN   messages m2 ON m2.thread_id = t.thread_id
                WHERE  ({batch_from})
                GROUP  BY t.thread_id, t.subject
                ORDER  BY MIN(m2.date_epoch) DESC
                LIMIT  5
            """
        rows = conn.execute(sql, batch_like).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    # epoch 1647129600 → 2022-03-13
    assert row["first_sent_at"].startswith("2022-03"), (
        f"Expected epoch-derived date starting with '2022-03', got: {row['first_sent_at']!r}"
    )
    # updated_at in the threads table was '2026-01-15' — must NOT appear as the date
    assert not row["first_sent_at"].startswith("2026"), (
        f"Got sync-timestamp date in output — historic regression not fixed: {row['first_sent_at']!r}"
    )


def test_champion_initiated_date_old_sql_would_use_wrong_date(tmp_path: Path) -> None:
    """Regression check: the OLD query (using updated_at) would return 2026, not 2022."""
    conn = _make_champion_db(tmp_path)
    try:
        old_sql = """
                SELECT t.subject, t.updated_at
                FROM   threads t
                JOIN   (
                           SELECT thread_id, from_addr, MIN(date_epoch) AS min_epoch
                           FROM   messages
                           GROUP  BY thread_id
                       ) m ON m.thread_id = t.thread_id
                WHERE  m.from_addr LIKE ?
                ORDER  BY t.updated_at DESC
                LIMIT  5
            """
        rows = conn.execute(old_sql, ["%champion@acme-corp.com%"]).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    # The old query returns the sync date (wrong)
    assert rows[0]["updated_at"].startswith("2026"), "Test setup incorrect: updated_at should be 2026"
