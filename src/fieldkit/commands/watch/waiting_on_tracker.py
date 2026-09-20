#!/usr/bin/env python3
"""Waiting-on tracker watcher — CLI wiring.

Business logic lives in ``fieldkit.watch.waiting_on_tracker``; this file is
Click wiring only (parse args → call domain → exit with code).

Usage:
    fieldkit watch run waiting-on-tracker
    fieldkit watch run waiting-on-tracker --threshold 14
    fieldkit watch run waiting-on-tracker --dry-run

Exit codes:
    0  Completed (with or without alerts); also exits 0 if TASKS.md is missing.
    1  Fatal filesystem or configuration error.
"""

import click

from fieldkit.cli_registry import declare_write
from fieldkit.watch.waiting_on_tracker import _DEFAULT_THRESHOLD_DAYS, _run

# ---------------------------------------------------------------------------
# Click CLI
# ---------------------------------------------------------------------------


@declare_write("workspace")
@click.command("waiting-on-tracker")
@click.option(
    "--threshold",
    type=int,
    default=_DEFAULT_THRESHOLD_DAYS,
    show_default=True,
    help="Days of silence before an item triggers an escalation alert.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Check items and log what would be written; do not modify any files.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the run outcome as JSON.")
def cli(threshold: int, dry_run: bool, as_json: bool) -> None:
    """Waiting-on tracker — escalate TASKS.md items silent beyond threshold."""
    raise SystemExit(_run(threshold=threshold, dry_run=dry_run, as_json=as_json))


if __name__ == "__main__":
    raise SystemExit(cli())
