"""Regression tests for historic regression — last_date uses epoch ordering, not lexicographic MAX.

RFC 2822 date strings like "Wed, 6 May 2025 10:00:00 +0000" are NOT lexicographically
sortable.  MAX(date_str) returns the wrong message in threads where RFC 2822 strings
are mixed, and when parsing fails, the old raw[:10] fallback produced garbage like
"Wed, 6 May".

Fixes:
  1. All SQL subqueries now use ORDER BY date_epoch DESC LIMIT 1 instead of MAX(date_str).
  2. _normalize_date() returns "" (empty string) instead of raw[:10] on parse failure.
"""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.commands.gmail.query import _normalize_date, query_dig

pytestmark = pytest.mark.unit

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "fieldkit" / "gmail" / "schema.sql"


# ---------------------------------------------------------------------------
# _normalize_date unit tests (historic regression: fallback must be "" not raw[:10])
# ---------------------------------------------------------------------------


# ── TestNormalizeDateFallback (flattened) ───────────────────────────────────


def test_normalize_date_fallback_valid_rfc2822_parses_correctly() -> None:
    """Well-formed RFC 2822 date must parse to YYYY-MM-DD."""
    result = _normalize_date("Wed, 06 May 2025 10:00:00 +0000")
    assert result == "2025-05-06", f"Unexpected result: {result!r}"


def test_normalize_date_fallback_iso_prefix_fast_path() -> None:
    """Already-ISO strings must be returned via the fast path."""
    result = _normalize_date("2025-05-06T10:00:00")
    assert result == "2025-05-06", f"Unexpected result: {result!r}"


def test_normalize_date_fallback_empty_input_returns_empty() -> None:
    """Empty input must return empty string."""
    assert _normalize_date("") == ""


def test_normalize_date_fallback_unparseable_rfc2822_returns_empty_not_garbage() -> None:
    """historic regression: raw[:10] of 'Wed, 6 May 2025 …' = 'Wed, 6 May' — must be '' instead."""
    # This is the exact failure mode from historic regression: a short-day RFC 2822 string
    # that doesn't match any _RFC2822_FORMATS pattern.
    result = _normalize_date("Wed, 6 May 2025 10:00:00 +0000")
    # Must not produce garbage like "Wed, 6 May"
    assert result != "Wed, 6 May", f"Garbage fallback returned: {result!r}"
    # Must be either a valid YYYY-MM-DD or empty string
    if result:
        assert len(result) == 10 and result[4] == "-" and result[7] == "-", (
            f"Non-empty result must be YYYY-MM-DD, got: {result!r}"
        )


def test_normalize_date_fallback_completely_corrupt_string_returns_empty() -> None:
    """A completely unparseable string must return '' not its first 10 chars."""
    result = _normalize_date("not a date at all")
    assert result == "", f"Expected empty string for corrupt input, got: {result!r}"


def test_normalize_date_fallback_short_corrupt_string_returns_empty() -> None:
    """A string shorter than 10 chars that isn't ISO must return ''."""
    result = _normalize_date("bad")
    assert result == "", f"Expected empty string, got: {result!r}"


def test_normalize_date_fallback_rfc2822_with_timezone_annotation_parses() -> None:
    """RFC 2822 with trailing (GMT) annotation must parse correctly."""
    result = _normalize_date("06 May 2025 10:00:00 +0000 (GMT)")
    assert result == "2025-05-06", f"Unexpected result: {result!r}"


