"""Contract-expiry watcher — domain module.

Moved from ``commands/watch/contract_expiry.py`` (watch-domain-migration,
implementation change slice 2.8). No Click imports — pure business logic.

Scans accounts/*/pursuits/*.md for sf_close_date values and fires idempotent
alerts at configurable thresholds (default 60/30/14 days). Past-date pursuits
(not yet Completed or Closed) are flagged as EXPIRED.

Alert output:  fieldkit-data/watchers/contract-expiry-alerts.md
State sidecar: fieldkit-data/watchers/contract-expiry-state.json

Suppression: an alert for a given account/pursuit/tier is suppressed when the
same tier was already alerted in the state file.  Moving to a closer tier
always re-fires.
"""

import json
import logging
import time
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from fieldkit.config import get_fieldkit_home, get_watchers_dir
from fieldkit.errors import FieldkitError
from fieldkit.pursuit.io import load_pursuit, parse_frontmatter
from fieldkit.pursuit.stages import CLOSED_STAGES
from fieldkit.pursuit.utils import iterate_pursuits
from fieldkit.watch.dedup import alert_block_exists
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.state import merge_state
from fieldkit.watch.status import WatcherOutcome, write_run_status


@cache
def _alerts_file() -> Path:
    return get_watchers_dir() / "contract-expiry-alerts.md"


@cache
def _state_file() -> Path:
    return get_watchers_dir() / "contract-expiry-state.json"


# ---------------------------------------------------------------------------
# Tier thresholds and labels
# ---------------------------------------------------------------------------

# Ordered from most urgent to least urgent (for suppression comparison)
_TIERS = ("expired", "red", "orange", "yellow")

_TIER_EMOJI = {
    "expired": "💀",
    "red": "🔴",
    "orange": "🟠",
    "yellow": "🟡",
}

_COMPLETED_STAGES = frozenset({"Completed", "Closed", "completed", "closed"})
_STATE_KEY_NAMESPACE = "pursuit-sf-close-date/v1"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frontmatter parsing (inlined from commands/pursuit/audit._parse_frontmatter)
# ---------------------------------------------------------------------------


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str] | tuple[None, str]:
    """Extract YAML frontmatter and body from a markdown file.

    Returns (frontmatter_dict, body) or (None, content) on parse failure.

    Delegates to parse_frontmatter() for well-formed blocks.  Falls back to
    split-based parsing for edge cases such as empty frontmatter (``---\\n---``)
    that the regex-based parser does not match.

    Note: Inlined from commands/pursuit/audit._parse_frontmatter to avoid
    importing from commands/ in a domain module (tach boundary violation).
    """
    result = parse_frontmatter(content)
    if result is not None:
        return result
    return _parse_frontmatter_fallback(content)


def _parse_frontmatter_fallback(content: str) -> tuple[dict[str, Any], str] | tuple[None, str]:
    """Parse empty frontmatter blocks that the canonical parser omits."""
    # Fallback: handle empty frontmatter blocks (---\n---) that the regex skips.
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    try:
        fm: dict[str, Any] = yaml.safe_load(parts[1]) or {}
        return fm, parts[2]
    except yaml.YAMLError:
        return None, content


# ---------------------------------------------------------------------------
# Tier logic
# ---------------------------------------------------------------------------


def _classify_tier(
    days_until_end: int,
    *,
    critical: int = 14,
    warning: int = 30,
    notice: int = 60,
) -> str | None:
    """Return tier name or None if outside the alert window.

    Args:
        days_until_end: Days until contract end (negative = already expired).
        critical: Days threshold for the red (critical) tier. Default 14.
        warning:  Days threshold for the orange (warning) tier. Default 30.
        notice:   Days threshold for the yellow (notice) tier. Default 60.

    Returns:
        One of "expired", "red", "orange", "yellow", or None (outside window).
    """
    if -180 <= days_until_end < 0:
        return "expired"
    if days_until_end < -180:
        return None  # pursuit expired over 180 days ago — suppress from brief
    if days_until_end <= critical:
        return "red"
    if days_until_end <= warning:
        return "orange"
    if days_until_end <= notice:
        return "yellow"
    return None


