"""Tests for champion signal query chunking (historic regression).

Verifies that _champion_thread_stats() and query_champion_signals() handle
email lists of any size — including lists exceeding SQLite's 1000-node
expression-tree limit — without raising OperationalError.
"""

import sqlite3

import pytest

from fieldkit.commands.gmail.query import _champion_thread_stats, query_champion_signals

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Minimal in-memory schema matching the real gmail.db tables used by the
# champion signal queries.  Only the columns actually referenced by the
# queries under test are included.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE threads (
    thread_id     TEXT PRIMARY KEY,
    subject       TEXT,
    updated_at    TEXT,
    message_count INTEGER
);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY,
    thread_id   TEXT,
    from_addr   TEXT,
    to_addr     TEXT,
    cc_addr     TEXT,
    date_epoch  INTEGER,
    date_str    TEXT,
    subject     TEXT
);

CREATE TABLE people (
    email         TEXT PRIMARY KEY,
    display_name  TEXT,
    message_count INTEGER DEFAULT 0
);
"""


def _make_conn() -> sqlite3.Connection:
    """Return a fresh in-memory connection with the minimal champion schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# _champion_thread_stats tests
# ---------------------------------------------------------------------------


# ── TestChampionThreadStatsSmallAccount (flattened) ─────────────────────────


def test_champion_thread_stats_small_account_champion_stats_small_account() -> None:
    """3 emails, populated DB — returns correct counts without crash."""
    conn = _make_conn()

    # Seed one thread initiated by alice@example.com  # pii-guard: ignore
    conn.execute(
        "INSERT INTO threads (thread_id, subject, updated_at, message_count) VALUES (?, ?, ?, ?)",
        ("t1", "Hello from Alice", "2025-01-10", 2),
    )
    # First message in thread — from alice (she initiated)
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, cc_addr, date_epoch, date_str, subject) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "t1",
            "alice@example.com",  # pii-guard: ignore
            "bob@example.com",  # pii-guard: ignore
            "",
            1_700_000_000,
            "2025-01-10",
            "Hello from Alice",
        ),  # pii-guard: ignore
    )
    # Reply from bob
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, cc_addr, date_epoch, date_str, subject) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "t1",
            "bob@example.com",  # pii-guard: ignore
            "alice@example.com",  # pii-guard: ignore
            "",
            1_700_001_000,
            "2025-01-11",
            "Re: Hello from Alice",
        ),  # pii-guard: ignore
    )
    conn.commit()

    emails = ["alice@example.com", "alice.work@example.com", "a.smith@example.com"]  # pii-guard: ignore
    initiated, total, sent, last = _champion_thread_stats(conn, emails)

    # alice@example.com initiated t1 and sent 1 message  # pii-guard: ignore
    assert initiated == 1, f"Expected 1 initiated thread, got {initiated}"
    assert total == 1, f"Expected 1 total thread, got {total}"
    assert sent == 1, f"Expected 1 sent message, got {sent}"
    assert last is not None, "Expected a last-sent row"
    assert last["date_epoch"] == 1_700_000_000


# ── TestChampionThreadStatsLargeAccount (flattened) ─────────────────────────


def test_champion_thread_stats_large_account_champion_stats_large_account_no_crash() -> None:
    """200 unique fake email addresses — returns tuple of ints without OperationalError."""
    conn = _make_conn()

    # Seed a thread initiated by the first email in the large list
    conn.execute(
        "INSERT INTO threads (thread_id, subject, updated_at, message_count) VALUES (?, ?, ?, ?)",
        ("t-large", "Large account thread", "2025-03-01", 1),
    )
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, cc_addr, date_epoch, date_str, subject) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("t-large", "user0@acme-corp.com", "other@acme-corp.com", "", 1_710_000_000, "2025-03-01", "Large thread"),
    )
    conn.commit()

    # 200 unique fake email addresses — well above the 50-per-batch threshold
    emails = [f"user{i}@acme-corp.com" for i in range(200)]

    # Must not raise sqlite3.OperationalError
    result = _champion_thread_stats(conn, emails)

    initiated, total, sent, _last = result
    assert isinstance(initiated, int), f"initiated must be int, got {type(initiated)}"
    assert isinstance(total, int), f"total must be int, got {type(total)}"
    assert isinstance(sent, int), f"sent must be int, got {type(sent)}"
    # user0 initiated t-large and sent 1 message
    assert initiated >= 1, "Expected at least 1 initiated thread for user0"
    assert total >= 1, "Expected at least 1 total thread for user0"
    assert sent >= 1, "Expected at least 1 sent message for user0"


# ── TestChampionThreadStatsEmptyEmails (flattened) ──────────────────────────


def test_champion_thread_stats_empty_emails_champion_stats_empty_emails() -> None:
    """Empty list returns (0, 0, 0, None) without crash."""
    conn = _make_conn()
    result = _champion_thread_stats(conn, [])
    assert result == (0, 0, 0, None), f"Expected (0, 0, 0, None), got {result}"


# ---------------------------------------------------------------------------
# query_champion_signals tests
# ---------------------------------------------------------------------------


# ── TestQueryChampionSignalsNoMatch (flattened) ─────────────────────────────


def test_query_champion_signals_no_match_query_champion_signals_no_match() -> None:
    """Name not in people table — returns string containing 'No people matched'."""
    conn = _make_conn()
    result = query_champion_signals(conn, "nonexistent-person-xyz")
    assert "No people matched" in result, f"Expected 'No people matched' in result, got: {result!r}"


# ── TestQueryChampionSignalsLargeMatch (flattened) ──────────────────────────


def test_query_champion_signals_large_match_query_champion_signals_large_match() -> None:
    """Person with 200 email aliases in people table — completes without OperationalError."""
    conn = _make_conn()

    # Insert 200 people rows all with the same display_name so the lookup
    # returns all 200 as the email list for the champion query.
    display = "Big Account Champion"
    conn.executemany(
        "INSERT INTO people (email, display_name) VALUES (?, ?)",
        [(f"champ{i}@acme-corp.com", display) for i in range(200)],
    )

    # Seed a thread initiated by the first alias so the query returns
    # non-trivial counts (exercises the chunked SQL paths fully).
    conn.execute(
        "INSERT INTO threads (thread_id, subject, updated_at, message_count) VALUES (?, ?, ?, ?)",
        ("t-champ", "Champion thread", "2025-04-01", 1),
    )
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, cc_addr, date_epoch, date_str, subject) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "t-champ",
            "champ0@acme-corp.com",
            "other@acme-corp.com",
            "",
            1_712_000_000,
            "2025-04-01",
            "Champion thread",
        ),
    )
    conn.commit()

    # Must not raise sqlite3.OperationalError
    result = query_champion_signals(conn, display)

    assert isinstance(result, str), f"Expected str result, got {type(result)}"
    assert "Champion signal" in result, f"Expected 'Champion signal' header in result, got: {result!r}"
    assert "No people matched" not in result, f"Unexpected 'No people matched' — people were inserted: {result!r}"
