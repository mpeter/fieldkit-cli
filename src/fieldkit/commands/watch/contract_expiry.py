#!/usr/bin/env python3
"""Contract-expiry watcher — CLI adapter.

Business logic lives in ``fieldkit.watch.contract_expiry``.
This module contains only the Click command.

Usage:
    fieldkit watch run contract-expiry
    fieldkit watch run contract-expiry --dry-run
    fieldkit watch run contract-expiry --account acme
    fieldkit watch run contract-expiry --threshold-critical 7 --threshold-warning 21 --threshold-notice 45

Exit codes:
    0  Run completed; zero or more alerts written.
    1  Fatal configuration or filesystem error.
"""

import click

from fieldkit.cli_registry import declare_write
from fieldkit.watch.contract_expiry import (
    _TIER_EMOJI,
    _TIERS,
    _alerts_file,
    _build_expiry_state_key,
    _classify_escalation_tier,
    _classify_tier,
    _ensure_alerts_header,
    _load_state,
    _parse_close_date,
    _process_pursuit_path,
    _run_contract_expiry,
    _run_contract_expiry_inner,
    _save_state,
    _state_file,
    _tier_urgency,
    append_alert,
    get_watchers_dir,
    should_suppress,
)

__all__ = [
    "_TIERS",
    "_TIER_EMOJI",
    "_alerts_file",
    "_build_expiry_state_key",
    "_classify_escalation_tier",
    "_classify_tier",
    "_ensure_alerts_header",
    "_load_state",
    "_parse_close_date",
    "_process_pursuit_path",
    "_run_contract_expiry",
    "_run_contract_expiry_inner",
    "_save_state",
    "_state_file",
    "_tier_urgency",
    "append_alert",
    "get_fieldkit_home",
    "get_watchers_dir",
    "should_suppress",
    "write_run_status",
]

# Re-export so existing patches on this module still work.
from fieldkit.config import get_fieldkit_home
from fieldkit.watch.status import write_run_status


@declare_write("workspace")
@click.command("contract-expiry")
@click.option("--account", metavar="KEY", default=None, help="Only check this account key.")
@click.option("--dry-run", is_flag=True, help="Log what would be written; do not modify any files.")
@click.option(
    "--threshold-notice",
    "threshold_notice",
    default=60,
    show_default=True,
    help="Days before expiry for the notice (yellow) tier.",
)
@click.option(
    "--threshold-warning",
    "threshold_warning",
    default=30,
    show_default=True,
    help="Days before expiry for the warning (orange) tier.",
)
@click.option(
    "--threshold-critical",
    "threshold_critical",
    default=14,
    show_default=True,
    help=(
        "Days before expiry for the critical (red) tier. "
        "Must satisfy 0 < critical < warning < notice. "
        "Custom thresholds must be used consistently across runs — changing them "
        "invalidates prior suppression state and causes re-alerting. "
        "fieldkit watch run --all always uses default thresholds (60/30/14)."
    ),
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the run outcome as JSON.")
def cli(
    account: str | None,
    dry_run: bool,
    threshold_notice: int,
    threshold_warning: int,
    threshold_critical: int,
    as_json: bool,
) -> None:
    """Contract-expiry watcher — alerts at configurable thresholds (default 60/30/14 days).

    Scans accounts/*/pursuits/*.md for sf_close_date values and fires tiered alerts.
    Past-date pursuits are flagged EXPIRED. Thresholds are encoded in the suppression
    state key so changing them between runs causes re-alerting at the new boundaries.

    Example with custom thresholds:

        fieldkit watch run contract-expiry --threshold-notice 90 --threshold-warning 45 --threshold-critical 7
    """
    raise SystemExit(
        _run_contract_expiry(
            account_filter=account,
            dry_run=dry_run,
            critical=threshold_critical,
            warning=threshold_warning,
            notice=threshold_notice,
            as_json=as_json,
        )
    )


if __name__ == "__main__":
    raise SystemExit(cli())