def _classify_escalation_tier(days_overdue: int) -> str:
    """Return escalation tier label for expired contracts.

    Args:
        days_overdue: The negative days_until_end value (e.g. -45 for 45 days overdue).
                      abs() is applied internally.

    Returns:
        One of '0-30 days overdue', '30-90 days overdue', or '90+ days overdue'.
    """
    n = abs(days_overdue)
    if n <= 30:
        return "0-30 days overdue"
    if n <= 90:
        return "30-90 days overdue"
    return "90+ days overdue"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, Any]:
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
    """Persist expiry-state changes without overwriting concurrent updates."""
    merge_state(_state_file(), state, previous_state)


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def _tier_urgency(tier: str) -> int:
    """Higher = more urgent. Used to allow re-alerts when urgency increases."""
    return {
        "yellow": 1,
        "orange": 2,
        "red": 3,
        "expired": 4,
    }.get(tier, 0)


def should_suppress(key: str, current_tier: str, state: dict[str, Any]) -> bool:
    """Suppress when the same (or higher) tier was already alerted."""
    prior = state.get(key)
    if prior is None:
        return False
    prior_tier = prior.get("alerted_tier", "")
    # Re-alert only when urgency has increased
    return _tier_urgency(current_tier) <= _tier_urgency(prior_tier)


# ---------------------------------------------------------------------------
# Alert writer
# ---------------------------------------------------------------------------


def _ensure_alerts_header() -> None:
    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Contract Expiry Alerts\n\nAutomated alerts written by contract_expiry.py.\n",
            encoding="utf-8",
        )


