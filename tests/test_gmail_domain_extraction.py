"""Tests for historic regression gmail domain extraction.

Covers:
- decay_domain.py: decay_report() characterization test
- query_domain.py: connect() raises GmailDbNotFoundError when db missing
- query_domain.py: query_champion_signals() returns str
- exceptions.py: GmailDbNotFoundError is importable and is an Exception subclass
"""

import contextlib
import io
import sqlite3
from pathlib import Path

import pytest

from fieldkit.gmail.exceptions import GmailDbNotFoundError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: minimal in-memory schema for decay and query tests
# ---------------------------------------------------------------------------

_DECAY_SCHEMA = """
CREATE TABLE thread_accounts (
    thread_id TEXT,
    account   TEXT
);
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY,
    thread_id  TEXT,
    from_addr  TEXT,
    to_addr    TEXT,
    cc_addr    TEXT,
    date_epoch INTEGER,
    date_str   TEXT
);
"""

_QUERY_SCHEMA = """
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
    subject     TEXT,
    body_plain  TEXT
);
CREATE TABLE people (
    email         TEXT PRIMARY KEY,
    display_name  TEXT,
    message_count INTEGER DEFAULT 0
);
"""


def _make_decay_conn(tmp_path: Path) -> sqlite3.Connection:
    """Create a minimal gmail.db with decay schema for testing."""
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DECAY_SCHEMA)
    conn.commit()
    return conn


