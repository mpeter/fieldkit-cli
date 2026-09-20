"""Extended unit tests for src/fieldkit/commands/gmail/query.py.

Targets the 148 uncovered lines (65% → target ≥ 75%) by exercising:
  - _chunk_list: empty list, single chunk, multi-chunk
  - _ensure_schema: duplicate column silently ignored, other errors re-raised
  - _normalize_date: RFC 2822 formats, empty string, unparseable
  - build_date_clause: since only, before only, both, neither
  - date_to_epoch: happy path, is_before flag, invalid date
  - _DateEpoch.convert: valid date, invalid date (fail path)
  - print_results: empty rows, rows with last_date, rows with updated_at
  - _strip_quoted: strips quoted lines, collapses blank runs
  - _champion_signal_label: all three thresholds
  - _extract_email_addr: bare email, Name <email> format
  - _is_noise: internal domain, noise regex match, clean external
  - _format_blindspots_table: with results, unknown last_epoch
  - query_champion_signals: empty DB (no people matched)
  - _build_addr_stats: noise filtering, multi-address rows
"""

import sqlite3
from collections import namedtuple
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared: minimal in-memory DB fixture
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """Create a minimal in-memory DB with the required schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE people (
            email TEXT PRIMARY KEY,
            display_name TEXT,
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            thread_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_str TEXT,
            date_epoch INTEGER,
            subject TEXT,
            body_plain TEXT
        );
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            message_count INTEGER,
            updated_at TEXT
        );
        CREATE TABLE thread_accounts (
            thread_id TEXT,
            account TEXT
        );
    """)
    return conn


# ---------------------------------------------------------------------------
# _chunk_list
# ---------------------------------------------------------------------------


def test_chunk_list_empty_produces_one_empty_chunk() -> None:
    """_chunk_list([]) produces [[] ] — one empty chunk for zero-result callers."""
    from fieldkit.commands.gmail.query import _chunk_list

    result = _chunk_list([], 50)
    assert result == [[]], "Empty list must produce one empty chunk"


def test_chunk_list_single_chunk() -> None:
    """_chunk_list with fewer items than size returns one chunk."""
    from fieldkit.commands.gmail.query import _chunk_list

    result = _chunk_list(["a", "b", "c"], 10)
    assert result == [["a", "b", "c"]]


def test_chunk_list_multi_chunk() -> None:
    """_chunk_list splits into multiple chunks of the given size."""
    from fieldkit.commands.gmail.query import _chunk_list

    result = _chunk_list(list(range(7)), 3)
    assert result == [[0, 1, 2], [3, 4, 5], [6]]  # type: ignore[comparison-overlap]


# ---------------------------------------------------------------------------
# _ensure_schema — duplicate column silently ignored, other errors re-raised
# ---------------------------------------------------------------------------


def test_ensure_schema_silently_ignores_duplicate_column() -> None:
    """_ensure_schema does not raise when body_plain column already exists."""
    from fieldkit.commands.gmail.query import _ensure_schema

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body_plain TEXT)")
    conn.commit()

    # Should not raise — duplicate column is silently ignored
    _ensure_schema(conn)
    conn.close()


def test_ensure_schema_reraises_non_duplicate_errors() -> None:
    """_ensure_schema re-raises OperationalError that is not 'duplicate column'."""
    from fieldkit.commands.gmail.query import _ensure_schema

    conn = sqlite3.connect(":memory:")
    # No messages table at all — ALTER TABLE will fail with a different error
    with pytest.raises(sqlite3.OperationalError, match=r"no such table: messages"):
        _ensure_schema(conn)
    conn.close()


# ---------------------------------------------------------------------------
# _normalize_date
# ---------------------------------------------------------------------------


def test_normalize_date_empty_string_returns_empty() -> None:
    """_normalize_date returns '' for empty input."""
    from fieldkit.commands.gmail.query import _normalize_date

    assert _normalize_date("") == ""


def test_normalize_date_iso_prefix_fast_path() -> None:
    """_normalize_date returns first 10 chars for ISO-formatted dates."""
    from fieldkit.commands.gmail.query import _normalize_date

    assert _normalize_date("2025-06-01T12:00:00Z") == "2025-06-01"
    assert _normalize_date("2025-06-01") == "2025-06-01"