def append_alert(result: dict[str, Any], dry_run: bool) -> bool:
    """Append a contract-expiry alert block and report whether it was written."""
    today_str = datetime.now(tz=UTC).date().isoformat()
    account = result["account"]
    pursuit = result["pursuit"]
    tier = result["tier"]
    emoji = _TIER_EMOJI[tier]
    days = result["days_until_end"]
    close_date = result["close_date"]
    sf_opportunity = result["sf_opportunity"]

    time_label = f"EXPIRED {abs(days)} day(s) ago" if tier == "expired" else f"expires in {days} day(s)"
    escalation = _classify_escalation_tier(days) if tier == "expired" else None

    lines = [
        "",
        f"## {today_str} — [pursuit-close-date] {account}/{pursuit} — {time_label} {emoji}",
        "",
        f"- **Account:** `{account}`",
        f"- **Pursuit:** `{pursuit}`",
        f"- **Tier:** {tier.upper()} ({emoji})",
        f"- **Days until close:** {days}",
        f"- **SF close date:** {close_date}",
        f"- **SF Opportunity:** {sf_opportunity or '(none)'}",
        f"- **Generated at:** {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ]
    if escalation is not None:
        lines.append(f"- **Escalation:** {escalation}")
    lines.append("")
    alert_text = "\n".join(lines)

    if dry_run:
        log.info("[DRY RUN] Would append alert:\n%s", alert_text)
        return True

    _ensure_alerts_header()
    heading_prefix = f"{today_str} — [pursuit-close-date] {account}/{pursuit}"
    if alert_block_exists(_alerts_file(), heading_prefix):
        return False

    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.info(
        "Contract-expiry alert written: account=%s pursuit=%s tier=%s days_until_close=%d",
        account,
        pursuit,
        tier,
        days,
    )
    return True


# ---------------------------------------------------------------------------
# Helpers extracted from _run_contract_expiry_inner
# ---------------------------------------------------------------------------


def _build_expiry_state_key(
    account_key: str,
    path: Path,
    tier: str,
    days_until_end: int,
    *,
    today: date | None = None,
    critical: int = 14,
    warning: int = 30,
    notice: int = 60,
) -> str:
    """Build the state dict key for a pursuit.

    Encodes the tier, expiry band (for expired pursuits), and the active threshold
    values so that changing thresholds between runs invalidates prior suppression
    state and causes re-alerting at the new tier boundaries (implementation note).

    historic regression: expired pursuits include the overdue band in the key so each band
    (0-30d, 30-90d, 90d+) fires exactly once rather than being permanently
    suppressed after the first expired alert.

    implementation note: the key also includes today's ISO date for the expired tier so that
    each calendar day produces a distinct key — prevents permanent within-band
    suppression after day 1, enabling daily re-alerting for expired pursuits.

    ``today`` is injected (not read from the system clock) so the key is
    timezone-consistent with the rest of the watcher (always UTC). Callers
    must pass ``datetime.now(tz=UTC).date()``.
    """
    threshold_suffix = f"/{critical}-{warning}-{notice}"
    if tier == "expired":
        days_past = abs(days_until_end)
        if days_past <= 30:
            expiry_band = "0-30d"
        elif days_past <= 90:
            expiry_band = "30-90d"
        else:
            expiry_band = "90d+"
        # implementation note: include the injected UTC date so each calendar day is a fresh key.
        # today must be provided by the caller using datetime.now(tz=UTC).date().
        if today is None:
            # Fallback: use UTC date — should not happen in production (caller always passes today)
            today = datetime.now(tz=UTC).date()
        return f"{_STATE_KEY_NAMESPACE}/{account_key}/{path.stem}/{tier}/{expiry_band}/{today.isoformat()}{threshold_suffix}"
    return f"{_STATE_KEY_NAMESPACE}/{account_key}/{path.stem}/{tier}{threshold_suffix}"


def _parse_close_date(fm: Any, path: Path) -> tuple[date | None, bool]:
    """Parse and coerce sf_close_date from pursuit frontmatter."""
    raw_close = getattr(fm, "sf_close_date", None)
    if not raw_close:
        log.debug("No sf_close_date in %s — skipping", path.stem)
        return None, True

    if isinstance(raw_close, datetime):
        raw_close = raw_close.date()

    try:
        close_date = raw_close if isinstance(raw_close, date) else date.fromisoformat(str(raw_close))
        return close_date, False
    except (ValueError, TypeError) as exc:
        log.warning("Unparseable sf_close_date %r in %s: %s", raw_close, path.stem, exc)
        return None, True


def _process_pursuit_path(
    path: Path,
    *,
    account_filter: str | None,
    today: date,
    updated_state: dict[str, Any],
    dry_run: bool,
    critical: int = 14,
    warning: int = 30,
    notice: int = 60,
) -> tuple[int, int, int, int]:
    """Process one pursuit path. Returns (checked, alerted, skipped, suppressed) deltas."""
    parts = path.parts
    try:
        pursuits_idx = next(i for i, p in enumerate(parts) if p == "pursuits")
    except StopIteration:
        log.warning("Unexpected path structure (no 'pursuits' segment): %s", path)
        return 0, 0, 1, 0

    account_key = parts[pursuits_idx - 1]

    if account_filter and account_key != account_filter:
        return 0, 0, 0, 0

    log.info("Scanning pursuit: account=%s pursuit=%s", account_key, path.stem)
    try:
        fm, _body, _mtime = load_pursuit(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot load pursuit %s: %s", path, exc)
        return 0, 0, 1, 0

    stage = str(getattr(fm, "stage", "") or "").strip().lower()
    if stage in CLOSED_STAGES:
        log.debug("Skipping terminal stage %r: %s", stage, path.stem)
        return 0, 0, 0, 0

    close_date, skip = _parse_close_date(fm, path)
    if skip or close_date is None:
        return 0, 0, 1, 0

    days_until_close = (close_date - today).days
    tier = _classify_tier(days_until_close, critical=critical, warning=warning, notice=notice)

    if tier is None:
        log.info(
            "SKIP (not in alert window): account=%s pursuit=%s days_until_close=%d "
            "source=pursuit.sf_close_date source_close_date=%s",
            account_key,
            path.stem,
            days_until_close,
            close_date.isoformat(),
        )
        return 0, 0, 0, 0

    sf_opportunity = str(getattr(fm, "sf_opportunity_id", "") or "").strip()
    key = _build_expiry_state_key(
        account_key,
        path,
        tier,
        days_until_close,
        today=today,
        critical=critical,
        warning=warning,
        notice=notice,
    )

    result: dict[str, Any] = {
        "account": account_key,
        "pursuit": path.stem,
        "tier": tier,
        "days_until_end": days_until_close,
        "close_date": close_date.isoformat(),
        "sf_opportunity": sf_opportunity,
    }

    log.info(
        "account=%s pursuit=%s tier=%s days_until_close=%d source=pursuit.sf_close_date source_close_date=%s",
        account_key,
        path.stem,
        tier,
        days_until_close,
        close_date.isoformat(),
    )

    # historic regression: read updated_state (not stale state) so that a pursuit processed
    # earlier in the same run is not double-alerted if its key was just written.
    suppressed = 0
    alerted = 0
    if should_suppress(key, tier, updated_state):
        log.debug("SUPPRESSED: %s already alerted at tier=%s", key, tier)
        suppressed = 1
    elif append_alert(result, dry_run):
        alerted = 1
        updated_state[key] = {
            "account": account_key,
            "pursuit": path.stem,
            "tier": tier,
            "days_until_end": days_until_close,
            "close_date": close_date.isoformat(),
            "alerted_tier": tier,
            "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    else:
        suppressed = 1

    return 1, alerted, 0, suppressed


# ---------------------------------------------------------------------------
# Core scan logic
# ---------------------------------------------------------------------------


def _run_contract_expiry(
    *,
    account_filter: str | None,
    dry_run: bool,
    critical: int = 14,
    warning: int = 30,
    notice: int = 60,
    as_json: bool = False,
) -> int:
    """Core logic; returns POSIX exit code."""
    with watcher_logging("contract-expiry"):
        return _run_contract_expiry_inner(
            account_filter=account_filter,
            dry_run=dry_run,
            critical=critical,
            warning=warning,
            notice=notice,
            as_json=as_json,
        )


def _watcher_outcome(checked: int, failures: int) -> WatcherOutcome:
    """Classify incomplete scans without hiding input failures."""
    if failures == 0:
        return "ok"
    return "fatal" if checked == 0 else "partial"


def _run_contract_expiry_inner(
    *,
    account_filter: str | None,
    dry_run: bool,
    critical: int = 14,
    warning: int = 30,
    notice: int = 60,
    as_json: bool = False,
) -> int:
    """Inner logic (separated for testability); returns POSIX exit code.

    ``as_json`` emits the run-status document on stdout in place of the
    ``[DRY-RUN]`` summary line; the exit code is unaffected.
    """
    # implementation note: validate thresholds before any I/O. Use FieldkitError (not
    # click.BadParameter/UsageError) to avoid P19a exit-code collision with EXIT_AUTH=2.
    if critical <= 0:
        raise FieldkitError(f"Invalid threshold: critical={critical} must be a positive integer greater than zero.")
    if critical >= warning:
        raise FieldkitError(f"Invalid thresholds: critical={critical} must be less than warning={warning}.")
    if warning >= notice:
        raise FieldkitError(f"Invalid thresholds: warning={warning} must be less than notice={notice}.")

    start = time.monotonic()
    today = datetime.now(tz=UTC).date()

    accounts_dir = get_fieldkit_home() / "accounts"
    if not accounts_dir.is_dir():
        log.error("Accounts directory not found: %s", accounts_dir)
        return 1

    pursuit_paths = [
        path
        for path in iterate_pursuits(get_fieldkit_home())
        if not path.parts[len(accounts_dir.parts)].startswith(".") and path.stem != "template"
    ]
    if not pursuit_paths:
        log.info("No pursuit files found under %s", accounts_dir)

    state = _load_state()
    updated_state: dict[str, Any] = dict(state)

    checked = 0
    alerted = 0
    skipped = 0
    suppressed = 0

    for path in pursuit_paths:
        c, a, s, sup = _process_pursuit_path(
            path,
            account_filter=account_filter,
            today=today,
            updated_state=updated_state,
            dry_run=dry_run,
            critical=critical,
            warning=warning,
            notice=notice,
        )
        checked += c
        alerted += a
        skipped += s
        suppressed += sup

    state_write_failed = False
    if not dry_run:
        try:
            _save_state(updated_state, previous_state=state)
        except OSError:
            log.error("State file write failed — contract-expiry state not persisted.")
            state_write_failed = True

    elapsed = time.monotonic() - start
    log.info(
        "Run complete: checked=%d alerted=%d suppressed=%d skipped=%d "
        "critical_days=%d warning_days=%d notice_days=%d source=pursuit.sf_close_date elapsed=%.1fs dry_run=%s",
        checked,
        alerted,
        suppressed,
        skipped,
        critical,
        warning,
        notice,
        elapsed,
        dry_run,
    )

    outcome = "fatal" if state_write_failed else _watcher_outcome(checked, skipped)
    write_run_status(
        watcher="contract-expiry",
        outcome=outcome,
        records_checked=checked,
        alerts_generated=alerted,
        failures=skipped,
        elapsed_seconds=elapsed,
        dry_run=dry_run,
    )
    if as_json:
        # The run happened — emit the outcome even when it is fatal, which is
        # exactly when a caller needs the detail. historic regression: `outcome` is reported
        # verbatim, including partial and fatal failures.
        print(
            json.dumps(
                {
                    "watcher": "contract-expiry",
                    "outcome": outcome,
                    "records_checked": checked,
                    "alerts_generated": alerted,
                    "failures": skipped,
                    "suppressed": suppressed,
                    "elapsed_seconds": round(elapsed, 1),
                    "dry_run": dry_run,
                },
                indent=2,
                default=str,
            )
        )
        return 1 if outcome != "ok" else 0
    # historic regression: print dry-run summary to stdout so --dry-run is useful as a preview.
    if dry_run:
        print(f"[DRY-RUN] contract-expiry: {checked} pursuit(s) scanned, {alerted} alert(s) would fire")
    return 1 if outcome != "ok" else 0


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
    "get_watchers_dir",
    "should_suppress",
]
