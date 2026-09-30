"""Tests for historic regression gmail domain extraction.

Covers:
- query_domain.py: connect() raises GmailDbNotFoundError when db missing
- query_domain.py: query_champion_signals() returns str
- exceptions.py: GmailDbNotFoundError is importable and is an Exception subclass
"""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.gmail.exceptions import GmailDbNotFoundError

pytestmark = pytest.mark.unit


def test_query_adapter_does_not_reexport_domain_helpers() -> None:
    from fieldkit.commands.gmail import enrich_pursuits, query

    domain_names = {
        "_build_addr_stats",
        "_champion_thread_stats",
        "_chunk_list",
        "_is_noise",
        "_normalize_date",
        "build_date_clause",
        "connect",
        "date_to_epoch",
        "query_blindspots",
        "query_by_email",
        "query_champion_signals",
        "query_dig",
    }
    assert not domain_names.intersection(vars(query))
    assert not domain_names.intersection(vars(enrich_pursuits))


# ---------------------------------------------------------------------------
# Helpers: minimal in-memory schema for query tests
# ---------------------------------------------------------------------------

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
CREATE TABLE thread_accounts (
    thread_id TEXT,
    account   TEXT
);
"""


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

    assert "has not been published" in str(exc_info.value).lower()
    assert str(missing_path) not in str(exc_info.value)


def test_query_domain_connect_returns_connection_when_db_exists(tmp_path: Path) -> None:
    """connect() returns a sqlite3.Connection when the db file exists."""
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
    with pytest.raises(GmailDbNotFoundError, match="not been published"):
        connect(missing_path)


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

    collect_path = Path(__file__).parent.parent / "src/fieldkit/brief/collect.py"
    source = collect_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("fieldkit.commands.gmail"), (
                f"collect.py must not import from fieldkit.commands.gmail.*; found: from {node.module} import ..."
            )