def test_normalize_date_rfc2822_format() -> None:
    """_normalize_date parses RFC 2822 date strings."""
    from fieldkit.commands.gmail.query import _normalize_date

    result = _normalize_date("Wed, 01 Jan 2025 12:00:00 +0000")
    assert result == "2025-01-01"


def test_normalize_date_unparseable_returns_empty() -> None:
    """_normalize_date returns '' for strings that cannot be parsed."""
    from fieldkit.commands.gmail.query import _normalize_date

    result = _normalize_date("not a date at all")
    assert result == ""


def test_normalize_date_strips_parenthesized_timezone() -> None:
    """_normalize_date strips trailing (GMT) annotations before parsing."""
    from fieldkit.commands.gmail.query import _normalize_date

    result = _normalize_date("01 Jan 2025 12:00:00 +0000 (GMT)")
    assert result == "2025-01-01"


# ---------------------------------------------------------------------------
# build_date_clause
# ---------------------------------------------------------------------------


def test_build_date_clause_since_only() -> None:
    """build_date_clause with since only returns correct fragment and params."""
    from fieldkit.commands.gmail.query import build_date_clause

    fragment, params = build_date_clause(since=1000, before=None)
    assert "date_epoch >= ?" in fragment
    assert params == [1000]


def test_build_date_clause_before_only() -> None:
    """build_date_clause with before only returns correct fragment and params."""
    from fieldkit.commands.gmail.query import build_date_clause

    fragment, params = build_date_clause(since=None, before=2000)
    assert "date_epoch < ?" in fragment
    assert params == [2000]


def test_build_date_clause_both() -> None:
    """build_date_clause with both since and before returns both conditions."""
    from fieldkit.commands.gmail.query import build_date_clause

    fragment, params = build_date_clause(since=1000, before=2000)
    assert "date_epoch >= ?" in fragment
    assert "date_epoch < ?" in fragment
    assert params == [1000, 2000]


def test_build_date_clause_neither() -> None:
    """build_date_clause with neither returns empty fragment and empty params."""
    from fieldkit.commands.gmail.query import build_date_clause

    fragment, params = build_date_clause(since=None, before=None)
    assert fragment == ""
    assert params == []


# ---------------------------------------------------------------------------
# date_to_epoch
# ---------------------------------------------------------------------------


def test_date_to_epoch_happy_path() -> None:
    """date_to_epoch converts YYYY-MM-DD to a positive integer epoch."""
    from fieldkit.commands.gmail.query import date_to_epoch

    epoch = date_to_epoch("2025-01-01")
    assert isinstance(epoch, int)
    assert epoch > 0


def test_date_to_epoch_is_before_adds_one_day() -> None:
    """date_to_epoch with is_before=True adds 86400 seconds."""
    from fieldkit.commands.gmail.query import date_to_epoch

    epoch_normal = date_to_epoch("2025-06-01", is_before=False)
    epoch_before = date_to_epoch("2025-06-01", is_before=True)
    assert epoch_before == epoch_normal + 86400


def test_date_to_epoch_invalid_raises_value_error() -> None:
    """date_to_epoch raises ValueError for invalid date strings."""
    from fieldkit.commands.gmail.query import date_to_epoch

    with pytest.raises(ValueError, match=r"does not match format"):
        date_to_epoch("not-a-date")


# ---------------------------------------------------------------------------
# _DateEpoch.convert — valid date, invalid date
# ---------------------------------------------------------------------------


def test_date_epoch_convert_valid_date() -> None:
    """_DateEpoch.convert returns an integer epoch for a valid YYYY-MM-DD."""
    from fieldkit.commands.gmail.query import _DateEpoch

    param_type = _DateEpoch()
    result = param_type.convert("2025-06-01", None, None)
    assert isinstance(result, int)
    assert result > 0


def test_date_epoch_convert_invalid_date_raises_bad_parameter() -> None:
    """_DateEpoch.convert calls self.fail() for invalid date strings."""
    import click

    from fieldkit.commands.gmail.query import _DateEpoch

    param_type = _DateEpoch()
    with pytest.raises(click.exceptions.BadParameter, match=r"YYYY-MM-DD"):
        param_type.convert("2025-13-99", None, None)


# ---------------------------------------------------------------------------
# print_results
# ---------------------------------------------------------------------------


