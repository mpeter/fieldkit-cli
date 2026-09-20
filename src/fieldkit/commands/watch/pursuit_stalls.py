#!/usr/bin/env python3
"""Pursuit stall watcher — CLI adapter.

Business logic lives in ``fieldkit.watch.pursuit_stalls``.
This module contains only the Click group and ack_cmd subcommand.

Usage:
    fieldkit watch run pursuit-stalls
    fieldkit watch run pursuit-stalls --threshold 21
    fieldkit watch run pursuit-stalls --account <account-slug>
    fieldkit watch run pursuit-stalls --dry-run
    fieldkit watch run pursuit-stalls ack <account>/<pursuit>

Exit codes:
    0  All accounts checked; zero or more alerts written.
    1  Fatal configuration or filesystem error.
"""

import datetime
import json
import logging

import click

from fieldkit.cli_exit import EXIT_DATA
from fieldkit.watch._pursuit_stall_render import _alerts_file
from fieldkit.watch._pursuit_stall_state import _SNOOZE_DAYS, snooze_pursuit
from fieldkit.watch.dedup import scrub_duplicate_alerts
from fieldkit.watch.pursuit_stalls import (
    _DEFAULT_STALL_DAYS,
    _run_pursuit_stalls,
)

__all__ = [
    "_DEFAULT_STALL_DAYS",
    "_alerts_file",
    "_run_pursuit_stalls",
]


# ---------------------------------------------------------------------------
# Click CLI
# ---------------------------------------------------------------------------


@click.group("pursuit-stalls", invoke_without_command=True)
@click.option(
    "--threshold",
    type=int,
    default=_DEFAULT_STALL_DAYS,
    show_default=True,
    help=("Global stall threshold in days. Override per account in accounts.yaml via stall_threshold_days."),
)
@click.option(
    "--account",
    metavar="KEY",
    default=None,
    help="Only check this account key (as defined in accounts.yaml). Default: all.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Check stalls and log what would be written; do not modify any files.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Run even if already ran today (bypass once-per-day guard). Used by run --all --force.",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Show which pursuits were skipped and why (terminal stage, missing last-transition, etc.).",
)
@click.option(
    "--scrub-duplicates",
    "scrub_duplicates",
    is_flag=True,
    default=False,
    help=(
        "Deduplicate pursuit-stall-alerts.md in-place. One-time operator action — "
        "keeps first occurrence of each alert per date section."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    threshold: int,
    account: str | None,
    dry_run: bool,
    force: bool,
    verbose: bool,
    scrub_duplicates: bool,
) -> None:
    """Pursuit stall watcher — detects pursuits stuck in the same stage for too long.

    Run without a subcommand to scan all pursuits and write alerts.

    Subcommands:
      ack <account>/<pursuit>  Snooze re-alerting for a pursuit for 7 days.
    """
    if ctx.invoked_subcommand is None:
        # implementation note: --scrub-duplicates deduplicates the alerts file then exits
        if scrub_duplicates:
            removed = scrub_duplicate_alerts(_alerts_file())
            click.echo(f"Scrubbed {removed} duplicate alert block(s) from {_alerts_file()}.")
            return

        if verbose:
            # Elevate debug-level skip reasons to INFO so they appear on stderr
            logging.getLogger("pursuit_stalls").setLevel(logging.DEBUG)
        raise SystemExit(_run_pursuit_stalls(threshold=threshold, account=account, dry_run=dry_run, force=force))


@cli.command("ack")
@click.argument("pursuit_key")
@click.option(
    "--days",
    type=int,
    default=_SNOOZE_DAYS,
    show_default=True,
    help="Snooze duration in days.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the snooze outcome as JSON.")
def ack_cmd(pursuit_key: str, days: int, as_json: bool) -> None:
    """Snooze re-alerting for a pursuit for N days (default: 7).

    PURSUIT_KEY must be in the form ``<account>/<pursuit-slug>`` — the same
    key shown in stall alert headings, e.g. ``acme-corp/virtualization-deal``.

    The snooze is cleared automatically when the pursuit's stage changes.
    State tracking continues while snoozed; only alert writes are suppressed.
    """
    try:
        snooze_pursuit(pursuit_key, days=days)
    except OSError as exc:
        click.echo(f"Error saving state: {exc}", err=True)
        raise SystemExit(EXIT_DATA) from None
    until = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=days)).isoformat()
    if as_json:
        click.echo(
            json.dumps(
                {"pursuit": pursuit_key, "snoozed_until": until, "days": days, "snoozed": True},
                indent=2,
                default=str,
            )
        )
        return
    click.echo(f"Snoozed {pursuit_key} until {until} ({days} days).")
