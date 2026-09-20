"""Close-date countdown watcher — domain module.

Moved from ``commands/watch/close_date_countdown.py`` (watch-domain-migration,
implementation change slice 2.6). No Click imports — pure business logic.

Scans active pursuit files, buckets them by days-to-close:

  Red    -- <=14 days  (or already overdue)
  Yellow -- 15-30 days
  Green  -- 31-60 days
  (skipped if >60 days or no close date)

Reports native qualification availability and writes idempotent alert blocks to:
  fieldkit-data/watchers/close-date-countdown-alerts.md

State is persisted as a JSON sidecar:
  fieldkit-data/watchers/close-date-countdown-state.json
"""

import json
import logging
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import get_accounts_config, get_fieldkit_home, get_watchers_dir
from fieldkit.pursuit import extract_champion_name, iterate_pursuits
from fieldkit.pursuit.io import load_pursuit
from fieldkit.pursuit.qualification import native_qualification_status
from fieldkit.pursuit.stages import CLOSED_STAGES as _CLOSED_STAGES
from fieldkit.watch.dedup import alert_block_exists
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.state import merge_state
from fieldkit.watch.state import state_write_failed as _state_write_failed
from fieldkit.watch.status import WatcherOutcome, write_run_status


@cache
def _alerts_file() -> Path:
    return get_watchers_dir() / "close-date-countdown-alerts.md"


@cache
def _state_file() -> Path:
    return get_watchers_dir() / "close-date-countdown-state.json"


# ---------------------------------------------------------------------------
# Tier thresholds
# ---------------------------------------------------------------------------

# Tiers (days) used for suppression comparison.  An alert is re-emitted only
# when the pursuit moves to a *different* (lower) tier since last alert.
_COUNTDOWN_TIERS = (14, 30, 60)

_TIER_EMOJI = {
    "red": "🔴",
    "yellow": "🟡",
    "green": "🟢",
}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier logic
# ---------------------------------------------------------------------------


def _classify_tier(
    days_to_close: int, *, threshold_red: int, threshold_yellow: int, threshold_green: int
) -> str | None:
    """Return tier name ('red', 'yellow', 'green') or None if out of range."""
    if days_to_close <= threshold_red:
        return "red"
    if days_to_close <= threshold_yellow:
        return "yellow"
    if days_to_close <= threshold_green:
        return "green"
    return None


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, Any]:
    """Load persisted countdown state; return {} if missing or unreadable."""
    if not _state_file().exists():
        return {}
    try:
        with _state_file().open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read state file %s: %s", _state_file(), exc)
        return {}


def _save_state(state: dict[str, Any], *, previous_state: dict[str, Any]) -> None:
    """Persist countdown-state changes without overwriting concurrent updates."""
    merge_state(_state_file(), state, previous_state)


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


_RE_ALERT_DAYS = 7  # historic regression: re-alert after this many days even if tier unchanged


def should_suppress_countdown(pursuit_key: str, current_tier: str, state: dict[str, Any]) -> bool:
    """Return True when the pursuit is still in the same countdown tier as last alert
    AND the last alert was less than _RE_ALERT_DAYS ago.

    First-time alerts (no prior state) are always emitted.
    Re-alerts fire after _RE_ALERT_DAYS days in the same tier (historic regression).
    """
    prior = state.get(pursuit_key)
    if prior is None:
        return False
    if prior.get("alerted_tier") != current_tier:
        return False
    # Same tier — check how long ago we last alerted.
    last_alerted = prior.get("last_alerted_date")
    if last_alerted is None:
        # Legacy state entry without last_alerted_date — suppress to avoid immediate spam.
        return True
    try:
        last_date = date.fromisoformat(last_alerted)
    except ValueError:
        return True  # malformed date — safe default: suppress
    days_since = (datetime.now(tz=UTC).date() - last_date).days
    return days_since < _RE_ALERT_DAYS


# ---------------------------------------------------------------------------
# Alert writer
# ---------------------------------------------------------------------------


def _ensure_alerts_header() -> None:
    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Close-Date Countdown Alerts\n\nAutomated alerts written by close_date_countdown.py.\n",
            encoding="utf-8",
        )


