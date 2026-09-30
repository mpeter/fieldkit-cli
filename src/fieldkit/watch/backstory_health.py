"""Backstory account health watcher — domain module.

Moved from ``commands/watch/backstory_health.py`` (watch-domain-migration,
implementation change slice 2.7). No Click imports — pure business logic.

Checks account engagement health for all configured accounts via an explicitly
configured Backstory MCP endpoint. Detects drops below a configurable threshold and appends dated alerts to
fieldkit-data/watchers/backstory-alerts.md.

Health score is the mean engagement_level across all opportunities returned
by backstory__find_account.  This is the only structured numeric metric
Backstory exposes at the account level.

State is persisted in fieldkit-data/watchers/backstory-health-state.json so
each run can compute deltas against the previous run.
"""

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import ConfigError, get_accounts_config, get_mcp_endpoint, get_watchers_dir
from fieldkit.errors import AuthError
from fieldkit.watch.dedup import alert_block_exists
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.mcp import MCPSession as MCPSession
from fieldkit.watch.state import merge_state
from fieldkit.watch.state import state_write_failed as _state_write_failed
from fieldkit.watch.status import WatcherOutcome, WatcherRunResult, write_run_status

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------


@cache
def _alerts_file() -> Path:
    return get_watchers_dir() / "backstory-alerts.md"


@cache
def _state_file() -> Path:
    return get_watchers_dir() / "backstory-health-state.json"


# ---------------------------------------------------------------------------
# MCP gateway
# ---------------------------------------------------------------------------

_MCP_TIMEOUT = 30  # seconds per HTTP call
_DEFAULT_THRESHOLD = 60  # engagement_level below this → alert

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