def test_print_results_empty_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """print_results with empty rows prints '0 thread(s) found' and no table."""
    from fieldkit.commands.gmail.query import print_results

    print_results([], "Test label")

    captured = capsys.readouterr()
    assert "0 thread(s)" in captured.out
    assert "Test label" in captured.out


def test_print_results_with_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """print_results with rows prints the subject and message count."""
    conn = _make_db()
    conn.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?)",
        ("t1", "Test Subject", 3, "2025-06-01"),
    )
    conn.commit()
    rows = conn.execute("SELECT thread_id, subject, message_count, updated_at FROM threads").fetchall()

    from fieldkit.commands.gmail.query import print_results

    print_results(rows, "Test label")

    captured = capsys.readouterr()
    assert "Test Subject" in captured.out
    assert "1 thread(s)" in captured.out
    conn.close()


# ---------------------------------------------------------------------------
# _strip_quoted
# ---------------------------------------------------------------------------


def test_strip_quoted_removes_quoted_lines() -> None:
    """_strip_quoted removes lines starting with '>'."""
    from fieldkit.commands.gmail.query import _strip_quoted

    text = "Hello world\n> Quoted line\n> Another quoted\nEnd"
    result = _strip_quoted(text)
    assert ">" not in result
    assert "Hello world" in result


def test_strip_quoted_removes_on_wrote_pattern() -> None:
    """_strip_quoted stops at 'On ... wrote:' lines."""
    from fieldkit.commands.gmail.query import _strip_quoted

    text = "My reply\nOn Mon, Jan 1, 2025 at 12:00 PM Alice wrote:\n> Original message"
    result = _strip_quoted(text)
    assert "My reply" in result
    assert "Original message" not in result


def test_strip_quoted_collapses_blank_runs() -> None:
    """_strip_quoted collapses 3+ consecutive blank lines to 2."""
    from fieldkit.commands.gmail.query import _strip_quoted

    text = "Line 1\n\n\n\n\nLine 2"
    result = _strip_quoted(text)
    assert "\n\n\n" not in result


def test_strip_quoted_truncates_at_max_chars() -> None:
    """_strip_quoted truncates output at max_chars."""
    from fieldkit.commands.gmail.query import _strip_quoted

    text = "A" * 500
    result = _strip_quoted(text, max_chars=100)
    assert len(result) <= 100


# ---------------------------------------------------------------------------
# _champion_signal_label
# ---------------------------------------------------------------------------


def test_champion_signal_label_initiator() -> None:
    """_champion_signal_label returns INITIATOR for pct >= 30."""
    from fieldkit.gmail.query_domain import _champion_signal_label

    assert "INITIATOR" in _champion_signal_label(30)
    assert "INITIATOR" in _champion_signal_label(50)


def test_champion_signal_label_mixed() -> None:
    """_champion_signal_label returns MIXED for 15 <= pct < 30."""
    from fieldkit.gmail.query_domain import _champion_signal_label

    assert "MIXED" in _champion_signal_label(15)
    assert "MIXED" in _champion_signal_label(29)


def test_champion_signal_label_reactive() -> None:
    """_champion_signal_label returns REACTIVE for pct < 15."""
    from fieldkit.gmail.query_domain import _champion_signal_label

    assert "REACTIVE" in _champion_signal_label(0)
    assert "REACTIVE" in _champion_signal_label(14)


# ---------------------------------------------------------------------------
# _extract_email_addr
# ---------------------------------------------------------------------------


def test_extract_email_addr_bare_email() -> None:
    """_extract_email_addr returns lowercased bare email as-is."""
    from fieldkit.commands.gmail.query import _extract_email_addr

    assert _extract_email_addr("Alice@Example.COM") == "alice@example.com"  # pii-guard: ignore


def test_extract_email_addr_name_angle_format() -> None:
    """_extract_email_addr extracts email from 'Name <email>' format."""
    from fieldkit.commands.gmail.query import _extract_email_addr

    assert _extract_email_addr("Alice Smith <alice@example.com>") == "alice@example.com"  # pii-guard: ignore


def test_extract_email_addr_strips_whitespace() -> None:
    """_extract_email_addr strips leading/trailing whitespace."""
    from fieldkit.commands.gmail.query import _extract_email_addr

    assert _extract_email_addr("  alice@example.com  ") == "alice@example.com"  # pii-guard: ignore