# ---------------------------------------------------------------------------
# Epoch-ordering integration tests via query_dig() (historic regression)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_epoch_order() -> sqlite3.Connection:
    """In-memory DB with messages whose lexicographic date_str order differs from epoch order.

    Thread t-epoch-1 has two messages:
      - m-early: date_str = "Fri, 01 Jan 2021 00:00:00 +0000", epoch = 1609459200 (earlier)
      - m-late:  date_str = "Mon, 01 Mar 2021 00:00:00 +0000", epoch = 1614556800 (later)

    Lexicographic MAX(date_str) would return "Mon, 01 Mar 2021 …" (M > F alphabetically),
    which happens to be correct here — but the test verifies epoch ordering is used by
    checking the returned last_date corresponds to the message with the highest epoch.

    Thread t-epoch-2 has messages with RFC 2822 strings that sort differently lexicographically:
      - m-z: date_str = "Sat, 01 May 2021 00:00:00 +0000", epoch = 1619827200 (LATEST)
      - m-a: date_str = "Thu, 01 Apr 2021 00:00:00 +0000", epoch = 1617235200 (earlier)

    MAX(date_str) would pick "Thu, 01 Apr …" (T > S alphabetically) — WRONG.
    Epoch ordering picks "Sat, 01 May …" — CORRECT.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    conn.executemany(
        "INSERT INTO threads (thread_id, subject, message_count, updated_at) VALUES (?, ?, ?, ?)",
        [
            ("t-epoch-1", "Epoch test thread one", 2, "2021-03-01"),
            ("t-epoch-2", "Epoch test thread two", 2, "2021-05-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)",
        [
            ("t-epoch-1", "acme"),
            ("t-epoch-2", "acme"),
        ],
    )
    conn.executemany(
        "INSERT INTO messages (message_id, thread_id, from_addr, to_addr, cc_addr, subject, date_str, date_epoch, body_plain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # t-epoch-1: early message first
            (
                "m-e1-early",
                "t-epoch-1",
                "alice@acme.example.com",
                "rep@company.example.com",  # pii-guard: ignore
                "",
                "Epoch test thread one",
                "Fri, 01 Jan 2021 00:00:00 +0000",
                1_609_459_200,
                "epoch keyword early",
            ),
            (
                "m-e1-late",
                "t-epoch-1",
                "rep@company.example.com",  # pii-guard: ignore
                "alice@acme.example.com",
                "",
                "Epoch test thread one",
                "Mon, 01 Mar 2021 00:00:00 +0000",
                1_614_556_800,
                "epoch keyword late",
            ),
            # t-epoch-2: lexicographic max is WRONG (T > S), epoch max is correct (May > Apr)
            (
                "m-e2-z",
                "t-epoch-2",
                "alice@acme.example.com",
                "rep@company.example.com",  # pii-guard: ignore
                "",
                "Epoch test thread two",
                "Sat, 01 May 2021 00:00:00 +0000",
                1_619_827_200,  # LATEST epoch
                "epoch keyword latest",
            ),
            (
                "m-e2-a",
                "t-epoch-2",
                "alice@acme.example.com",
                "rep@company.example.com",  # pii-guard: ignore
                "",
                "Epoch test thread two",
                "Thu, 01 Apr 2021 00:00:00 +0000",
                1_617_235_200,  # earlier epoch
                "epoch keyword earlier",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO people (email, display_name, message_count) VALUES (?, ?, ?)",
        [
            ("alice@acme.example.com", "Alice", 3),
            ("rep@company.example.com", "Rep", 2),  # pii-guard: ignore
        ],
    )
    conn.commit()
    return conn


# ── TestEpochOrderingInQueryDig (flattened) ─────────────────────────────────


def test_epoch_ordering_in_query_dig_last_date_uses_epoch_order_not_lex(db_epoch_order: sqlite3.Connection) -> None:
    """Thread t-epoch-2: epoch-latest message is May (Sat), not Apr (Thu).

    MAX(date_str) would return "Thu, 01 Apr …" because 'T' > 'S' lexicographically.
    Epoch ordering correctly returns "Sat, 01 May …".
    """
    results = query_dig(db_epoch_order, "acme", "epoch")
    assert results, "Expected at least one result for keyword 'epoch'"

    # Find t-epoch-2 in results
    t2 = next((r for r in results if r["thread_id"] == "t-epoch-2"), None)
    assert t2 is not None, "t-epoch-2 should appear in results"

    last_date = t2["last_date"]
    # The epoch-latest message has date_str "Sat, 01 May 2021 00:00:00 +0000"
    # After _normalize_date it becomes "2021-05-01"
    assert "May" in last_date or last_date == "2021-05-01", (
        f"Expected May (epoch-latest) as last_date, got: {last_date!r}. "
        f"'Thu' or 'Apr' would indicate MAX(date_str) lexicographic bug."
    )
    # Must NOT be the lexicographic max (April / Thu)
    assert "Apr" not in last_date and "Thu" not in last_date, (
        f"last_date contains April/Thu — lexicographic MAX bug still present: {last_date!r}"
    )


def test_epoch_ordering_in_query_dig_thread_one_last_date_is_march(db_epoch_order: sqlite3.Connection) -> None:
    """Thread t-epoch-1: epoch-latest message is March (epoch 1614556800)."""
    results = query_dig(db_epoch_order, "acme", "epoch")
    t1 = next((r for r in results if r["thread_id"] == "t-epoch-1"), None)
    assert t1 is not None, "t-epoch-1 should appear in results"

    last_date = t1["last_date"]
    assert "Mar" in last_date or last_date == "2021-03-01", (
        f"Expected March as last_date for t-epoch-1, got: {last_date!r}"
    )
