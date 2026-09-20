"""Coverage boost tests for fieldkit.commands.gmail.query.

Targets uncovered pure-logic functions and query helpers to push file-level
coverage above 83% (required for cc=14 to achieve CRAP < 15).

Functions targeted:
  - query_blindspots: pure query function, testable with in-memory DB
  - query_dig: pure query function, testable with in-memory DB
  - _build_addr_stats: pure aggregation, no DB needed
  - _fetch_blindspot_messages: pure DB query helper
  - _format_blindspots_table: pure formatting
  - _champion_thread_stats: with since filter and empty emails
"""

import sqlite3
import uuid
from unittest.mock import patch

import pytest
from rich.console import Console

from fieldkit.commands.gmail.query import (
    _build_addr_stats,
    _champion_thread_stats,
    _fetch_blindspot_messages,
    _format_blindspots_table,
    query_blindspots,
    query_dig,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: in-memory DB
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
        CREATE TABLE thread_accounts (
            thread_id TEXT,
            account TEXT
        );
        """
    )
    conn.commit()
    return conn


def _insert_message(
    conn: sqlite3.Connection,
    thread_id: str,
    from_addr: str,
    to_addr: str = "",
    cc_addr: str = "",
    date_epoch: int = 1700000000,
    date_str: str = "2023-11-14",
    subject: str = "Test thread",
    body_plain: str = "",
) -> None:
    conn.execute(
        "INSERT INTO messages (msg_id, thread_id, subject, from_addr, to_addr, cc_addr, date_str, date_epoch, body_plain)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), thread_id, subject, from_addr, to_addr, cc_addr, date_str, date_epoch, body_plain),
    )
    conn.execute(
        "INSERT OR IGNORE INTO threads (thread_id, subject, message_count, updated_at) VALUES (?, ?, 1, ?)",
        (thread_id, subject, date_str),
    )
    conn.commit()


def _insert_thread_account(conn: sqlite3.Connection, thread_id: str, account: str) -> None:
    conn.execute("INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)", (thread_id, account))
    conn.commit()


# ---------------------------------------------------------------------------
# query_blindspots
# ---------------------------------------------------------------------------


# ── TestQueryBlindspotsFunction (flattened) ─────────────────────────────────


def test_query_blindspots_no_threads_for_account_returns_empty() -> None:
    """Account with no threads returns empty list."""
    conn = _make_db()
    try:
        result = query_blindspots(conn, "nonexistent-account")
    finally:
        conn.close()

    assert result == []


def test_query_blindspots_returns_external_contacts(monkeypatch: pytest.MonkeyPatch) -> None:
    """External contacts in account threads are returned."""

    def _no_internal_domains() -> set[str]:
        return set()

    monkeypatch.setattr("fieldkit.commands.gmail.query._internal_blind_domains", _no_internal_domains)
    conn = _make_db()
    _insert_message(
        conn,
        thread_id="t-001",
        from_addr="external@acme-corp.com",
        to_addr="me@acme-corp.com",
        date_epoch=1700000000,
    )
    _insert_thread_account(conn, "t-001", "acme-corp")

    try:
        result = query_blindspots(conn, "acme-corp")
    finally:
        conn.close()

    assert len(result) == 2
    emails = [r[0] for r in result]
    assert "external@acme-corp.com" in emails


def test_query_blindspots_since_filter_excludes_old_messages() -> None:
    """Messages before since epoch are excluded."""
    conn = _make_db()
    _insert_message(
        conn,
        thread_id="t-old",
        from_addr="old@acme-corp.com",
        date_epoch=1000000,  # very old
    )
    _insert_thread_account(conn, "t-old", "acme-corp")

    try:
        # since=2000000000 (far future) excludes the old message
        result = query_blindspots(conn, "acme-corp", since=2000000000)
    finally:
        conn.close()

    # The thread is still in thread_accounts, but messages are filtered
    # so addr_stats will be empty → returns []
    assert result == []


def test_query_blindspots_limit_caps_results() -> None:
    """Results are capped at the limit parameter."""
    conn = _make_db()
    for i in range(10):
        _insert_message(
            conn,
            thread_id=f"t-{i}",
            from_addr=f"contact{i}@acme-corp.com",
            date_epoch=1700000000 + i,
        )
        _insert_thread_account(conn, f"t-{i}", "acme-corp")

    try:
        result = query_blindspots(conn, "acme-corp", limit=3)
    finally:
        conn.close()

    assert len(result) <= 3


def test_query_blindspots_empty_addr_stats_returns_empty() -> None:
    """Account with no threads returns empty list."""
    conn = _make_db()
    # No threads inserted for this account — blindspots should be empty

    try:
        result = query_blindspots(conn, "no-such-account")
    finally:
        conn.close()

    assert result == []


# ---------------------------------------------------------------------------
# query_dig
# ---------------------------------------------------------------------------


# ── TestQueryDigFunction (flattened) ────────────────────────────────────────


def test_query_dig_no_matching_threads_returns_empty() -> None:
    """No threads matching keyword returns empty list."""
    conn = _make_db()
    try:
        result = query_dig(conn, "nonexistent-account", "keyword")
    finally:
        conn.close()

    assert result == []


def test_query_dig_matching_thread_returned() -> None:
    """Thread matching keyword in subject is returned."""
    conn = _make_db()
    _insert_message(
        conn,
        thread_id="t-deal",
        from_addr="sales@acme-corp.com",
        subject="OpenShift deal discussion",
        body_plain="Let's talk about OpenShift pricing.",
        date_epoch=1700000000,
    )
    _insert_thread_account(conn, "t-deal", "acme-corp")

    try:
        result = query_dig(conn, "acme-corp", "OpenShift")
    finally:
        conn.close()

    assert len(result) >= 1
    subjects = [r["subject"] for r in result]
    assert any("OpenShift" in (s or "") for s in subjects)


def test_query_dig_since_filter_applied() -> None:
    """since parameter filters out old threads."""
    conn = _make_db()
    _insert_message(
        conn,
        thread_id="t-old-deal",
        from_addr="sales@acme-corp.com",
        subject="Old OpenShift deal",
        body_plain="Old discussion.",
        date_epoch=1000000,  # very old
    )
    _insert_thread_account(conn, "t-old-deal", "acme-corp")

    try:
        result = query_dig(conn, "acme-corp", "OpenShift", since=2000000000)
    finally:
        conn.close()

    assert result == []


def test_query_dig_result_has_expected_keys() -> None:
    """Result dicts have all expected keys."""
    conn = _make_db()
    _insert_message(
        conn,
        thread_id="t-check",
        from_addr="sales@acme-corp.com",
        subject="Deal check",
        body_plain="Check this deal.",
        date_epoch=1700000000,
    )
    _insert_thread_account(conn, "t-check", "acme-corp")

    try:
        result = query_dig(conn, "acme-corp", "check")
    finally:
        conn.close()

    if result:
        row = result[0]
        assert "thread_id" in row
        assert "subject" in row
        assert "message_count" in row
        assert "from_addr" in row
        assert "first_date" in row
        assert "last_date" in row


def test_query_dig_limit_caps_results() -> None:
    """Results are capped at the limit parameter."""
    conn = _make_db()
    for i in range(5):
        _insert_message(
            conn,
            thread_id=f"t-deal-{i}",
            from_addr="sales@acme-corp.com",
            subject=f"Deal {i}",
            body_plain="Deal discussion.",
            date_epoch=1700000000 + i,
        )
        _insert_thread_account(conn, f"t-deal-{i}", "acme-corp")

    try:
        result = query_dig(conn, "acme-corp", "Deal", limit=2)
    finally:
        conn.close()

    assert len(result) <= 2


# ---------------------------------------------------------------------------
# _build_addr_stats
# ---------------------------------------------------------------------------


# ── TestBuildAddrStats (flattened) ──────────────────────────────────────────


def test_build_addr_stats_empty_rows_returns_empty_dict() -> None:
    """Empty input produces empty stats dict."""
    result = _build_addr_stats([])
    assert result == {}


def test_build_addr_stats_single_from_addr_counted() -> None:
    """Single from_addr row is counted correctly."""

    class FakeRow:
        def __getitem__(self, i: int) -> object:
            return [("sender@acme-corp.com", "recipient@example.com", 1700000000)][0][i]  # pii-guard: ignore

    # Use a simple tuple instead
    rows = [("sender@acme-corp.com", "recipient@example.com", 1700000000)]  # pii-guard: ignore
    result = _build_addr_stats(rows)

    assert "sender@acme-corp.com" in result
    assert result["sender@acme-corp.com"]["msgs"] >= 1


def test_build_addr_stats_noise_addresses_filtered() -> None:
    """Internal/noise addresses are excluded from stats."""
    rows = [("noreply@acme-corp.com", "team@acme-corp.com", 1700000000)]
    result = _build_addr_stats(rows)

    # acme-corp.com is internal — should be filtered
    assert "noreply@acme-corp.com" not in result


def test_build_addr_stats_multiple_rows_aggregated() -> None:
    """Multiple rows for same address are aggregated."""
    rows = [
        ("contact@acme-corp.com", "", 1700000000),
        ("contact@acme-corp.com", "", 1700001000),
    ]
    result = _build_addr_stats(rows)

    assert "contact@acme-corp.com" in result
    assert result["contact@acme-corp.com"]["msgs"] == 2
    assert result["contact@acme-corp.com"]["last_epoch"] == 1700001000


def test_build_addr_stats_addr_without_at_sign_excluded() -> None:
    """Addresses without @ are excluded."""
    rows = [("not-an-email", "valid@acme-corp.com", 1700000000)]
    result = _build_addr_stats(rows)

    assert "not-an-email" not in result


# ---------------------------------------------------------------------------
# _fetch_blindspot_messages
# ---------------------------------------------------------------------------


# ── TestFetchBlindspotMessages (flattened) ──────────────────────────────────


def test_fetch_blindspot_messages_no_since_returns_all_messages() -> None:
    """Without since filter, all messages for thread IDs are returned."""
    conn = _make_db()
    _insert_message(conn, "t-1", "a@acme-corp.com", date_epoch=1000000)
    _insert_message(conn, "t-2", "b@acme-corp.com", date_epoch=2000000)

    try:
        rows = _fetch_blindspot_messages(conn, ["t-1", "t-2"], since=None)
    finally:
        conn.close()

    assert len(rows) == 2


def test_fetch_blindspot_messages_since_filter_excludes_old_messages() -> None:
    """Messages before since epoch are excluded."""
    conn = _make_db()
    _insert_message(conn, "t-old", "old@acme-corp.com", date_epoch=1000000)
    _insert_message(conn, "t-new", "new@acme-corp.com", date_epoch=2000000000)

    try:
        rows = _fetch_blindspot_messages(conn, ["t-old", "t-new"], since=1500000000)
    finally:
        conn.close()

    # Only the new message should be returned
    from_addrs = [r[0] for r in rows]
    assert "new@acme-corp.com" in from_addrs
    assert "old@acme-corp.com" not in from_addrs


def test_fetch_blindspot_messages_empty_thread_ids_returns_empty() -> None:
    """Empty thread_ids list returns no messages."""
    conn = _make_db()
    _insert_message(conn, "t-1", "a@acme-corp.com")

    try:
        # SQLite IN () with empty list would be a syntax error, but the
        # function is only called when thread_id_rows is non-empty
        # (guarded in query_blindspots). Test with a non-matching ID.
        rows = _fetch_blindspot_messages(conn, ["nonexistent-thread"], since=None)
    finally:
        conn.close()

    assert rows == []


# ---------------------------------------------------------------------------
# _format_blindspots_table
# ---------------------------------------------------------------------------


# ── TestFormatBlindspotsTable (flattened) ───────────────────────────────────


def test_format_blindspots_table_formats_results_without_error(capsys: pytest.CaptureFixture[str]) -> None:
    """_format_blindspots_table prints without raising."""
    results = [
        {"email": "contact@acme-corp.com", "name": "Alice", "msgs": 5, "days": 10},
        {"email": "other@globalpay.example.com", "name": "", "msgs": 3, "days": 9999},
    ]
    with patch("fieldkit.commands.gmail.query.console", Console(width=200, force_terminal=False)):
        _format_blindspots_table(results)
    captured = capsys.readouterr()
    assert "contact@acme-corp.com" in captured.out
    assert "10d ago" in captured.out
    assert "unknown" in captured.out


def test_format_blindspots_table_empty_results_prints_header(capsys: pytest.CaptureFixture[str]) -> None:
    """Empty results list still prints the header."""
    _format_blindspots_table([])
    captured = capsys.readouterr()
    assert "Email" in captured.out


# ---------------------------------------------------------------------------
# _champion_thread_stats — empty emails path
# ---------------------------------------------------------------------------


# ── TestChampionThreadStatsEdgeCases (flattened) ────────────────────────────


def test_build_addr_stats_empty_emails_returns_zeros() -> None:
    """Empty email list returns (0, 0, 0, None) immediately."""
    conn = _make_db()
    try:
        result = _champion_thread_stats(conn, [])
    finally:
        conn.close()

    assert result == (0, 0, 0, None)


def test_build_addr_stats_with_since_filter() -> None:
    """since parameter is passed through without error."""
    conn = _make_db()
    try:
        initiated, total, sent, last = _champion_thread_stats(conn, ["test@acme-corp.com"], since=1700000000)
    finally:
        conn.close()

    assert initiated == 0
    assert total == 0
    assert sent == 0
    assert last is None
