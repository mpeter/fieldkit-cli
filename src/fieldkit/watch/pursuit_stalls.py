#!/usr/bin/env python3
"""Pursuit stall watcher — domain module.

Moved from ``commands/watch/pursuit_stalls.py`` (watch-domain-migration,
implementation change slice 2.11). No Click imports — pure business logic.

Note: cli() group callback and ack_cmd() stay in commands/watch/pursuit_stalls.py
because they use Click decorators and context.


Scans all pursuit files under fieldkit-data/accounts/*/pursuits/*.md and
detects pursuits that have been stuck in the same stage longer than a
configurable threshold (default: 14 days, or ``stall_threshold_days`` per
account in accounts.yaml).

Alerts are appended to:
  fieldkit-data/watchers/pursuit-stall-alerts.md

State is persisted as a JSON sidecar:
  fieldkit-data/watchers/pursuit-stall-state.json

Usage:
    fieldkit watch run pursuit-stalls
    fieldkit watch run pursuit-stalls --threshold 21
    fieldkit watch run pursuit-stalls --account <account-slug>
    fieldkit watch run pursuit-stalls --dry-run
    fieldkit watch run pursuit-stalls ack <account>/<pursuit>

Snooze / acknowledge:
    The ``ack`` subcommand silences re-alerting for a pursuit for 7 days.
    A snoozed pursuit's state is still tracked — only the alert output is
    suppressed.  When the pursuit's stage changes the snooze is automatically
    cleared so the new stall cycle alerts normally.

Exit codes:
    0  All accounts checked; zero or more alerts written.
    1  Fatal configuration or filesystem error.
"""

import datetime
import logging
from pathlib import Path
from typing import Any

import click

from fieldkit.config import get_accounts_config, get_config_path
from fieldkit.watch import _pursuit_stall_render as stall_render
from fieldkit.watch import _pursuit_stall_scan as stall_scan
from fieldkit.watch import _pursuit_stall_state as stall_state
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.status import WatcherOutcome, get_last_run_outcome, was_run_today, write_run_status

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_STALL_DAYS = 14  # configurable per account via stall_threshold_days

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


def _emit_stall_alert(
    result: dict[str, Any],
    prior: dict[str, Any] | None,
    prior_entry: dict[str, Any],
    file_threshold: int,
    *,
    dry_run: bool,
) -> tuple[bool, int]:
    """Emit stall alert if not snoozed/suppressed.

    Returns (suppress, snoozed_delta).
    """
    snoozed, snooze_reason = stall_state.is_snoozed(prior_entry, current_stage=result["stage"])
    if snoozed:
        log.debug("SNOOZED: %s/%s — %s", result["account"], result["pursuit"], snooze_reason)
        return True, 1

    suppress, reason = stall_state.should_suppress_alert(result, prior)
    if suppress:
        log.debug("SUPPRESSED: %s/%s — %s", result["account"], result["pursuit"], reason)
    else:
        stall_render.append_stall_alert(result, dry_run=dry_run)
        log.info(
            "STALL: %s/%s stage=%r days=%d (threshold=%d)",
            result["account"],
            result["pursuit"],
            result["stage"],
            result["days_since_transition"],
            file_threshold,
        )
    return suppress, 0


def _process_stall_entry(
    *,
    result: dict[str, Any],
    prior: dict[str, Any] | None,
    state_key: str,
    file_threshold: int,
    today: datetime.date,
    updated_state: dict[str, Any],
    dry_run: bool,
) -> tuple[int, int, int, dict[str, Any]]:
    """Process one pursuit stall entry. Returns (checked_delta, stalled_delta, snoozed_delta, updated_state)."""
    prior_entry: dict[str, Any] = dict(prior) if prior else {}

    result, stage_changed = stall_state.detect_stage_change(result, prior, prior_entry)

    # historic regression: apply detected_transition_date sentinel logic
    result = stall_state.apply_detected_transition_date(result, prior, stage_changed=stage_changed, today=today)

    stalled_delta = 0
    snoozed_delta = 0
    suppress = False

    if result["is_stalled"]:
        stalled_delta = 1
        suppress, snoozed_delta = _emit_stall_alert(result, prior, prior_entry, file_threshold, dry_run=dry_run)
    else:
        log.info(
            "OK: %s/%s stage=%r days=%d (threshold=%d)",
            result["account"],
            result["pursuit"],
            result["stage"],
            result["days_since_transition"],
            file_threshold,
        )

    new_entry = stall_state.build_stall_state_entry(
        result, prior, prior_entry, file_threshold, stage_changed=stage_changed, suppress=suppress
    )
    updated_state[state_key] = new_entry
    return 1, stalled_delta, snoozed_delta, updated_state


