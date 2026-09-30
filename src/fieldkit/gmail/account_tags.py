"""Publish Gmail label-to-account associations as one committed generation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page
from fieldkit.sqlite_publication import SQLiteMutationConnection


@dataclass(frozen=True)
class AccountTagResult:
    """Bounded summary of one published account-tag update."""

    account_threads: tuple[tuple[str, int], ...]
    associations: int
    removed: int


def _tag_rows(connection: SQLiteMutationConnection, account_filter: str | None) -> list[tuple[str, str]]:
    ready = connection.execute("SELECT value FROM sync_state WHERE key = ?", (GMAIL_QUERY_READY_KEY,)).fetchone()
    if ready is None or ready[0] != "true":
        raise GmailSyncPartialError("Gmail cache is not ready; complete the first provider page before tagging")
    if account_filter is not None:
        cursor = connection.execute(
            """
            SELECT DISTINCT m.thread_id, l.label_name
            FROM messages m, json_each(m.labels) je
            JOIN labels l ON l.label_id = je.value
            WHERE l.label_name = ?
              AND l.label_name != 'ref/keep'
            """,
            (f"ref/{account_filter}",),
        )
    else:
        cursor = connection.execute(
            """
            SELECT DISTINCT m.thread_id, l.label_name
            FROM messages m, json_each(m.labels) je
            JOIN labels l ON l.label_id = je.value
            WHERE l.label_name LIKE 'ref/%'
              AND l.label_name != 'ref/keep'
            """
        )
    rows = cursor.fetchall()
    if any(
        not isinstance(row, tuple)
        or len(row) != 2
        or not isinstance(row[0], str)
        or not isinstance(row[1], str)
        or not row[1].startswith("ref/")
        for row in rows
    ):
        raise SQLiteSnapshotError("Gmail account-tag data is unverified", reason="unverified")
    return [(row[0], row[1]) for row in rows]


def _existing_pairs(
    connection: SQLiteMutationConnection,
    account_filter: str | None,
) -> set[tuple[str, str]]:
    if account_filter is None:
        rows = connection.execute("SELECT thread_id, account FROM thread_accounts").fetchall()
    else:
        rows = connection.execute(
            "SELECT thread_id, account FROM thread_accounts WHERE account = ?",
            (account_filter,),
        ).fetchall()
    if any(
        not isinstance(row, tuple) or len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], str)
        for row in rows
    ):
        raise SQLiteSnapshotError("Gmail account-tag data is unverified", reason="unverified")
    return {(row[0], row[1]) for row in rows}


def update_account_tags(db_path: Path, account_filter: str | None = None) -> AccountTagResult:
    """Publish associations derived from the exact managed Gmail source."""
    result = AccountTagResult(account_threads=(), associations=0, removed=0)

    def mutation(connection: SQLiteMutationConnection) -> None:
        nonlocal result
        account_threads: defaultdict[str, int] = defaultdict(int)
        pairs: set[tuple[str, str]] = set()
        for thread_id, label_name in _tag_rows(connection, account_filter):
            account = label_name.removeprefix("ref/")
            pair = (thread_id, account)
            if pair not in pairs:
                pairs.add(pair)
                account_threads[account] += 1
        existing_pairs = _existing_pairs(connection, account_filter)
        if account_filter is None:
            connection.execute("DELETE FROM thread_accounts")
        else:
            connection.execute("DELETE FROM thread_accounts WHERE account = ?", (account_filter,))
        connection.executemany(
            "INSERT OR REPLACE INTO thread_accounts(thread_id, account) VALUES (?, ?)",
            sorted(pairs),
        )
        counts = tuple(sorted(account_threads.items(), key=lambda item: (-item[1], item[0])))
        result = AccountTagResult(
            account_threads=counts,
            associations=len(pairs),
            removed=len(existing_pairs - pairs),
        )

    apply_gmail_page(db_path, mutation)
    return result
