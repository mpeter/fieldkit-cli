"""Integration test: account_tags.py correctly populates thread_accounts from ref/* labels."""

from pathlib import Path

import pytest

from fieldkit.gmail.account_tags import update_account_tags
from fieldkit.gmail.publication import open_gmail_publication


@pytest.mark.integration
def test_account_tags_populates_thread_accounts_excluding_keep(account_tags_db):
    """The published updater maps ref/* labels while skipping ref/keep."""
    db_path: Path = account_tags_db

    update_account_tags(db_path)

    with open_gmail_publication(db_path) as connection:
        rows = [
            tuple(row)
            for row in connection.execute("SELECT thread_id, account FROM thread_accounts ORDER BY thread_id")
        ]

    assert rows == [
        ("thread001", "acme-bank"),
        ("thread002", "acme-bank"),
        ("thread004", "global-pay"),
    ], f"Unexpected thread_accounts rows: {rows}"

    # thread003 (ref/keep) must be absent
    thread_ids = {r[0] for r in rows}
    assert "thread003" not in thread_ids, "thread003 (ref/keep) must not appear in thread_accounts"