def append_countdown_alert(result: dict[str, Any], dry_run: bool) -> None:
    """Append a countdown alert block to close-date-countdown-alerts.md."""
    today_str = datetime.now(tz=UTC).date().isoformat()
    account = result["account"]
    pursuit = result["pursuit"]
    days = result["days_to_close"]
    tier = result["tier"]
    emoji = _TIER_EMOJI[tier]
    native_qualification = result["native_qualification"]
    champion = result["champion"]
    next_steps = result["next_steps"]
    rel_path = result["rel_path"]

    close_label = f"OVERDUE by {abs(days)} day(s)" if days <= 0 else f"closes in {days} day(s)"

    lines = [
        "",
        f"## {today_str} — {account}/{pursuit} — {close_label} {emoji}",
        "",
        f"- **Account:** `{account}`",
        f"- **Pursuit:** [{pursuit}]({rel_path})",
        f"- **Tier:** {tier.upper()} ({emoji})",
        f"- **Days to close:** {days}",
        f"- **Native qualification:** {native_qualification}",
        f"- **Champion:** {champion}",
        f"- **Next steps:** {next_steps}",
        f"- **Generated at:** {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
    ]
    alert_text = "\n".join(lines)

    if dry_run:
        log.info("[DRY RUN] Would append alert:\n%s", alert_text)
        return

    _ensure_alerts_header()
    heading_prefix = f"{today_str} — {account}/{pursuit}"
    if alert_block_exists(_alerts_file(), heading_prefix):
        return

    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.info(
        "Countdown alert written: %s/%s tier=%s days=%d",
        account,
        pursuit,
        tier,
        days,
    )


# ---------------------------------------------------------------------------
# Helpers extracted from _run_countdown_inner
# ---------------------------------------------------------------------------


def _build_live_keys(live_pursuit_paths: list[Path]) -> set[str]:
    """Build the set of live pursuit keys from a list of pursuit paths."""
    live_keys: set[str] = set()
    for _p in live_pursuit_paths:
        _parts = _p.parts
        _pursuits_idx = next((i for i, seg in enumerate(_parts) if seg == "pursuits"), None)
        if _pursuits_idx is not None and _pursuits_idx >= 1:
            live_keys.add(f"{_parts[_pursuits_idx - 1]}/{_p.stem}")
    return live_keys


def _parse_close_date(fm: Any, path: Path) -> tuple[date | None, bool]:
    """Parse and coerce the sf_close_date from frontmatter.

    Returns (close_date, skip) where skip=True means the pursuit should be skipped.
    """
    raw_close = getattr(fm, "sf_close_date", None)
    if not raw_close:
        log.debug("No sf_close_date in %s — skipping", path.stem)
        return None, True

    # historic regression: yaml.safe_load returns datetime.datetime for date fields.
    # datetime is a subclass of date, so isinstance(raw_close, date) is True,
    # but str(datetime_obj) gives "YYYY-MM-DD HH:MM:SS" which date.fromisoformat
    # rejects on Python < 3.11.  Coerce datetime → date before the isinstance check.
    if isinstance(raw_close, datetime):
        raw_close = raw_close.date()

    try:
        close_date = raw_close if isinstance(raw_close, date) else date.fromisoformat(str(raw_close))
        return close_date, False
    except (ValueError, TypeError) as exc:
        log.warning("Unparseable sf_close_date %r in %s: %s", raw_close, path.stem, exc)
        return None, True


def _extract_next_steps(fm: Any, body: str) -> str:
    """Extract next steps from frontmatter or body text."""
    next_steps = str(getattr(fm, "sf_next_steps", "") or "").strip()
    if not next_steps:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("next steps:"):
                next_steps = stripped[len("next steps:") :].strip()[:200]
                break
    if not next_steps:
        # implementation change: scan for a markdown "## Next Steps" section heading and
        # collect the first substantive line beneath it.
        in_next_steps_section = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("##") and "next steps" in stripped.lower():
                in_next_steps_section = True
                continue
            if in_next_steps_section:
                if stripped.startswith("#"):
                    break
                if stripped:
                    next_steps = stripped[:200]
                    break
    return next_steps or "(none)"


