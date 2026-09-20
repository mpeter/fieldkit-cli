#!/usr/bin/env python3
"""
Relationship decay report — last email contact per person, per account.

Usage:
    python tools/gmail-cache/decay.py global-pay
    python tools/gmail-cache/decay.py acme-bank --days 90
    python tools/gmail-cache/decay.py shield-ins --all

Shows every external contact in the account's tagged threads, sorted by
days since last contact. Highlights contacts who have gone quiet.

Domain logic has been extracted to fieldkit.gmail.decay_domain (historic regression).
"""

import click

from fieldkit.cli_exit import EXIT_DATA
from fieldkit.gmail.decay_domain import COLD_DAYS, connect, decay_report
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.exceptions import GmailDbNotFoundError, GmailIndexMissingError


@click.command(name="decay")
@click.argument("account", required=False, default=None)
@click.option("--account", "account_opt", "-a", default=None, help="Account slug (preferred over positional argument).")
@click.option(
    "--days", type=int, default=COLD_DAYS, help=f"Days-silent threshold to flag as stale (default: {COLD_DAYS})"
)
@click.option("--all", "show_all", is_flag=True, help="Show all contacts, not just stale ones")
@click.option("--domain", default=None, help="Restrict to contacts at this domain (e.g. globalpay.com)")
@click.option("--min-messages", type=int, default=1, help="Only show contacts with at least N messages (default: 1)")
@click.option("--limit", type=int, default=None, help="Cap results to N contacts (default: unlimited)")
@click.option(
    "--max-age-days",
    default=365,
    type=int,
    show_default=True,
    help="Exclude contacts silent for more than N days (0 = no cutoff).",
)
@click.option("--db", default=lambda: str(get_gmail_db_path()), help="Path to gmail.db", show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit the decay report as JSON.")
def cli(
    account: str | None,
    account_opt: str | None,
    days: int,
    show_all: bool,
    domain: str | None,
    min_messages: int,
    limit: int | None,
    max_age_days: int,
    db: str,
    as_json: bool,
) -> None:
    """Relationship decay report — last contact per external person per account."""
    from pathlib import Path

    # implementation change: --account flag preferred; positional ACCOUNT is deprecated
    resolved_account = account_opt or account
    if resolved_account is None:
        raise click.UsageError("Must provide an account name (positional or --account/-a).")
    account = resolved_account
    db_path = Path(db).resolve()
    if not db_path.name.endswith(".db"):
        raise click.UsageError(f"--db must point to a .db file, got: {db_path}")
    try:
        conn = connect(db_path)
    except GmailDbNotFoundError as exc:
        click.echo(f"Error: Gmail database not found: {db_path}. Run 'fieldkit gmail sync' first.", err=True)
        raise SystemExit(EXIT_DATA) from exc
    # Convert 0 → None so the library API receives the canonical "no cutoff" sentinel.
    resolved_max_age: int | None = None if max_age_days == 0 else max_age_days
    try:
        decay_report(
            conn,
            account=account,
            days_threshold=days,
            show_all=show_all,
            domain_filter=domain,
            min_messages=min_messages,
            limit=limit,
            max_age_days=resolved_max_age,
            as_json=as_json,
        )
    except GmailIndexMissingError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
