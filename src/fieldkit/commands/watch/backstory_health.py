#!/usr/bin/env python3
"""Backstory account health watcher — CLI adapter.

Business logic lives in ``fieldkit.watch.backstory_health``.
This module contains only the Click command adapter.

Usage:
    fieldkit watch run backstory-health
    fieldkit watch run backstory-health --threshold 50
    fieldkit watch run backstory-health --dry-run
    fieldkit watch run backstory-health --account <account-slug>

Exit codes:
    0  All accounts checked; zero or more alerts written.
    1  Retryable provider or persistence failure.
    2  Provider authentication requires user action.
    3  Invalid watcher or account configuration.
"""

import click

from fieldkit.cli_registry import declare_write
from fieldkit.watch.backstory_health import _DEFAULT_THRESHOLD, _run_backstory_health


@declare_write("workspace")
@click.command("backstory-health")
@click.option(
    "--threshold",
    type=int,
    default=_DEFAULT_THRESHOLD,
    show_default=True,
    help="Alert threshold for mean engagement_level. Override per account in accounts.yaml via health_score_threshold.",
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
    help="Check scores and log what would be written; do not modify any files.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the run outcome as JSON.")
def cli(threshold: int, account: str | None, dry_run: bool, as_json: bool) -> None:
    """Backstory account health watcher — detects engagement drops and writes alerts."""
    result = _run_backstory_health(threshold=threshold, account=account, dry_run=dry_run, as_json=as_json)
    raise SystemExit(result.exit_code)
