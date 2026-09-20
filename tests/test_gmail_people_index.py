"""Unit tests for build_people_index in fieldkit.contact.people_index."""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.contact.people_index import build_people_index

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(path: str | Path) -> sqlite3.Connection:
    """Create a minimal gmail.db schema at *path* and return the open connection."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id   TEXT PRIMARY KEY,
            thread_id    TEXT,
            from_addr    TEXT,
            to_addr      TEXT,
            cc_addr      TEXT,
            date_epoch   INTEGER,
            subject      TEXT
        );
        CREATE TABLE IF NOT EXISTS thread_accounts (
            thread_id TEXT,
            account   TEXT
        );
        CREATE TABLE IF NOT EXISTS people (
            email         TEXT PRIMARY KEY,
            display_name  TEXT,
            first_seen    TEXT,
            last_seen     TEXT,
            message_count INTEGER DEFAULT 0
        );
        """
    )
    conn.commit()
    return conn


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    thread_id: str,
    from_addr: str,
    to_addr: str = "",
    cc_addr: str = "",
    date_epoch: int = 1_700_000_000,
    account: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO messages (message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch) VALUES (?,?,?,?,?,?)",
        (message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch),
    )
    if account:
        conn.execute(
            "INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)",
            (thread_id, account),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_messages_table_no_crash(tmp_path: Path) -> None:
    """Empty messages table → completes without crash and inserts 0 contacts."""
    db_file = str(tmp_path / "gmail.db")
    conn = _make_db(db_file)
    conn.close()

    build_people_index(db_file, show_progress=False)

    verify = sqlite3.connect(db_file)
    count = verify.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    verify.close()
    assert count == 0


@pytest.mark.unit
def test_limit_stops_after_n_messages(tmp_path: Path) -> None:
    """limit=5 causes at most 5 messages to be processed."""
    db_file = str(tmp_path / "gmail.db")
    conn = _make_db(db_file)
    # Insert 10 messages with distinct from addresses
    for i in range(10):
        _insert_message(
            conn,
            message_id=f"msg{i}",
            thread_id=f"t{i}",
            from_addr=f"user{i}@example.com",  # pii-guard: ignore
            date_epoch=1_700_000_000 + i,
        )
    conn.close()

    build_people_index(db_file, limit=5, show_progress=False)

    verify = sqlite3.connect(db_file)
    count = verify.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    verify.close()
    # With limit=5 only 5 distinct senders are processed
    assert count <= 5


@pytest.mark.unit
def test_show_progress_false_suppresses_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """show_progress=False writes nothing to stderr."""
    db_file = str(tmp_path / "gmail.db")
    conn = _make_db(db_file)
    for i in range(3):
        _insert_message(
            conn,
            message_id=f"msg{i}",
            thread_id=f"t{i}",
            from_addr=f"person{i}@company.com",  # pii-guard: ignore
        )
    conn.close()

    build_people_index(db_file, show_progress=False)

    captured = capsys.readouterr()
    assert captured.err == ""


@pytest.mark.unit
def test_account_filter_restricts_to_matching_account(tmp_path: Path) -> None:
    """account_filter='acme' only processes threads tagged with 'acme'."""
    db_file = str(tmp_path / "gmail.db")
    conn = _make_db(db_file)

    # Two messages in different accounts
    _insert_message(
        conn,
        message_id="msg1",
        thread_id="t1",
        from_addr="alice@acme-corp.com",
        account="acme",
    )
    _insert_message(
        conn,
        message_id="msg2",
        thread_id="t2",
        from_addr="bob@globalpay.example.com",
        account="globalpay",
    )
    conn.close()

    build_people_index(db_file, account_filter="acme", show_progress=False)

    verify = sqlite3.connect(db_file)
    rows = verify.execute("SELECT email FROM people").fetchall()
    verify.close()
    emails = {r[0] for r in rows}
    # Only alice (acme account) should appear; bob is in a different account
    assert "alice@acme-corp.com" in emails
    assert "bob@globalpay.example.com" not in emails


@pytest.mark.unit
def test_missing_messages_table_raises_operational_error(tmp_path: Path) -> None:
    """A DB with no messages table raises sqlite3.OperationalError."""
    db_file = str(tmp_path / "empty.db")
    # Create a completely empty SQLite DB (no tables at all)
    conn = sqlite3.connect(db_file)
    conn.close()

    with pytest.raises(sqlite3.OperationalError, match=r"no such table"):
        build_people_index(db_file, show_progress=False)