def _check_already_ran(dry_run: bool, force: bool = False) -> int | None:
    """historic regression/historic regression: skip or error if already ran today.

    Returns an exit code (0 or 1) if the run should be skipped, or None to proceed.
    Pass force=True (historic regression) to bypass the guard unconditionally.
    """
    if force or dry_run or not was_run_today("pursuit-stalls"):
        return None
    prior_outcome = get_last_run_outcome("pursuit-stalls")
    if prior_outcome == "fatal":
        log.warning(
            "pursuit-stalls last run today had outcome=fatal — "
            "re-run with --force to investigate or check watcher-run-status.json"
        )
        return 1
    log.info("pursuit-stalls already ran today; skipping (use --force to override)")
    # historic regression: write zero counts on skip so watcher-run-status.json reflects the
    # skip rather than retaining stale counts from the previous real run.
    write_run_status(
        watcher="pursuit-stalls",
        outcome="ok",
        records_checked=0,
        alerts_generated=0,
        failures=0,
        elapsed_seconds=0.0,
        dry_run=False,
    )
    return 0


def _load_and_validate_accounts(
    account: str | None,
    threshold: int,
) -> dict[str, Any] | None:
    """Load accounts config, validate, and inject CLI threshold defaults.

    Returns the accounts dict, or None on fatal error.
    """
    config = get_accounts_config()
    accounts: dict[str, Any] = config.get("accounts", {})
    if not isinstance(accounts, dict) or not accounts:
        log.error("No accounts found in %s", get_config_path("accounts.yaml"))
        return None
    if account and account not in accounts:
        log.error("Account %r not found in accounts.yaml", account)
        return None
    # Inject CLI threshold as default for accounts without per-account override
    for acc_cfg in accounts.values():
        if isinstance(acc_cfg, dict) and "stall_threshold_days" not in acc_cfg:
            acc_cfg["stall_threshold_days"] = threshold
    return accounts


