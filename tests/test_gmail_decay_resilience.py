"""Bounded decay-domain behavior on exact account-associated Gmail rows."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fieldkit.gmail import query_support
from fieldkit.gmail.decay_domain import DecayQuery, query_decay
from fieldkit.gmail.exceptions import GmailSchemaError
from fieldkit.gmail.query_support import ConfiguredAccountScope, scan_row_budget

pytestmark = pytest.mark.unit


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE thread_accounts (thread_id TEXT, account TEXT);
        CREATE TABLE messages (
            message_id TEXT, thread_id TEXT, from_addr TEXT, to_addr TEXT,
            cc_addr TEXT, date_epoch INTEGER
        );
        CREATE TABLE people (email TEXT, display_name TEXT);
        """
    )
    return conn


def _scope(account: str = "acme-corp") -> ConfiguredAccountScope:
    return ConfiguredAccountScope(account, ("acme-corp.example.com",), ("internal.example.com",))


def test_shared_contact_uses_only_selected_account_recency(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    old_epoch = int((now - timedelta(days=120)).timestamp())
    recent_epoch = int((now - timedelta(days=5)).timestamp())
    conn.executemany("INSERT INTO thread_accounts VALUES (?, ?)", (("ta", "acme-corp"), ("tg", "globex")))
    conn.executemany(
        "INSERT INTO messages VALUES (?, ?, ?, ?, '', ?)",
        (
            ("ma", "ta", "buyer@acme-corp.example.com", "me@internal.example.com", old_epoch),
            ("mg", "tg", "buyer@acme-corp.example.com", "me@internal.example.com", recent_epoch),
        ),
    )
    conn.execute("INSERT INTO people VALUES ('buyer@acme-corp.example.com', 'Buyer')")

    try:
        report = query_decay(
            conn,
            _scope(),
            DecayQuery(
                days_threshold=90,
                show_all=False,
                domain_filter=None,
                min_messages=1,
                limit=10,
                max_age_days=365,
                now=now,
            ),
        )
    finally:
        conn.close()

    assert len(report.contacts) == 1
    assert report.contacts[0].days_any == 120
    assert report.contacts[0].messages == 1
    assert report.scan_truncated is False


def test_query_decay_reports_bounded_incomplete_scan(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    old_epoch = int((now - timedelta(days=120)).timestamp())
    conn.execute("INSERT INTO thread_accounts VALUES ('ta', 'acme-corp')")
    conn.executemany(
        "INSERT INTO messages VALUES (?, 'ta', ?, 'me@internal.example.com', '', ?)",
        ((f"m{index}", f"buyer{index}@acme-corp.example.com", old_epoch) for index in range(scan_row_budget(1) + 10)),
    )
    try:
        report = query_decay(
            conn,
            _scope(),
            DecayQuery(
                days_threshold=90,
                show_all=False,
                domain_filter=None,
                min_messages=1,
                limit=1,
                max_age_days=365,
                now=now,
            ),
        )
    finally:
        conn.close()

    assert report.scan_truncated is True
    assert report.scanned_rows <= scan_row_budget(1)
    assert len(report.contacts) <= 1


def test_query_decay_reports_sqlite_work_exhaustion_and_restores_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn(tmp_path)
    conn.executemany(
        "INSERT INTO thread_accounts VALUES (?, 'globex')",
        ((f"thread-{index}",) for index in range(10_000)),
    )
    monkeypatch.setattr(query_support, "DEFAULT_SQLITE_STEPS_PER_ROW", 1)

    try:
        report = query_decay(
            conn,
            _scope(),
            DecayQuery(
                days_threshold=90,
                show_all=False,
                domain_filter=None,
                min_messages=1,
                limit=1,
                max_age_days=365,
                now=datetime(2026, 9, 28, tzinfo=UTC),
            ),
        )
        restored = conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()

    assert report.scan_truncated is True
    assert restored[0] == 1


def test_query_decay_rejects_corrupt_message_rows(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    conn.execute("INSERT INTO thread_accounts VALUES ('ta', 'acme-corp')")
    conn.execute("INSERT INTO messages VALUES ('m1', 'ta', 'buyer@acme-corp.example.com', '', '', 'not-an-epoch')")
    try:
        with pytest.raises(GmailSchemaError, match="message date"):
            query_decay(
                conn,
                _scope(),
                DecayQuery(
                    days_threshold=90,
                    show_all=True,
                    domain_filter=None,
                    min_messages=1,
                    limit=10,
                    max_age_days=None,
                    now=datetime(2026, 9, 28, tzinfo=UTC),
                ),
            )
    finally:
        conn.close()
