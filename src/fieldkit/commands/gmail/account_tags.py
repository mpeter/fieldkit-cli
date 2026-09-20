#!/usr/bin/env python3
"""Map Gmail threads to accounts via ref/* labels stored in thread_accounts."""

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.gmail.discover import get_gmail_db_path


def _emit_json(account_threads: dict[str, int], account_filter: str | None) -> None:
    """Emit the tagging summary as a machine-readable document on stdout."""
    items = [
        {"account": account, "threads": count}
        for account, count in sorted(account_threads.items(), key=lambda x: -x[1])
    ]
    click.echo(
        json.dumps(
            {
                "items": items,
                "count": len(items),
                "associations": sum(account_threads.values()),
                "filters": {"account": account_filter},
            },
            indent=2,
            default=str,
        )
    )


def build_account_tags(db_path: str, account_filter: str | None = None, *, as_json: bool = False) -> None:
    """Map Gmail threads to accounts by scanning ref/* labels and upserting into thread_accounts.

    Args:
        db_path:        Path to gmail.db.
        account_filter: When set, only processes the ``ref/<account_filter>`` label.
        as_json:        Emit the summary as JSON instead of prose. The upsert is
                        identical either way — only the rendering differs.
    """
    db = Path(db_path)
    if not db.exists():
        click.echo(f"ERROR: database not found: {db_path}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.cursor()

        # Join messages (labels JSON array) with labels table to find ref/* entries.
        # json_each() expands the labels JSON array into individual rows.
        # We exclude ref/keep — it is a metadata tag, not an account identifier.
        if account_filter is not None:
            # Exact-match on ref/<slug> — ref/keep exclusion is implicit here because
            # slug validation at the CLI boundary (validate_account_slug) rejects "keep"
            # as an unknown account. No explicit guard needed, but document the invariant.
            cur.execute(
                """
                SELECT DISTINCT m.thread_id, l.label_name
                FROM   messages m,
                       json_each(m.labels) je
                JOIN   labels l ON l.label_id = je.value
                WHERE  l.label_name = ?
                """,
                (f"ref/{account_filter}",),
            )
        else:
            cur.execute("""
                SELECT DISTINCT m.thread_id, l.label_name
                FROM   messages m,
                       json_each(m.labels) je
                JOIN   labels l ON l.label_id = je.value
                WHERE  l.label_name LIKE 'ref/%'
                  AND  l.label_name != 'ref/keep'
            """)

        rows = cur.fetchall()

        if not rows:
            if as_json:
                _emit_json({}, account_filter)
            else:
                click.echo("No ref/* labels found in messages — thread_accounts will be empty.")
            return

        # Build (thread_id, account) pairs and tally per account for the summary.
        account_threads: defaultdict[str, int] = defaultdict(int)
        pairs: list[tuple[str, str]] = []
        for thread_id, label_name in rows:
            account = label_name.removeprefix("ref/")
            pairs.append((thread_id, account))
            account_threads[account] += 1

        # Idempotent upsert — composite PK (thread_id, account) handles duplicates.
        conn.executemany(
            "INSERT OR REPLACE INTO thread_accounts(thread_id, account) VALUES (?, ?)",
            pairs,
        )
        conn.commit()

        if as_json:
            _emit_json(account_threads, account_filter)
            return

        total = sum(account_threads.values())
        click.echo(f"Upserted {total} thread-account associations across {len(account_threads)} account(s):")
        for account, count in sorted(account_threads.items(), key=lambda x: -x[1]):
            click.echo(f"  {account}: {count} threads")
    finally:
        conn.close()


@click.command(name="account-tags")
@click.option("--db", default=None, help="Path to gmail.db (defaults to data/gmail.db in workspace).")
@click.option(
    "--account", "-a", default=None, help="Filter tagging to a single account slug (processes only ref/<slug> labels)."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def cli(db: str | None, account: str | None, as_json: bool) -> None:
    """Map Gmail threads to accounts via ref/* labels."""
    from fieldkit.commands._account_guard import validate_account_slug

    validate_account_slug(account)
    db_path = db if db is not None else str(get_gmail_db_path())
    build_account_tags(db_path, account_filter=account, as_json=as_json)


if __name__ == "__main__":
    cli()