def _prune_state_for_account(
    state: dict[str, Any],
    account: str | None,
    pursuit_files: list[tuple[Path, int]],
    failed_accounts: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prune stale state entries and return (working_state, updated_state).

    historic regression: when --account is set, only prune entries for that account so
    other accounts' state is not clobbered. Failed account collection also
    preserves that account's state because absence from the file list is unknown.
    """
    retained_state, prunable_state = _partition_failed_account_state(state, failed_accounts)
    if account is not None:
        scoped_state = {k: v for k, v in prunable_state.items() if k.startswith(f"{account}/")}
        pruned_scoped = stall_state.prune_stale_state(scoped_state, pursuit_files)
        updated_state: dict[str, Any] = {k: v for k, v in prunable_state.items() if not k.startswith(f"{account}/")}
        updated_state.update(retained_state)
        updated_state.update(pruned_scoped)
        return dict(updated_state), updated_state
    else:
        pruned = stall_state.prune_stale_state(prunable_state, pursuit_files)
        pruned.update(retained_state)
        return pruned, dict(pruned)


def _partition_failed_account_state(
    state: dict[str, Any], failed_accounts: set[str] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate state that must survive a failed account collection."""
    failed_accounts = failed_accounts or set()
    retained_state = {key: value for key, value in state.items() if key.split("/", maxsplit=1)[0] in failed_accounts}
    prunable_state = {
        key: value for key, value in state.items() if key.split("/", maxsplit=1)[0] not in failed_accounts
    }
    return retained_state, prunable_state


def _scan_pursuit_files(
    pursuit_files: list[tuple[Path, int]],
    state: dict[str, Any],
    updated_state: dict[str, Any],
    today: datetime.date,
    *,
    dry_run: bool,
) -> tuple[int, int, int, int, int, dict[str, Any]]:
    """Scan all pursuit files and process stall entries.

    Returns (checked, stalled_count, snoozed_count, data_failures,
    intentional_exclusions, updated_state).
    """
    checked = stalled_count = snoozed_count = data_failures = intentional_exclusions = 0
    for path, file_threshold in pursuit_files:
        result, reason, intentional = stall_scan.scan_pursuit_file_details(
            path, threshold_days=file_threshold, today=today
        )
        if result is None:
            failures, exclusions = _record_scan_skip(path, reason, intentional)
            data_failures += failures
            intentional_exclusions += exclusions
            continue
        s_delta, sup_delta, updated_state = _process_scanned_pursuit(
            result, state, updated_state, file_threshold, today, dry_run
        )
        checked += 1
        stalled_count += s_delta
        snoozed_count += sup_delta
    return checked, stalled_count, snoozed_count, data_failures, intentional_exclusions, updated_state


def _record_scan_skip(path: Path, reason: str | None, intentional: bool) -> tuple[int, int]:
    """Log a skipped scan and return its failure and exclusion deltas."""
    assert reason is not None
    if intentional:
        log.info("pursuit-stalls: excluding %s — %s", path, reason)
        return 0, 1
    log.warning("pursuit-stalls: cannot scan %s — %s", path, reason)
    return 1, 0


def _process_scanned_pursuit(
    result: dict[str, Any],
    state: dict[str, Any],
    updated_state: dict[str, Any],
    file_threshold: int,
    today: datetime.date,
    dry_run: bool,
) -> tuple[int, int, dict[str, Any]]:
    """Process one successfully parsed pursuit and return its alert deltas."""
    state_key = f"{result['account']}/{result['pursuit']}"
    _checked, stalled, snoozed, updated_state = _process_stall_entry(
        result=result,
        prior=state.get(state_key),
        state_key=state_key,
        file_threshold=file_threshold,
        today=today,
        updated_state=updated_state,
        dry_run=dry_run,
    )
    return stalled, snoozed, updated_state


def _save_stall_state(updated_state: dict[str, Any], previous_state: dict[str, Any], *, dry_run: bool) -> bool:
    """Persist state and report whether a real write failed."""
    if dry_run:
        return False
    try:
        stall_state.save_state(updated_state, previous_state=previous_state)
    except OSError:
        log.error("State file write failed — stall state not persisted.", exc_info=True)
        return True
    return False


def _watcher_outcome(checked: int, data_failures: int, intentional_exclusions: int) -> WatcherOutcome:
    """Classify an empty scan as fatal unless it consisted only of intentional exclusions."""
    if checked == 0 and (data_failures > 0 or intentional_exclusions == 0):
        return "fatal"
    return "partial" if data_failures else "ok"


def _run_pursuit_stalls(
    *,
    threshold: int,
    account: str | None,
    dry_run: bool,
    force: bool = False,
) -> int:
    """Core logic; returns POSIX exit code."""
    import time

    start = time.monotonic()
    today = datetime.datetime.now(tz=datetime.UTC).date()
    with watcher_logging("pursuit-stalls"):
        # historic regression / historic regression: skip if already ran today (historic regression: force bypasses)
        skip_code = _check_already_ran(dry_run, force=force)
        if skip_code is not None:
            return skip_code

        accounts = _load_and_validate_accounts(account, threshold)
        if accounts is None:
            return 1

        config = get_accounts_config()
        persisted_state = stall_state.load_state()

        pursuit_files, intentional_exclusions, data_failures, failed_accounts = (
            stall_scan.collect_pursuit_files_details(accounts_config=config, account_filter=account)
        )
        log.info("Scanning %d pursuit file(s) across account(s): %s", len(pursuit_files), account or "all")

        # historic regression / historic regression: prune stale state entries
        state, updated_state = _prune_state_for_account(persisted_state, account, pursuit_files, failed_accounts)

        checked, stalled_count, snoozed_count, scan_failures, scan_exclusions, updated_state = _scan_pursuit_files(
            pursuit_files, state, updated_state, today, dry_run=dry_run
        )
        data_failures += scan_failures
        intentional_exclusions += scan_exclusions

        state_write_failed = _save_stall_state(updated_state, persisted_state, dry_run=dry_run)

        elapsed = time.monotonic() - start
        run_ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        stall_render.append_run_summary_header(
            run_ts=run_ts,
            checked=checked,
            stalled=stalled_count,
            snoozed=snoozed_count,
            dry_run=dry_run,
        )
        log.info(
            "Run complete: checked=%d stalled=%d snoozed=%d data_failures=%d intentional_exclusions=%d "
            "elapsed=%.1fs dry_run=%s",
            checked,
            stalled_count,
            snoozed_count,
            data_failures,
            intentional_exclusions,
            elapsed,
            dry_run,
        )
        outcome = "fatal" if state_write_failed else _watcher_outcome(checked, data_failures, intentional_exclusions)
        write_run_status(
            watcher="pursuit-stalls",
            outcome=outcome,
            records_checked=checked,
            alerts_generated=stalled_count,
            failures=data_failures + int(state_write_failed),
            elapsed_seconds=elapsed,
            dry_run=dry_run,
        )
        # historic regression: print dry-run summary to stdout so --dry-run is useful as a preview.
        if dry_run:
            click.echo(
                f"[DRY-RUN] pursuit-stalls: {checked} pursuit(s) scanned, {stalled_count} stall alert(s) would fire"
            )
        # historic regression: surface partial failures to stderr so operators know without
        # having to inspect watcher-run-status.json manually.
        if outcome == "fatal":
            message = (
                "ERROR: pursuit-stall state was not persisted — resolve the storage failure before rerunning"
                if state_write_failed
                else "ERROR: no pursuits were scanned — use --verbose to diagnose the watcher input"
            )
            click.echo(message, err=True)
            return 1
        if data_failures > 0:
            click.echo(
                f"NOTE: {data_failures} pursuit(s) could not be scanned — use --verbose to see details",
                err=True,
            )
            return 1
        return 0
