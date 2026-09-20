"""Unit tests for tools/gmail-cache/account-tags.py build_account_tags()."""

import json
import sqlite3
from pathlib import Path

import pytest

from fieldkit.commands.gmail import account_tags

pytestmark = pytest.mark.unit

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "fieldkit" / "gmail" / "schema.sql"

# account_tags imported above from fieldkit.commands.gmail


def _make_db(tmp_path, threads, messages, label_rows):
    """Create a temp SQLite DB with schema and seeded rows."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    for t in threads:
        conn.execute(
            "INSERT INTO threads(thread_id, subject, snippet, message_count, updated_at) VALUES (?,?,?,?,?)",
            (t, "", "", 0, "2024-01-01"),
        )
    for msg_id, thread_id, labels in messages:
        conn.execute(
            "INSERT INTO messages(message_id, thread_id, labels, synced_at) VALUES (?,?,?,datetime('now'))",
            (msg_id, thread_id, json.dumps(labels)),
        )
    for label_id, label_name in label_rows:
        conn.execute(
            "INSERT OR IGNORE INTO labels(label_id, label_name) VALUES (?,?)",
            (label_id, label_name),
        )
    conn.commit()
    conn.close()
    return db_path


def _get_thread_accounts(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT thread_id, account FROM thread_accounts ORDER BY thread_id, account").fetchall()
    conn.close()
    return rows


def test_basic_ref_tagging(tmp_path):
    """A message with a ref/global-pay label produces (thread_id, global-pay) in thread_accounts."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_globalpay"])],
        label_rows=[("Label_ref_globalpay", "ref/global-pay")],
    )
    account_tags.build_account_tags(db)
    assert _get_thread_accounts(db) == [("t1", "global-pay")]


def test_ref_keep_excluded(tmp_path):
    """A ref/keep label must not produce any thread_accounts row."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_keep"])],
        label_rows=[("Label_ref_keep", "ref/keep")],
    )
    account_tags.build_account_tags(db)
    assert _get_thread_accounts(db) == []


def test_multi_account_tagging(tmp_path):
    """A message tagged with two ref/* labels produces two rows."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_globalpay", "Label_ref_acme-bank"])],
        label_rows=[
            ("Label_ref_globalpay", "ref/global-pay"),
            ("Label_ref_acme-bank", "ref/acme-bank"),
        ],
    )
    account_tags.build_account_tags(db)
    assert set(_get_thread_accounts(db)) == {("t1", "global-pay"), ("t1", "acme-bank")}


def test_no_ref_labels(tmp_path):
    """A message with only a system label (INBOX) produces no thread_accounts rows."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["INBOX"])],
        label_rows=[("INBOX", "INBOX")],
    )
    account_tags.build_account_tags(db)
    assert _get_thread_accounts(db) == []


def test_idempotent_upsert(tmp_path):
    """Calling build_account_tags twice must not duplicate rows (composite PK)."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_globalpay"])],
        label_rows=[("Label_ref_globalpay", "ref/global-pay")],
    )
    account_tags.build_account_tags(db)
    account_tags.build_account_tags(db)
    assert len(_get_thread_accounts(db)) == 1


def test_mixed_keep_and_ref(tmp_path):
    """When a message has both ref/keep and ref/global-pay, only ref/global-pay should be tagged."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_keep", "Label_ref_globalpay"])],
        label_rows=[
            ("Label_ref_keep", "ref/keep"),
            ("Label_ref_globalpay", "ref/global-pay"),
        ],
    )
    account_tags.build_account_tags(db)
    assert _get_thread_accounts(db) == [("t1", "global-pay")]
