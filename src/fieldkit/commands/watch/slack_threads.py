#!/usr/bin/env python3
"""Slack thread age watcher — CLI adapter.

Business logic lives in ``fieldkit.watch.slack_threads``.
This module contains only the Click command.

Usage:
    fieldkit watch run slack-threads
    fieldkit watch run slack-threads --threshold-hours 24
    fieldkit watch run slack-threads --account acme-corp
    fieldkit watch run slack-threads --limit 50
    fieldkit watch run slack-threads --dry-run

Exit codes:
    0  All selected accounts checked successfully.
    1  Provider or persistence failure; retry may help.
    2  Slack authentication requires user action.
    3  Invalid or missing configuration/account selection.
"""

import json

import click

from fieldkit.cli_registry import declare_write
from fieldkit.watch.slack_threads import (
    _DEFAULT_SEARCH_LIMIT,
    _DEFAULT_THRESHOLD_HOURS,
    SlackRunOutcome,
    _run_slack_threads,
)


def _render_json(outcome: SlackRunOutcome) -> str:
    """Render the public, sanitized watcher outcome document."""
    return json.dumps(
        {
            "watcher": "slack-threads",
            "outcome": outcome.run.outcome,
            "records_checked": outcome.records_checked,
            "alerts_generated": outcome.alerts_generated,
            "failures": outcome.failures,
            "auth_error": outcome.auth_error,
            "provider_error": outcome.provider_error,
            "elapsed_seconds": outcome.elapsed_seconds,
            "dry_run": outcome.dry_run,
        },
        indent=2,
    )


@declare_write("workspace")
@click.command("slack-threads")
@click.option(
    "--threshold-hours",
    type=int,
    default=_DEFAULT_THRESHOLD_HOURS,
    show_default=True,
    help="Alert threshold in hours. Threads older than this from a non-self sender are flagged.",
)
@click.option(
    "--account",
    metavar="KEY",
    default=None,
    help="Only check this account key (as defined in accounts.yaml). Default: all.",
)
@click.option(
    "--limit",
    type=int,
    default=_DEFAULT_SEARCH_LIMIT,
    show_default=True,
    help=(
        "Total number of Slack messages to fetch per account query. "
        "Use --limit-per-account to cap results per account independently."
    ),
)
@click.option(
    "--limit-per-account",
    "limit_per_account",
    type=int,
    default=None,
    help=(
        "Cap Slack search results per account (default: no per-account limit). "
        "When set, each account fetches at most this many results regardless of --limit. "
        "Useful when active accounts consume the full --limit quota."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Run checks and log what would be written; do not modify any files.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the run outcome as JSON.")
def cli(
    threshold_hours: int,
    account: str | None,
    limit: int,
    limit_per_account: int | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Slack thread age watcher — detects unanswered account-related threads."""
    outcome = _run_slack_threads(
        threshold_hours=threshold_hours,
        account=account,
        limit=limit,
        limit_per_account=limit_per_account,
        dry_run=dry_run,
    )
    if as_json:
        click.echo(_render_json(outcome))
    raise SystemExit(outcome.run.exit_code)