def _process_pursuit_path(
    path: Path,
    *,
    account_filter: str | None,
    today: date,
    threshold_red: int,
    threshold_yellow: int,
    threshold_green: int,
    updated_state: dict[str, Any],
    dry_run: bool,
) -> tuple[int, int, int, int]:
    """Process one pursuit path. Returns checked, alerted, skipped, and failure deltas."""
    parts = path.parts
    pursuits_idx = next((i for i, p in enumerate(parts) if p == "pursuits"), None)
    if pursuits_idx is None or pursuits_idx < 1:
        return 0, 0, 1, 1
    account_key = parts[pursuits_idx - 1]

    if account_filter and account_key != account_filter:
        return 0, 0, 0, 0

    try:
        fm, body, _mtime = load_pursuit(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot load pursuit %s: %s", path, exc)
        return 0, 0, 1, 1

    stage = str(getattr(fm, "stage", "") or "").strip().lower()
    if stage in _CLOSED_STAGES:
        log.debug("Skipping terminal stage %r: %s", stage, path.stem)
        return 0, 0, 0, 0

    close_date, skip = _parse_close_date(fm, path)
    if skip or close_date is None:
        return 0, 0, 1, int(bool(getattr(fm, "sf_close_date", None)))

    days_to_close = (close_date - today).days
    tier = _classify_tier(
        days_to_close,
        threshold_red=threshold_red,
        threshold_yellow=threshold_yellow,
        threshold_green=threshold_green,
    )
    if tier is None:
        log.info(
            "SKIP (out of range): account=%s pursuit=%s days_to_close=%d",
            account_key,
            path.stem,
            days_to_close,
        )
        return 0, 0, 0, 0

    account_dir = path.parent.parent
    champion = extract_champion_name(account_dir)
    next_steps = _extract_next_steps(fm, body)

    try:
        rel_path = str(path.relative_to(get_fieldkit_home()))
    except ValueError:
        rel_path = str(path)

    pursuit_key = f"{account_key}/{path.stem}"
    result: dict[str, Any] = {
        "account": account_key,
        "pursuit": path.stem,
        "tier": tier,
        "days_to_close": days_to_close,
        "close_date": close_date.isoformat(),
        "native_qualification": native_qualification_status(getattr(fm, "sf_opportunity_id", None)),
        "champion": champion,
        "next_steps": next_steps,
        "rel_path": rel_path,
    }

    log.info(
        "account=%s pursuit=%s tier=%s days_to_close=%d native_qualification=%s",
        account_key,
        path.stem,
        tier,
        days_to_close,
        result["native_qualification"],
    )

    # historic regression: use updated_state (not the original state snapshot) so that
    # within-run deduplication is consistent with across-run suppression.
    suppress = should_suppress_countdown(pursuit_key, tier, updated_state)
    alerted = 0
    if suppress:
        log.debug("SUPPRESSED: %s already alerted at tier=%s", pursuit_key, tier)
    else:
        append_countdown_alert(result, dry_run)
        alerted = 1

    today_iso = datetime.now(tz=UTC).date().isoformat()
    prior_entry = updated_state.get(pursuit_key, {})
    updated_state[pursuit_key] = {
        "account": account_key,
        "pursuit": path.stem,
        "tier": tier,
        "days_to_close": days_to_close,
        "close_date": close_date.isoformat(),
        "alerted_tier": tier if not suppress else prior_entry.get("alerted_tier", tier),
        # historic regression: track the date of the last actual alert for re-alert logic.
        "last_alerted_date": today_iso if not suppress else prior_entry.get("last_alerted_date", today_iso),
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    return 1, alerted, 0, 0


# ---------------------------------------------------------------------------
# Core scan logic
# ---------------------------------------------------------------------------

_DEFAULT_RED_DAYS: int = 14
_DEFAULT_YELLOW_DAYS: int = 30
_DEFAULT_GREEN_DAYS: int = 60


def _run_countdown(
    *,
    threshold_red: int,
    threshold_yellow: int,
    threshold_green: int,
    account_filter: str | None,
    dry_run: bool,
    as_json: bool = False,
) -> int:
    """Core logic; returns POSIX exit code."""
    with watcher_logging("close-date-countdown"):
        return _run_countdown_inner(
            threshold_red=threshold_red,
            threshold_yellow=threshold_yellow,
            threshold_green=threshold_green,
            account_filter=account_filter,
            dry_run=dry_run,
            as_json=as_json,
        )


def _validate_accounts_config(account_filter: str | None) -> tuple[dict[str, Any], int] | tuple[None, int]:
    """Validate accounts config and account_filter.

    Returns (accounts_dict, 0) on success or (None, 1) on error.
    Separated from _run_countdown_inner to reduce cyclomatic complexity.
    """
    config = get_accounts_config()
    accounts: dict[str, Any] = config.get("accounts", {})
    if not isinstance(accounts, dict) or not accounts:
        log.error("No accounts found in config")
        return None, 1
    if account_filter and account_filter not in accounts:
        log.error("Account %r not found in accounts.yaml", account_filter)
        return None, 1
    return accounts, 0


def _prune_and_scan_pursuits(
    *,
    state: dict[str, Any],
    account_filter: str | None,
    today: date,
    threshold_red: int,
    threshold_yellow: int,
    threshold_green: int,
    dry_run: bool,
) -> tuple[int, int, int, int, dict[str, Any]]:
    """Load pursuits, prune stale state, scan all paths.

    Returns (checked, alerted, skipped, failures, updated_state).
    Separated from _run_countdown_inner to reduce cyclomatic complexity (historic regression).
    """
    # historic regression: Prune stale state entries for pursuits that no longer exist on
    # disk.  Copying all keys (including deleted pursuits) caused ghost entries
    # to accumulate indefinitely.  Build a live-key set first, then filter.
    live_pursuit_paths = list(iterate_pursuits(get_fieldkit_home()))
    live_keys = _build_live_keys(live_pursuit_paths)
    updated_state: dict[str, Any] = {k: v for k, v in state.items() if k in live_keys}
    for _stale_key in set(state) - live_keys:
        log.info("historic regression: pruning stale state entry for deleted pursuit: %s", _stale_key)

    checked = 0
    alerted = 0
    skipped = 0
    failures = 0
    for path in live_pursuit_paths:
        c, a, s, f = _process_pursuit_path(
            path,
            account_filter=account_filter,
            today=today,
            threshold_red=threshold_red,
            threshold_yellow=threshold_yellow,
            threshold_green=threshold_green,
            updated_state=updated_state,
            dry_run=dry_run,
        )
        checked += c
        alerted += a
        skipped += s
        failures += f

    return checked, alerted, skipped, failures, updated_state


def _run_countdown_inner(
    *,
    threshold_red: int,
    threshold_yellow: int,
    threshold_green: int,
    account_filter: str | None,
    dry_run: bool,
    as_json: bool = False,
) -> int:
    """Inner logic (separated for testability); returns POSIX exit code.

    ``as_json`` emits the run-status document on stdout; the exit code is unaffected.
    """
    import time

    start = time.monotonic()
    today = datetime.now(tz=UTC).date()

    _accounts, err = _validate_accounts_config(account_filter)
    if err:
        return err

    state = _load_state()
    checked, alerted, skipped, failures, updated_state = _prune_and_scan_pursuits(
        state=state,
        account_filter=account_filter,
        today=today,
        threshold_red=threshold_red,
        threshold_yellow=threshold_yellow,
        threshold_green=threshold_green,
        dry_run=dry_run,
    )

    state_write_failed = _state_write_failed(
        dry_run=dry_run,
        write=lambda: _save_state(updated_state, previous_state=state),
        logger=log,
        message="State file write failed — countdown state not persisted.",
    )

    elapsed = time.monotonic() - start
    log.info(
        "Run complete: checked=%d alerted=%d skipped=%d elapsed=%.1fs dry_run=%s",
        checked,
        alerted,
        skipped,
        elapsed,
        dry_run,
    )
    outcome: WatcherOutcome = "fatal" if state_write_failed or (checked == 0 and failures > 0) else "ok"
    write_run_status(
        watcher="close-date-countdown",
        outcome=outcome,
        records_checked=checked,
        alerts_generated=alerted,
        failures=failures,
        elapsed_seconds=elapsed,
        dry_run=dry_run,
    )
    if as_json:
        # The run happened — emit the outcome even when it is fatal, which is
        # exactly when a caller needs the detail. historic regression: `outcome` is reported
        # verbatim; only "fatal" denotes a failed run.
        print(
            json.dumps(
                {
                    "watcher": "close-date-countdown",
                    "outcome": outcome,
                    "records_checked": checked,
                    "alerts_generated": alerted,
                    "failures": failures,
                    "elapsed_seconds": round(elapsed, 1),
                    "dry_run": dry_run,
                },
                indent=2,
                default=str,
            )
        )
    return 1 if outcome == "fatal" else 0


__all__ = [
    "_COUNTDOWN_TIERS",
    "_DEFAULT_GREEN_DAYS",
    "_DEFAULT_RED_DAYS",
    "_DEFAULT_YELLOW_DAYS",
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
    "get_watchers_dir",
    "should_suppress_countdown",
]
