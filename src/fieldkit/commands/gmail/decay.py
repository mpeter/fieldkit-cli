"""CLI adapter for bounded relationship-decay reads."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import click

from fieldkit.config import get_accounts_config
from fieldkit.errors import GmailSyncPartialError
from fieldkit.gmail.decay_domain import COLD_DAYS, DecayQuery, query_decay, render_decay_text
from fieldkit.gmail.discover import get_gmail_db_path
from fieldkit.gmail.query_domain import connect
from fieldkit.gmail.query_support import DEFAULT_QUERY_LIMIT, MAX_QUERY_LIMIT, configured_account_scope


@click.command(name="decay")
@click.option("--account", "account", "-a", required=True, metavar="SLUG", help="Configured account slug.")
@click.option(
    "--days",
    type=click.IntRange(min=0, max=36_500),
    default=COLD_DAYS,
    show_default=True,
    help="Days-silent threshold to flag as stale.",
)
@click.option("--all", "show_all", is_flag=True, help="Show recent and stale contacts in selected account threads.")
@click.option(
    "--domain",
    default=None,
    help="Restrict contacts to one email domain, for example acme-corp.example.com.",
)
@click.option(
    "--min-messages",
    type=click.IntRange(min=0),
    default=1,
    show_default=True,
    help="Minimum account-thread messages per contact.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=MAX_QUERY_LIMIT),
    default=DEFAULT_QUERY_LIMIT,
    show_default=True,
    help="Maximum contacts to return.",
)
@click.option(
    "--max-age-days",
    default=365,
    type=click.IntRange(min=0, max=36_500),
    show_default=True,
    help="Exclude contacts silent longer than this (0 disables the cutoff).",
)
@click.option(
    "--db",
    default=lambda: str(get_gmail_db_path()),
    help="Path to the managed Gmail cache.",
    show_default="<data/gmail.db>",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the decay report as JSON.")
def cli(
    account: str,
    days: int,
    show_all: bool,
    domain: str | None,
    min_messages: int,
    limit: int,
    max_age_days: int,
    db: str,
    as_json: bool,
) -> None:
    """Report contact recency from the ready published Gmail cache."""
    scope = configured_account_scope(get_accounts_config(strict=True), account)
    with connect(Path(db)) as connection:
        report = query_decay(
            connection,
            scope,
            DecayQuery(
                days_threshold=days,
                show_all=show_all,
                domain_filter=domain,
                min_messages=min_messages,
                limit=limit,
                max_age_days=None if max_age_days == 0 else max_age_days,
            ),
        )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "account": report.account,
                    "as_of": report.as_of,
                    "contacts": [asdict(contact) for contact in report.contacts],
                    "count": len(report.contacts),
                    "truncated": report.truncated,
                    "scan_truncated": report.scan_truncated,
                    "scanned_rows": report.scanned_rows,
                    "filters": {
                        "days": days,
                        "show_all": show_all,
                        "domain": domain,
                        "min_messages": min_messages,
                        "limit": limit,
                        "max_age_days": max_age_days,
                    },
                },
                sort_keys=True,
            )
        )
    else:
        click.echo(render_decay_text(report))
    if report.scan_truncated:
        raise GmailSyncPartialError("Gmail decay scan reached its bounded work budget")
