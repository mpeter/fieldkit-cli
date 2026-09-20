#!/usr/bin/env python3
"""Close-date countdown watcher — CLI adapter.

Business logic lives in ``fieldkit.watch.close_date_countdown``.
This module contains only the Click command.

Usage:
    fieldkit watch run close-date-countdown
    fieldkit watch run close-date-countdown --dry-run
    fieldkit watch run close-date-countdown --account acme

Exit codes:
    0  All pursuits checked; zero or more alerts written.
    1  Fatal configuration or filesystem error.
"""

import click

from fieldkit.cli_registry import declare_write

# Re-export get_fieldkit_home so existing tests can patch
# fieldkit.commands.watch.close_date_countdown.get_fieldkit_home.
# The domain module uses its own binding; this re-export is a patch target only.
from fieldkit.config import get_fieldkit_home  # isort: skip
from fieldkit.watch.close_date_countdown import (
    _COUNTDOWN_TIERS,
    _DEFAULT_GREEN_DAYS,
    _DEFAULT_RED_DAYS,
    _DEFAULT_YELLOW_DAYS,
    _RE_ALERT_DAYS,
    _TIER_EMOJI,
    _alerts_file,
    _build_live_keys,
    _classify_tier,
    _ensure_alerts_header,
    _extract_next_steps,
    _load_state,
    _parse_close_date,
    _process_pursuit_path,
    _prune_and_scan_pursuits,
    _run_countdown,
    _run_countdown_inner,
    _save_state,
    _state_file,
    _validate_accounts_config,
    append_countdown_alert,
    get_watchers_dir,
    should_suppress_countdown,
)

__all__ = [
    "_COUNTDOWN_TIERS",
    "_RE_ALERT_DAYS",
    "_TIER_EMOJI",
    "_alerts_file",
    "_build_live_keys",
    "_classify_tier",
    "_ensure_alerts_header",
    "_extract_next_steps",
    "_load_state",
    "_parse_close_date",
    "_process_pursuit_path",
    "_prune_and_scan_pursuits",
    "_run_countdown",
    "_run_countdown_inner",
    "_save_state",
    "_state_file",
    "_validate_accounts_config",
    "append_countdown_alert",
    "get_fieldkit_home",
    "get_watchers_dir",
    "should_suppress_countdown",
]


@declare_write("workspace")
@click.command("close-date-countdown")
@click.option(
    "--threshold-red", type=int, default=_DEFAULT_RED_DAYS, show_default=True, help="Days-to-close ≤ this → red tier."
)
@click.option(
    "--threshold-yellow",
    type=int,
    default=_DEFAULT_YELLOW_DAYS,
    show_default=True,
    help="Days-to-close ≤ this → yellow tier.",
)
@click.option(
    "--threshold-green",
    type=int,
    default=_DEFAULT_GREEN_DAYS,
    show_default=True,
    help="Days-to-close ≤ this → green tier.",
)
@click.option("--account", metavar="KEY", default=None, help="Only check this account key.")
@click.option("--dry-run", is_flag=True, help="Log what would be written; do not modify any files.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the run outcome as JSON.")
def cli(
    threshold_red: int,
    threshold_yellow: int,
    threshold_green: int,
    account: str | None,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Close-date countdown watcher with native qualification availability."""
    raise SystemExit(
        _run_countdown(
            threshold_red=threshold_red,
            threshold_yellow=threshold_yellow,
            threshold_green=threshold_green,
            account_filter=account,
            dry_run=dry_run,
            as_json=as_json,
        )
    )


if __name__ == "__main__":
    raise SystemExit(cli())
