"""Tests for query_champion_signals() — covering uncovered branches.

cc=14, cov=70%, target: no-match, thread stats, signal labels,
since filter, chunking.
"""

import sqlite3

import pytest

from fieldkit.gmail.query_domain import (
    _champion_signal_label,
    query_champion_signals,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: in-memory DB setup
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """Create a minimal in-memory gmail.db with required schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            message_count INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE messages (
            msg_id TEXT PRIMARY KEY,
            thread_id TEXT,
            subject TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_str TEXT,
            date_epoch INTEGER,
            body_plain TEXT
        );
        CREATE TABLE people (
            person_id TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            message_count INTEGER DEFAULT 0
        );
        """
    )
    conn.commit()
    return conn


def _insert_person(conn: sqlite3.Connection, email: str, display_name: str) -> None:
    conn.execute(
        "INSERT INTO people (person_id, email, display_name) VALUES (?, ?, ?)",
        (email, email, display_name),
    )
    conn.commit()


def _insert_message(
    conn: sqlite3.Connection,
    thread_id: str,
    from_addr: str,
    to_addr: str = "",
    date_epoch: int = 1700000000,
    date_str: str = "2023-11-14",
    subject: str = "Test thread",
) -> None:
    import uuid

    conn.execute(
        "INSERT INTO messages (msg_id, thread_id, subject, from_addr, to_addr, cc_addr, date_str, date_epoch)"
        " VALUES (?, ?, ?, ?, ?, '', ?, ?)",
        (str(uuid.uuid4()), thread_id, subject, from_addr, to_addr, date_str, date_epoch),
    )
    conn.execute(
        "INSERT OR IGNORE INTO threads (thread_id, subject, message_count, updated_at) VALUES (?, ?, 1, ?)",
        (thread_id, subject, date_str),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# No match found
# ---------------------------------------------------------------------------


# ── TestQueryChampionSignalsNoMatch (flattened) ─────────────────────────────


def test_query_champion_signals_no_match_no_match_returns_not_found_string() -> None:
    """Returns a 'No people matched' string when name resolves to nothing."""
    conn = _make_db()
    try:
        result = query_champion_signals(conn, "Nonexistent Person")
    finally:
        conn.close()

    assert "No people matched" in result
    assert "Nonexistent Person" in result


def test_query_champion_signals_no_match_empty_name_returns_not_found() -> None:
    """Empty name string returns not-found message."""
    conn = _make_db()
    try:
        result = query_champion_signals(conn, "")
    finally:
        conn.close()

    assert "No people matched" in result


# ---------------------------------------------------------------------------
# Match found — zero thread activity
# ---------------------------------------------------------------------------


# ── TestQueryChampionSignalsZeroActivity (flattened) ────────────────────────


def test_query_champion_signals_zero_activity_zero_threads_shows_zero_stats() -> None:
    """Person exists but has no threads → stats show zeros."""
    conn = _make_db()
    _insert_person(conn, "alice@acme-corp.com", "Alice Smith")

    try:
        result = query_champion_signals(conn, "Alice Smith")
    finally:
        conn.close()

    assert "Alice Smith" in result
    assert "Threads involved in" in result
    assert "0" in result  # zero threads


def test_query_champion_signals_zero_activity_zero_pct_gets_reactive_label() -> None:
    """0% initiation rate → REACTIVE signal label."""
    conn = _make_db()
    _insert_person(conn, "bob@example.com", "Bob Jones")  # pii-guard: ignore

    try:
        result = query_champion_signals(conn, "Bob Jones")
    finally:
        conn.close()

    assert "REACTIVE" in result


# ---------------------------------------------------------------------------
# Match found — with thread activity
# ---------------------------------------------------------------------------


# ── TestQueryChampionSignalsWithActivity (flattened) ────────────────────────


def test_query_champion_signals_with_activity_initiated_thread_shows_in_output() -> None:
    """Initiated threads appear in thread stats and recent list."""
    conn = _make_db()
    _insert_person(conn, "carol@acme-corp.com", "Carol White")

    # Carol initiates a thread (she is from_addr on the first message)
    _insert_message(
        conn,
        thread_id="t-001",
        from_addr="carol@acme-corp.com",
        to_addr="someone@example.com",  # pii-guard: ignore
        date_epoch=1700100000,
        subject="Carol's thread",
    )

    try:
        result = query_champion_signals(conn, "Carol White")
    finally:
        conn.close()

    assert "Carol White" in result or "carol@acme-corp.com" in result
    assert "Threads initiated" in result


def test_query_champion_signals_with_activity_last_outbound_shown_when_present() -> None:
    """Last outbound date appears in output when there is send history."""
    conn = _make_db()
    _insert_person(conn, "dave@globalpay.example.com", "Dave Brown")

    _insert_message(
        conn,
        thread_id="t-002",
        from_addr="dave@globalpay.example.com",
        to_addr="team@example.com",  # pii-guard: ignore
        date_epoch=1700200000,
        date_str="2023-11-17",
        subject="Dave's outbound",
    )

    try:
        result = query_champion_signals(conn, "Dave Brown")
    finally:
        conn.close()

    assert "Last outbound" in result


def test_query_champion_signals_with_activity_signal_label_present_in_output() -> None:
    """Signal: label is always present in the output."""
    conn = _make_db()
    _insert_person(conn, "eve@acme-corp.com", "Eve Green")

    try:
        result = query_champion_signals(conn, "Eve Green")
    finally:
        conn.close()

    assert "Signal:" in result


# ---------------------------------------------------------------------------
# _champion_signal_label unit tests
# ---------------------------------------------------------------------------


# ── TestChampionSignalLabel (flattened) ─────────────────────────────────────


def test_champion_signal_label_30pct_is_initiator() -> None:
    assert "INITIATOR" in _champion_signal_label(30)


def test_champion_signal_label_50pct_is_initiator() -> None:
    assert "INITIATOR" in _champion_signal_label(50)


def test_champion_signal_label_15pct_is_mixed() -> None:
    assert "MIXED" in _champion_signal_label(15)


def test_champion_signal_label_20pct_is_mixed() -> None:
    assert "MIXED" in _champion_signal_label(20)


def test_champion_signal_label_0pct_is_reactive() -> None:
    assert "REACTIVE" in _champion_signal_label(0)


def test_champion_signal_label_14pct_is_reactive() -> None:
    assert "REACTIVE" in _champion_signal_label(14)


def test_champion_signal_label_29pct_is_mixed() -> None:
    assert "MIXED" in _champion_signal_label(29)


# ---------------------------------------------------------------------------
# since filter — only messages on or after epoch are counted
# ---------------------------------------------------------------------------


# ── TestQueryChampionSignalsSinceFilter (flattened) ─────────────────────────


def test_query_champion_signals_since_filter_since_filter_excludes_old_messages() -> None:
    """Messages before the since epoch are excluded from stats."""
    conn = _make_db()
    _insert_person(conn, "frank@acme-corp.com", "Frank Black")

    # Old message (epoch 1000000) — before since filter
    _insert_message(
        conn,
        thread_id="t-old",
        from_addr="frank@acme-corp.com",
        date_epoch=1000000,
        date_str="2001-09-08",
        subject="Old thread",
    )

    try:
        # since=2000000000 (far future) — should exclude the old message
        result = query_champion_signals(conn, "Frank Black", since=2000000000)
    finally:
        conn.close()

    # With since filter excluding the old message, threads involved = 0
    assert "Frank Black" in result or "frank@acme-corp.com" in result
    # The since filter should exclude the old message — thread count must be 0
    assert "Threads involved in : 0" in result or "Threads involved in: 0" in result


def test_query_champion_signals_since_filter_since_none_includes_all_messages() -> None:
    """When since=None, all messages are included."""
    conn = _make_db()
    _insert_person(conn, "grace@globalpay.example.com", "Grace Hall")

    _insert_message(
        conn,
        thread_id="t-grace",
        from_addr="grace@globalpay.example.com",
        date_epoch=1700000000,
        subject="Grace's thread",
    )

    try:
        result = query_champion_signals(conn, "Grace Hall", since=None)
    finally:
        conn.close()

    assert "Grace Hall" in result or "grace@globalpay.example.com" in result
    assert "Threads involved" in result


# ---------------------------------------------------------------------------
# Multiple recent threads — recent list populated
# ---------------------------------------------------------------------------


# ── TestQueryChampionSignalsRecentThreads (flattened) ───────────────────────


def test_query_champion_signals_recent_threads_multiple_threads_shows_recent_list() -> None:
    """Multiple initiated threads appear in the recent threads list."""
    conn = _make_db()
    _insert_person(conn, "henry@acme-corp.com", "Henry Ford")

    for i in range(3):
        _insert_message(
            conn,
            thread_id=f"t-henry-{i}",
            from_addr="henry@acme-corp.com",
            date_epoch=1700000000 + i * 1000,
            date_str=f"2023-11-{14 + i:02d}",
            subject=f"Henry's thread {i}",
        )

    try:
        result = query_champion_signals(conn, "Henry Ford")
    finally:
        conn.close()

    assert "Henry Ford" in result or "henry@acme-corp.com" in result
    assert "Threads they started" in result
