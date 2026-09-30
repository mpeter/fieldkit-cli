#!/usr/bin/env python3
"""Map Gmail threads to accounts via ref/* labels stored in thread_accounts."""

import json
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.gmail import account_tags as account_tags_domain
from fieldkit.gmail.discover import get_gmail_db_path


def _emit_json(result: account_tags_domain.AccountTagResult, account_filter: str | None) -> None:
    """Emit the tagging summary as a machine-readable document on stdout."""
    items = [{"account": account, "threads": count} for account, count in result.account_threads]
    click.echo(
        json.dumps(
            {
                "items": items,
                "count": len(items),
                "associations": result.associations,
                "removed": result.removed,
                "filters": {"account": account_filter},
            },
            indent=2,
            default=str,
        )
    )


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
    db_path = Path(db) if db is not None else get_gmail_db_path()
    if not db_path.exists():
        click.echo("ERROR: Gmail cache is unavailable. Run 'fieldkit gmail sync' first.", err=True)
        raise SystemExit(EXIT_PARTIAL)
    result = account_tags_domain.update_account_tags(db_path, account_filter=account)
    if as_json:
        _emit_json(result, account)
        return
    if result.associations == 0:
        if result.removed:
            click.echo(
                f"Removed {result.removed} stale thread-account association(s); no current ref/* labels matched."
            )
        else:
            click.echo("No ref/* labels found in messages — thread_accounts is already current.")
        return
    click.echo(
        f"Upserted {result.associations} thread-account associations across {len(result.account_threads)} account(s):"
    )
    for account_name, count in result.account_threads:
        click.echo(f"  {account_name}: {count} threads")
    if result.removed:
        click.echo(f"Removed {result.removed} stale thread-account association(s).")
