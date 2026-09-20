"""Characterization tests for the MIN(date_epoch) first-message-per-thread subquery.

Verifies that the corrected SQL pattern:

    SELECT thread_id, from_addr, MIN(date_epoch) AS min_epoch
    FROM   messages
    GROUP  BY thread_id

returns the from_addr of the earliest message in each thread.  This replaces
the formerly buggy HAVING date_epoch = MIN(date_epoch) pattern, which was
semantically dead code relying on SQLite's bare-column rule.
"""

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "fieldkit" / "gmail" / "schema.sql"

FIRST_MESSAGE_SQL = """
    SELECT thread_id, from_addr, MIN(date_epoch) AS min_epoch
    FROM   messages
    GROUP  BY thread_id
"""


@pytest.fixture
def db():
    """In-memory SQLite DB with schema and test threads/messages."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())

    # Two threads
    conn.executemany(
        "INSERT INTO threads (thread_id, subject, message_count) VALUES (?, ?, ?)",
        [
            ("thread-a", "Thread A subject", 0),
            ("thread-b", "Thread B subject", 0),
            ("thread-c", "Thread C — single message", 0),
        ],
    )

    # Thread A: 3 messages at different epochs — alice started it
    conn.executemany(
        "INSERT INTO messages (message_id, thread_id, from_addr, date_epoch) VALUES (?, ?, ?, ?)",
        [
            ("msg-a1", "thread-a", "alice@example.com", 1_000),  # earliest  # pii-guard: ignore
            ("msg-a2", "thread-a", "bob@example.com", 2_000),  # pii-guard: ignore
            ("msg-a3", "thread-a", "alice@example.com", 3_000),  # pii-guard: ignore
        ],
    )

    # Thread B: 2 messages — bob started it
    conn.executemany(
        "INSERT INTO messages (message_id, thread_id, from_addr, date_epoch) VALUES (?, ?, ?, ?)",
        [
            ("msg-b1", "thread-b", "bob@example.com", 500),  # earliest  # pii-guard: ignore
            ("msg-b2", "thread-b", "carol@example.com", 1_500),  # pii-guard: ignore
        ],
    )

    # Thread C: single message — carol started (and ended) it
    conn.execute(
        "INSERT INTO messages (message_id, thread_id, from_addr, date_epoch) VALUES (?, ?, ?, ?)",
        ("msg-c1", "thread-c", "carol@example.com", 800),  # pii-guard: ignore
    )

    conn.commit()
    return conn


def _first_senders(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {thread_id: from_addr} for the earliest message in each thread."""
    rows = conn.execute(FIRST_MESSAGE_SQL).fetchall()
    return {r["thread_id"]: r["from_addr"] for r in rows}


# ── TestFirstMessageSubquery (flattened) ────────────────────────────────────


def test_first_message_subquery_multi_message_thread_returns_earliest_sender(db):
    senders = _first_senders(db)
    assert senders["thread-a"] == "alice@example.com", (  # pii-guard: ignore
        "Thread A was started by alice (epoch 1000), not bob (epoch 2000)"
    )


def test_first_message_subquery_second_thread_returns_correct_earliest_sender(db):
    senders = _first_senders(db)
    assert senders["thread-b"] == "bob@example.com", (  # pii-guard: ignore
        "Thread B was started by bob (epoch 500), not carol (epoch 1500)"
    )


def test_first_message_subquery_single_message_thread_returns_that_sender(db):
    senders = _first_senders(db)
    assert senders["thread-c"] == "carol@example.com", (  # pii-guard: ignore
        "Thread C has only one message from carol"
    )  # pii-guard: ignore


def test_first_message_subquery_all_three_threads_present(db):
    senders = _first_senders(db)
    assert set(senders.keys()) == {"thread-a", "thread-b", "thread-c"}


def test_first_message_subquery_min_epoch_matches_expected_earliest(db):
    rows = db.execute(FIRST_MESSAGE_SQL).fetchall()
    by_thread = {r["thread_id"]: r["min_epoch"] for r in rows}
    assert by_thread["thread-a"] == 1_000
    assert by_thread["thread-b"] == 500
    assert by_thread["thread-c"] == 800


def test_first_message_subquery_sender_filter_via_join(db):
    """Simulate the query.py pattern: join first-sender subquery to filter by from_addr."""
    sql = f"""
            SELECT COUNT(DISTINCT t.thread_id) as cnt
            FROM   threads t
            JOIN   ({FIRST_MESSAGE_SQL.strip()}) m ON m.thread_id = t.thread_id
            WHERE  m.from_addr LIKE ?
        """
    # alice initiated 1 thread
    row = db.execute(sql, ("%alice%",)).fetchone()
    assert row["cnt"] == 1

    # bob initiated 1 thread
    row = db.execute(sql, ("%bob%",)).fetchone()
    assert row["cnt"] == 1

    # carol initiated 1 thread
    row = db.execute(sql, ("%carol%",)).fetchone()
    assert row["cnt"] == 1


def test_first_message_subquery_old_having_pattern_is_absent_from_query_py():
    """Regression guard: the buggy HAVING date_epoch = MIN(date_epoch) must not exist."""
    query_src = (
        Path(__file__).resolve().parents[1] / "src" / "fieldkit" / "commands" / "gmail" / "query.py"
    ).read_text()
    assert "HAVING date_epoch = MIN(date_epoch)" not in query_src, (
        "Buggy HAVING pattern found in query.py — fix was reverted!"
    )