def _make_query_conn() -> sqlite3.Connection:
    """Create an in-memory connection with query schema for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_QUERY_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# GmailDbNotFoundError — exceptions.py
# ---------------------------------------------------------------------------


# ── TestGmailDbNotFoundError (flattened) ────────────────────────────────────


def test_gmail_db_not_found_error_is_exception_subclass() -> None:
    """GmailDbNotFoundError must be a subclass of Exception."""
    assert issubclass(GmailDbNotFoundError, Exception)


def test_gmail_db_not_found_error_can_be_raised_and_caught() -> None:
    """GmailDbNotFoundError can be raised and caught by callers."""
    with pytest.raises(GmailDbNotFoundError, match="not found"):
        raise GmailDbNotFoundError("Gmail database not found: /tmp/missing.db")


def test_gmail_db_not_found_error_message_preserved() -> None:
    """Exception message is preserved on the raised instance."""
    msg = "Gmail database not found: /nonexistent/path/gmail.db"
    exc = GmailDbNotFoundError(msg)
    assert str(exc) == msg


# ---------------------------------------------------------------------------
# query_domain.connect() — raises GmailDbNotFoundError when db missing
# ---------------------------------------------------------------------------


# ── TestQueryDomainConnect (flattened) ──────────────────────────────────────


def test_query_domain_connect_raises_when_db_missing(tmp_path: Path) -> None:
    """connect() raises GmailDbNotFoundError for a non-existent path."""
    from fieldkit.gmail.query_domain import connect

    missing_path = tmp_path / "nonexistent.db"
    assert not missing_path.exists()

    with pytest.raises(GmailDbNotFoundError) as exc_info:
        connect(missing_path)

    assert "not found" in str(exc_info.value).lower()
    assert str(missing_path) in str(exc_info.value)


def test_query_domain_connect_returns_connection_when_db_exists(tmp_path: Path) -> None:
    """connect() returns a sqlite3.Connection when the db file exists."""
    from fieldkit.gmail.query_domain import connect

    db_path = tmp_path / "gmail.db"
    # Create a minimal db with the required tables
    setup_conn = sqlite3.connect(str(db_path))
    setup_conn.executescript(_QUERY_SCHEMA)
    setup_conn.commit()
    setup_conn.close()

    conn = connect(db_path)
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


def test_query_domain_connect_does_not_call_sys_exit(tmp_path: Path) -> None:
    """connect() must not call sys.exit() — raises GmailDbNotFoundError instead.

    historic regression: domain functions must not call sys.exit().
    """
    from fieldkit.gmail.query_domain import connect

    missing_path = tmp_path / "missing.db"

    # If sys.exit() were called, SystemExit would be raised (not GmailDbNotFoundError).
    # We assert GmailDbNotFoundError specifically to confirm the correct exception type.
    with pytest.raises(GmailDbNotFoundError, match="not found"):
        connect(missing_path)


# ---------------------------------------------------------------------------
# decay_domain.connect() — raises GmailDbNotFoundError when db missing
# ---------------------------------------------------------------------------


# ── TestDecayDomainConnect (flattened) ──────────────────────────────────────


def test_decay_domain_connect_raises_when_db_missing(tmp_path: Path) -> None:
    """connect() raises GmailDbNotFoundError for a non-existent path."""
    from fieldkit.gmail.decay_domain import connect

    missing_path = tmp_path / "nonexistent.db"
    assert not missing_path.exists()

    with pytest.raises(GmailDbNotFoundError) as exc_info:
        connect(missing_path)

    assert str(missing_path) in str(exc_info.value)


def test_decay_domain_connect_returns_connection_when_db_exists(tmp_path: Path) -> None:
    """connect() returns a sqlite3.Connection when the db file exists."""
    from fieldkit.gmail.decay_domain import connect

    db_path = tmp_path / "gmail.db"
    setup_conn = sqlite3.connect(str(db_path))
    setup_conn.commit()
    setup_conn.close()

    conn = connect(db_path)
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# decay_domain.decay_report() — characterization test
# ---------------------------------------------------------------------------


# ── TestDecayReportCharacterization (flattened) ─────────────────────────────


def test_decay_report_characterization_no_threads_prints_no_threads_message(tmp_path: Path) -> None:
    """When no threads exist for the account, prints a 'No threads found' message."""
    from fieldkit.gmail.decay_domain import decay_report

    conn = _make_decay_conn(tmp_path)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            decay_report(
                conn,
                account="nonexistent-account",
                days_threshold=30,
                show_all=False,
            )
        output = buf.getvalue()
    finally:
        conn.close()

    assert "No threads found for account" in output
    assert "nonexistent-account" in output


def test_decay_report_characterization_no_contacts_prints_no_contacts_message(tmp_path: Path) -> None:
    """When threads exist but no external contacts, prints appropriate message."""
    from fieldkit.gmail.decay_domain import decay_report

    conn = _make_decay_conn(tmp_path)
    # Insert a thread for the account but no messages
    conn.execute("INSERT INTO thread_accounts VALUES ('t1', 'acme-corp')")
    conn.commit()

    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            decay_report(
                conn,
                account="acme-corp",
                days_threshold=30,
                show_all=False,
            )
        output = buf.getvalue()
    finally:
        conn.close()

    # Either "No contacts silent" or "No external contacts found"
    assert "No contacts" in output or "No external contacts" in output


def test_decay_report_characterization_report_header_format(tmp_path: Path) -> None:
    """decay_report() prints a title and 'As of:' date line when threads exist."""
    from fieldkit.gmail.decay_domain import decay_report

    conn = _make_decay_conn(tmp_path)
    # Insert a thread so the function proceeds past the early-return guard
    conn.execute("INSERT INTO thread_accounts VALUES ('t1', 'test-account')")
    conn.commit()

    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            decay_report(
                conn,
                account="test-account",
                days_threshold=30,
                show_all=False,
            )
        output = buf.getvalue()
    finally:
        conn.close()

    assert "Relationship decay: test-account" in output
    assert "As of:" in output


def test_decay_report_characterization_show_all_bypasses_threshold_filter(tmp_path: Path) -> None:
    """show_all=True includes contacts regardless of days_threshold."""
    from fieldkit.gmail.decay_domain import decay_report

    conn = _make_decay_conn(tmp_path)
    # Insert a thread and a recent message (should be filtered out with show_all=False)
    conn.execute("INSERT INTO thread_accounts VALUES ('t1', 'acme-corp')")
    import time

    now_epoch = int(time.time())
    # Message from 5 days ago — below any reasonable threshold
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, cc_addr, date_epoch, date_str) "
        "VALUES ('t1', 'contact@client.example.com', 'me@company.example.com', '', ?, '2026-06-01')",  # pii-guard: ignore
        [now_epoch - 5 * 86400],
    )
    conn.commit()

    try:
        buf_all = io.StringIO()
        with contextlib.redirect_stdout(buf_all):
            decay_report(
                conn,
                account="acme-corp",
                days_threshold=90,  # 90-day threshold would filter out 5-day contact
                show_all=True,  # but show_all bypasses it
            )
        output_all = buf_all.getvalue()

        buf_filtered = io.StringIO()
        with contextlib.redirect_stdout(buf_filtered):
            decay_report(
                conn,
                account="acme-corp",
                days_threshold=90,
                show_all=False,
            )
        output_filtered = buf_filtered.getvalue()
    finally:
        conn.close()

    # show_all=True should show the contact; show_all=False should not
    assert "contact@client.example.com" in output_all
    assert "contact@client.example.com" not in output_filtered


# ---------------------------------------------------------------------------
# query_domain.query_champion_signals() — return type check
# ---------------------------------------------------------------------------


# ── TestQueryChampionSignalsReturnType (flattened) ──────────────────────────


def test_query_champion_signals_return_type_returns_str_for_unknown_name() -> None:
    """Returns str even when no match is found."""
    from fieldkit.gmail.query_domain import query_champion_signals

    conn = _make_query_conn()
    try:
        result = query_champion_signals(conn, "Unknown Person XYZ")
    finally:
        conn.close()

    assert isinstance(result, str)
    assert "No people matched" in result


def test_query_champion_signals_return_type_returns_str_for_known_name() -> None:
    """Returns str when a match is found."""
    from fieldkit.gmail.query_domain import query_champion_signals

    conn = _make_query_conn()
    conn.execute(
        "INSERT INTO people (email, display_name) VALUES ('alice@example.com', 'Alice Smith')"  # pii-guard: ignore
    )  # pii-guard: ignore
    conn.commit()

    try:
        result = query_champion_signals(conn, "Alice Smith")
    finally:
        conn.close()

    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# collect.py import path — no commands.gmail imports
# ---------------------------------------------------------------------------


# ── TestCollectImportPaths (flattened) ──────────────────────────────────────


def test_collect_import_paths_collect_imports_from_domain() -> None:
    """collect.py must not import from fieldkit.commands.gmail.*."""
    import ast
    from pathlib import Path

    collect_path = Path(__file__).parent.parent / "src/fieldkit/commands/brief/collect.py"
    source = collect_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("fieldkit.commands.gmail"), (
                f"collect.py must not import from fieldkit.commands.gmail.*; found: from {node.module} import ..."
            )