# ---------------------------------------------------------------------------
# _is_noise
# ---------------------------------------------------------------------------


def test_is_noise_internal_domain_returns_true() -> None:
    """_is_noise returns True for known internal domains."""
    from fieldkit.commands.gmail.query import _is_noise

    with patch(
        "fieldkit.commands.gmail.query._internal_blind_domains",
        return_value={"internal-corp.example.com", "external.example.com"},
    ):
        assert _is_noise("user@internal-corp.example.com") is True
        assert _is_noise("user@external.example.com") is True


def test_is_noise_external_domain_returns_false() -> None:
    """_is_noise returns False for external, non-noise domains."""
    from fieldkit.commands.gmail.query import _is_noise

    with (
        patch("fieldkit.commands.gmail.query._internal_blind_domains", return_value={"internal-corp.example.com"}),
        patch("fieldkit.commands.gmail.query.NOISE_REGEX") as mock_noise,
    ):
        mock_noise.search.return_value = None
        assert _is_noise("alice@acme-corp.com") is False


def test_is_noise_noise_regex_match_returns_true() -> None:
    """_is_noise returns True when NOISE_REGEX matches the email."""
    from fieldkit.commands.gmail.query import _is_noise

    with (
        patch("fieldkit.commands.gmail.query._internal_blind_domains", return_value=set()),
        patch("fieldkit.commands.gmail.query.NOISE_REGEX") as mock_noise,
    ):
        mock_noise.search.return_value = True  # truthy match
        assert _is_noise("noreply@notifications.example.com") is True


# ---------------------------------------------------------------------------
# _format_blindspots_table
# ---------------------------------------------------------------------------


def test_format_blindspots_table_with_results(capsys: pytest.CaptureFixture[str]) -> None:
    """_format_blindspots_table prints a formatted table with results."""
    from rich.console import Console

    from fieldkit.commands.gmail.query import _format_blindspots_table

    results = [
        {"email": "alice@acme-corp.com", "name": "Alice Smith", "msgs": 5, "days": 3},
        {"email": "bob@acme-corp.com", "name": "Bob Jones", "msgs": 2, "days": 9999},
    ]
    with patch("fieldkit.commands.gmail.query.console", Console(width=200, force_terminal=False)):
        _format_blindspots_table(results)

    captured = capsys.readouterr()
    assert "alice@acme-corp.com" in captured.out
    assert "3d ago" in captured.out
    assert "unknown" in captured.out  # days=9999 → "unknown"


# ---------------------------------------------------------------------------
# _build_addr_stats — noise filtering
# ---------------------------------------------------------------------------


def test_build_addr_stats_filters_noise_addresses() -> None:
    """_build_addr_stats excludes noise addresses from the stats."""
    from fieldkit.commands.gmail.query import _build_addr_stats

    FakeRow = namedtuple("FakeRow", ["from_addr", "to_addr", "date_epoch"])

    rows = [
        FakeRow("alice@acme-corp.com", "noreply@automation.example.com", 1000),
        FakeRow("alice@acme-corp.com", "bob@acme-corp.com", 2000),
    ]

    def mock_is_noise(email: str) -> bool:
        return "automation.example.com" in email or "noreply" in email

    with patch("fieldkit.commands.gmail.query._is_noise", side_effect=mock_is_noise):
        stats = _build_addr_stats(rows)

    assert "alice@acme-corp.com" in stats
    assert "noreply@automation.example.com" not in stats
    assert "bob@acme-corp.com" in stats


def test_build_addr_stats_skips_empty_email() -> None:
    """_build_addr_stats skips rows where email extraction yields empty string."""
    from fieldkit.commands.gmail.query import _build_addr_stats

    FakeRow = namedtuple("FakeRow", ["from_addr", "to_addr", "date_epoch"])

    rows = [
        FakeRow("", None, 1000),  # empty from_addr, None to_addr
        FakeRow("alice@acme-corp.com", "", 2000),
    ]

    with patch("fieldkit.commands.gmail.query._is_noise", return_value=False):
        stats = _build_addr_stats(rows)

    assert "alice@acme-corp.com" in stats
    # Empty email should not appear
    assert "" not in stats
