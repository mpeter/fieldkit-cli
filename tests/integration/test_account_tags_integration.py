"""Integration test: account_tags.py correctly populates thread_accounts from ref/* labels."""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.commands.gmail import account_tags

build_account_tags = account_tags.build_account_tags


@pytest.mark.integration
def test_account_tags_populates_thread_accounts_excluding_keep(account_tags_db):
    """build_account_tags maps ref/* labels to thread_accounts, skipping ref/keep."""
    db_path: Path = account_tags_db

    build_account_tags(str(db_path))

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT thread_id, account FROM thread_accounts ORDER BY thread_id").fetchall()
    conn.close()

    assert rows == [
        ("thread001", "acme-bank"),
        ("thread002", "acme-bank"),
        ("thread004", "global-pay"),
    ], f"Unexpected thread_accounts rows: {rows}"

    # thread003 (ref/keep) must be absent
    thread_ids = {r[0] for r in rows}
    assert "thread003" not in thread_ids, "thread003 (ref/keep) must not appear in thread_accounts"
