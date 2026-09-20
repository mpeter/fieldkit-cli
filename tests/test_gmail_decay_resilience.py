"""Tests for gmail decay resilience — historic regression (missing thread_accounts)."""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.gmail.decay_domain import _get_account_thread_ids
from fieldkit.gmail.exceptions import GmailIndexMissingError

pytestmark = pytest.mark.unit


def _make_conn(tmp_path: Path, *, with_thread_accounts: bool) -> sqlite3.Connection:
    """Create a minimal in-memory-style connection for testing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if with_thread_accounts:
        conn.execute("CREATE TABLE thread_accounts (thread_id TEXT, account TEXT)")
        conn.execute("INSERT INTO thread_accounts VALUES ('t1', 'acme-corp')")
        conn.commit()
    return conn


# ── TestGetAccountThreadIds (flattened) ─────────────────────────────────────


def test_get_account_thread_ids_returns_thread_ids_when_table_exists(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path, with_thread_accounts=True)
    try:
        result = _get_account_thread_ids(conn, "acme-corp")
        assert result == ["t1"]
    finally:
        conn.close()


def test_get_account_thread_ids_raises_index_missing_error_when_table_missing(tmp_path: Path) -> None:
    """historic regression: missing thread_accounts table → GmailIndexMissingError with repair hint."""
    conn = _make_conn(tmp_path, with_thread_accounts=False)
    try:
        with pytest.raises(GmailIndexMissingError) as exc_info:
            _get_account_thread_ids(conn, "acme-corp")
        assert "thread_accounts" in str(exc_info.value)
        assert "fieldkit sync" in str(exc_info.value).lower()
    finally:
        conn.close()


def test_get_account_thread_ids_returns_empty_list_for_unknown_account(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path, with_thread_accounts=True)
    try:
        result = _get_account_thread_ids(conn, "nonexistent-account")
        assert result == []
    finally:
        conn.close()