class _AlertPublicationError(OSError):
    """A required alert failed, optionally following a failed lookup."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__("Backstory alert publication failed")
        self.failures = failures


@dataclass(frozen=True)
class _AccountScanResult:
    accounts_attempted: int
    accounts_checked: int
    alerts_generated: int
    api_failures: int
    publication_failures: int
    updated_state: dict[str, Any]


# ---------------------------------------------------------------------------
# Backstory helpers
# ---------------------------------------------------------------------------


def compute_health_score(opportunities: list[dict[str, Any]]) -> float | None:
    """Return a validated mean engagement level, or ``None`` when unavailable."""
    if not opportunities:
        return None
    levels: list[float] = []
    for opportunity in opportunities:
        raw = opportunity.get("engagement_level")
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return None
        level = float(raw)
        if not math.isfinite(level) or not 0 <= level <= 100:
            return None
        levels.append(level)
    return sum(levels) / len(levels)


def get_risk_count(session: MCPSession, account_id: int) -> int:
    """Return the number of risk bullets in the account status narrative.

    The risk count is included in the alert as a qualitative context signal.
    Returns -1 on failure (so callers can log a warning without crashing).
    """
    try:
        text = session.call_tool("backstory__get_account_status", {"peopleai_account_id": account_id})
    except RuntimeError:
        log.warning("Backstory risk context request failed")
        return -1

    if not isinstance(text, str):
        return -1

    # Count lines starting with " - " under a "Risks:" section
    in_risks = False
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("risks:"):
            in_risks = True
            continue
        if in_risks:
            if stripped.startswith("-") or stripped.startswith("*"):
                count += 1
            elif stripped and not stripped.startswith(" "):
                # New section header — stop counting
                in_risks = False
    return count


# ---------------------------------------------------------------------------
# Account config
# ---------------------------------------------------------------------------


def account_threshold(account_cfg: dict[str, Any], default: int) -> int:
    """Return health_score_threshold for an account, falling back to *default*."""
    val = account_cfg.get("health_score_threshold", default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# State (sidecar JSON)
# ---------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    """Load the persisted health state; return {} if missing or unreadable."""
    if not _state_file().exists():
        return {}
    try:
        with _state_file().open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        log.warning("Backstory state could not be read; prior deltas are unavailable")
        return {}


def save_state(state: dict[str, Any], *, previous_state: dict[str, Any] | None = None) -> None:
    """Persist health-state changes without overwriting concurrent updates."""
    merge_state(_state_file(), state, previous_state or {})


# ---------------------------------------------------------------------------
# Alert writer
# ---------------------------------------------------------------------------


def append_alert(
    *,
    account_key: str,
    current_score: float,
    previous_score: float | None,
    threshold: int,
    risk_count: int,
    dry_run: bool,
) -> bool:
    """Append a dated alert entry and report whether it was published."""
    now_utc = datetime.now(UTC)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now_utc.strftime("%Y-%m-%d")

    delta_str = ""
    if previous_score is not None:
        delta = current_score - previous_score
        delta_str = f" (Δ {delta:+.1f} from previous {previous_score:.1f})"

    risk_str = f", {risk_count} active risks" if risk_count >= 0 else ""
    lines = [
        f"\n## {date_label} — {account_key} health alert",
        "",
        f"- **Account:** `{account_key}`",
        f"- **Health score:** {current_score:.1f}{delta_str}",
        f"- **Threshold:** {threshold}{risk_str}",
        f"- **Timestamp:** {ts}",
        "",
    ]
    alert_text = "\n".join(lines)

    if dry_run:
        log.info("[DRY RUN] Would append one Backstory health alert")
        return False

    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Backstory Health Alerts\n\nAutomated alerts written by watch_backstory_health.py.\n", encoding="utf-8"
        )

    heading_prefix = f"{date_label} — {account_key} health alert"
    if alert_block_exists(_alerts_file(), heading_prefix):
        return False

    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.info("Backstory health alert written")
    return True


def append_api_error(account_key: str, error_msg: str, dry_run: bool) -> None:
    """Append a dated API error line to backstory-alerts.md."""
    now_utc = datetime.now(UTC)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now_utc.strftime("%Y-%m-%d")
    line = f"\n## {date_label} — {account_key} API error\n\n- **Error:** {error_msg}\n- **Timestamp:** {ts}\n"

    if dry_run:
        log.info("[DRY RUN] Would append one Backstory provider failure")
        return

    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Backstory Health Alerts\n\nAutomated alerts written by watch_backstory_health.py.\n", encoding="utf-8"
        )
    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(line)
    log.warning("Backstory provider failure recorded")


def _publish_api_error(account_key: str, error_msg: str, dry_run: bool) -> None:
    try:
        append_api_error(account_key, error_msg, dry_run)
    except (AuthError, ConfigError):
        raise
    except Exception:  # noqa: BLE001 -- publication failures must remain fatal
        raise _AlertPublicationError(failures=2) from None


# ---------------------------------------------------------------------------
# Per-account check
# ---------------------------------------------------------------------------


def check_account(
    *,
    session: MCPSession,
    account_key: str,
    account_cfg: dict[str, Any],
    state: dict[str, Any],
    default_threshold: int,
    dry_run: bool,
) -> dict[str, Any] | None:
    """Check one account; return updated state entry or None on total failure.

    Returns a dict with at least:
        {"health_score": float, "checked_at": str}
    """
    threshold = account_threshold(account_cfg, default_threshold)

    # ---- Step 1: find the account in Backstory -------------------------
    # Use the configured display keywords or account key as search name
    keywords: list[str] = account_cfg.get("keywords", [])
    search_name = keywords[0] if keywords else account_key

    log.info("Checking configured account (threshold=%d)", threshold)

    try:
        result = session.call_tool("backstory__find_account", {"account_name": search_name})
    except (AuthError, ConfigError):
        raise
    except RuntimeError:
        error_msg = "Backstory account lookup failed; retry later."
        log.warning("Backstory account lookup failed")
        _publish_api_error(account_key, error_msg, dry_run)
        return None

    if not isinstance(result, dict):
        error_msg = "Backstory account lookup returned invalid data."
        log.warning("Backstory account lookup returned invalid data")
        _publish_api_error(account_key, error_msg, dry_run)
        return None

    account_id = result.get("peopleai_account_id")
    if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id <= 0:
        error_msg = "Backstory account lookup omitted its required identifier."
        log.warning("Backstory account lookup omitted its required identifier")
        _publish_api_error(account_key, error_msg, dry_run)
        return None

    opportunities = result.get("opportunities")
    if not isinstance(opportunities, list) or not all(isinstance(item, dict) for item in opportunities):
        error_msg = "Backstory account lookup returned invalid opportunity data."
        log.warning("Backstory account lookup returned invalid opportunity data")
        _publish_api_error(account_key, error_msg, dry_run)
        return None

    # ---- Step 2: compute health score ---------------------------------
    current_score = compute_health_score(opportunities)
    if current_score is None:
        error_msg = "Backstory account lookup returned no usable engagement scores."
        log.warning("Backstory account lookup returned no usable engagement scores")
        _publish_api_error(account_key, error_msg, dry_run)
        return None
    log.info("Account score computed from %d opportunities", len(opportunities))

    # ---- Step 3: read previous state ----------------------------------
    prev_entry = state.get(account_key, {})
    raw_previous_score = prev_entry.get("health_score") if isinstance(prev_entry, dict) else None
    previous_score = (
        float(raw_previous_score)
        if isinstance(raw_previous_score, int | float)
        and not isinstance(raw_previous_score, bool)
        and math.isfinite(raw_previous_score)
        and 0 <= raw_previous_score <= 100
        else None
    )

    # ---- Step 4: check threshold & emit alert -------------------------
    if current_score < threshold:
        # Suppress re-alert when score is unchanged (delta == 0).
        # First detection (previous_score is None) always alerts.
        # Subsequent runs only alert when the score actually changed.
        # Threshold tightened from 1.0 to 0.01: a 1.0 tolerance was suppressing
        # genuine 1-point score changes (e.g. 72 → 71). Float equality noise is
        # well below 0.01 for scores stored as rounded integers (historic regression).
        score_unchanged = previous_score is not None and abs(current_score - previous_score) < 0.01
        if score_unchanged:
            log.info("Account score is unchanged; suppressing repeat alert")
            _alert_written = False
        else:
            risk_count = get_risk_count(session, account_id)
            try:
                _alert_written = append_alert(
                    account_key=account_key,
                    current_score=current_score,
                    previous_score=previous_score,
                    threshold=threshold,
                    risk_count=risk_count,
                    dry_run=dry_run,
                )
            except (AuthError, ConfigError):
                raise
            except Exception:  # noqa: BLE001 -- publication failures must remain fatal
                raise _AlertPublicationError() from None
    else:
        log.info("Account score is at or above its configured threshold")
        _alert_written = False

    # ---- Step 5: return updated state entry ---------------------------
    # historic regression: include alerted flag so _check_all_accounts counts only real alerts.
    return {
        "health_score": current_score,
        "peopleai_account_id": account_id,
        "opportunities_count": len(opportunities),
        "threshold": threshold,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_alerted": _alert_written,
    }


# ---------------------------------------------------------------------------
# Run summary log
# ---------------------------------------------------------------------------


def log_run_summary(
    *,
    accounts_checked: int,
    alerts_generated: int,
    api_failures: int,
    dry_run: bool,
    elapsed_seconds: float,
) -> None:
    """Emit a structured run summary to stderr."""
    log.info(
        "Run summary: accounts_checked=%d alerts_generated=%d api_failures=%d dry_run=%s elapsed=%.1fs",
        accounts_checked,
        alerts_generated,
        api_failures,
        dry_run,
        elapsed_seconds,
    )


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------


def _load_and_filter_accounts(account: str | None) -> dict[str, Any] | None:
    """Load accounts config and optionally filter to a single account.

    Returns the accounts dict, or None on fatal error.
    """
    try:
        config = get_accounts_config(strict=True)
    except (AuthError, ConfigError):
        raise
    except RuntimeError:
        return None
    accounts: dict[str, Any] = config.get("accounts", {})
    if not isinstance(accounts, dict) or not accounts:
        raise ConfigError("No accounts are configured for the Backstory watcher")
    if account:
        if account not in accounts:
            raise ConfigError("Requested Backstory account is not configured")
        return {account: accounts[account]}
    return accounts


def _open_mcp_session(endpoint: str) -> MCPSession | None:
    """Initialize and return an MCP session, or None on failure."""
    session = MCPSession(endpoint)
    try:
        session.initialize()
        return session
    except (AuthError, ConfigError):
        session.close()
        raise
    except RuntimeError:
        session.close()
        log.error("The configured Backstory MCP endpoint is unavailable; retry later")
        return None


def _check_all_accounts(
    *,
    accounts: dict[str, Any],
    session: MCPSession,
    state: dict[str, Any],
    threshold: int,
    dry_run: bool,
) -> _AccountScanResult:
    """Iterate over accounts and run health checks.

    Keep attempted lookups distinct from successfully processed accounts.
    """
    accounts_checked = 0
    accounts_attempted = 0
    publication_failures = 0
    alerts_generated = 0
    api_failures = 0
    updated_state: dict[str, Any] = dict(state)

    for account_key, account_cfg in accounts.items():
        if not isinstance(account_cfg, dict):
            log.warning("Skipping an account with invalid watcher configuration")
            continue
        # historic regression: skip internal accounts — they have no Backstory presence
        # Note: truthy check (not == True) per watch_subtleties memory
        if account_cfg.get("internal"):
            log.debug("Skipping an internal account")
            continue

        accounts_attempted += 1
        try:
            result = check_account(
                session=session,
                account_key=account_key,
                account_cfg=account_cfg,
                state=state,
                default_threshold=threshold,
                dry_run=dry_run,
            )
        except (AuthError, ConfigError):
            raise
        except _AlertPublicationError as exc:
            log.warning("Required Backstory alert publication failed")
            publication_failures += 1
            api_failures += exc.failures - 1
            continue
        except Exception:  # noqa: BLE001 -- one account failure yields a partial watcher run
            log.warning("Unexpected Backstory account check failure")
            api_failures += 1
            continue

        if result is None:
            api_failures += 1
        else:
            accounts_checked += 1
            # historic regression: use _alerted flag (set by check_account) to count only
            # alerts that were actually written — not suppressed or dry-run alerts.
            if result.get("_alerted"):
                alerts_generated += 1
            updated_state[account_key] = {k: v for k, v in result.items() if not k.startswith("_")}

    return _AccountScanResult(
        accounts_attempted, accounts_checked, alerts_generated, api_failures, publication_failures, updated_state
    )


def _run_backstory_health(
    *,
    threshold: int,
    account: str | None,
    dry_run: bool,
    as_json: bool = False,
) -> WatcherRunResult:
    """Core logic; returns this invocation's completion and persistence facts.

    ``as_json`` emits the run-status document on stdout in place of the
    ``[DRY-RUN]`` summary line; the exit code is unaffected.
    """
    import time

    start = time.monotonic()

    def fail_before_scan() -> WatcherRunResult:
        elapsed = time.monotonic() - start
        status_write = write_run_status(
            watcher="backstory-health",
            outcome="fatal",
            records_checked=0,
            alerts_generated=0,
            failures=1,
            elapsed_seconds=elapsed,
            dry_run=False,
        )
        if as_json:
            print(
                json.dumps(
                    {
                        "watcher": "backstory-health",
                        "outcome": "fatal",
                        "records_checked": 0,
                        "alerts_generated": 0,
                        "failures": 1 + int(status_write == "failed"),
                        "elapsed_seconds": round(elapsed, 1),
                        "dry_run": False,
                    },
                    indent=2,
                )
            )
        return WatcherRunResult("fatal", False, status_write)

    with watcher_logging("backstory-health", enabled=not dry_run):
        if dry_run:
            if as_json:
                print(
                    json.dumps(
                        {
                            "watcher": "backstory-health",
                            "outcome": "ok",
                            "records_checked": 0,
                            "alerts_generated": 0,
                            "failures": 0,
                            "elapsed_seconds": 0.0,
                            "dry_run": True,
                            "provider_accessed": False,
                        },
                        indent=2,
                    )
                )
            else:
                print("[DRY-RUN] backstory-health: provider input was not requested; no files were written")
            return WatcherRunResult("ok", True, None)

        accounts = _load_and_filter_accounts(account)
        if accounts is None:
            return fail_before_scan()

        endpoint = get_mcp_endpoint("backstory")
        if endpoint is None:
            raise ConfigError("Config key 'mcp_endpoints.backstory' is required for the Backstory watcher")

        state = load_state()

        session = _open_mcp_session(endpoint)
        if session is None:
            return fail_before_scan()

        try:
            scan = _check_all_accounts(
                accounts=accounts,
                session=session,
                state=state,
                threshold=threshold,
                dry_run=dry_run,
            )
        finally:
            session.close()

        accounts_checked = scan.accounts_checked
        alerts_generated = scan.alerts_generated
        api_failures = scan.api_failures
        updated_state = scan.updated_state

        state_write_failed = _state_write_failed(
            dry_run=dry_run or updated_state == state,
            write=lambda: save_state(updated_state, previous_state=state),
            logger=log,
            message="State file write failed — current scores not persisted.",
        )

        elapsed = time.monotonic() - start
        failures = api_failures + scan.publication_failures + int(state_write_failed)
        outcome: WatcherOutcome = (
            "fatal"
            if state_write_failed
            or scan.publication_failures
            or (scan.accounts_attempted > 0 and accounts_checked == 0)
            else ("partial" if api_failures > 0 else "ok")
        )
        log_run_summary(
            accounts_checked=accounts_checked,
            alerts_generated=alerts_generated,
            api_failures=api_failures,
            dry_run=dry_run,
            elapsed_seconds=elapsed,
        )
        status_write = write_run_status(
            watcher="backstory-health",
            outcome=outcome,
            records_checked=accounts_checked,
            alerts_generated=alerts_generated,
            failures=failures,
            elapsed_seconds=elapsed,
            dry_run=dry_run,
        )
        if status_write == "failed":
            outcome = "fatal"
            failures += 1
        if as_json:
            # The run happened — emit the outcome even when it is partial/fatal,
            # which is exactly when a caller needs the detail. historic regression: `outcome`
            # is reported verbatim; only "fatal" denotes a failed run.
            print(
                json.dumps(
                    {
                        "watcher": "backstory-health",
                        "outcome": outcome,
                        "records_checked": accounts_checked,
                        "alerts_generated": alerts_generated,
                        "failures": failures,
                        "elapsed_seconds": round(elapsed, 1),
                        "dry_run": dry_run,
                    },
                    indent=2,
                    default=str,
                )
            )
            return WatcherRunResult(outcome, True, status_write)
        # historic regression: print dry-run summary to stdout so --dry-run is useful as a preview.
        if dry_run:
            print(
                f"[DRY-RUN] backstory-health: {accounts_checked} account(s) scanned, "
                f"{alerts_generated} alert(s) would fire"
            )
        return WatcherRunResult(outcome, True, status_write)


__all__ = [
    "_DEFAULT_THRESHOLD",
    "_MCP_TIMEOUT",
    "_alerts_file",
    "_check_all_accounts",
    "_load_and_filter_accounts",
    "_open_mcp_session",
    "_run_backstory_health",
    "_state_file",
    "account_threshold",
    "append_alert",
    "append_api_error",
    "check_account",
    "compute_health_score",
    "get_risk_count",
    "get_watchers_dir",
    "load_state",
    "log_run_summary",
    "save_state",
]
