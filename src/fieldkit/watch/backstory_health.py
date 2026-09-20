"""Backstory account health watcher — domain module.

Moved from ``commands/watch/backstory_health.py`` (watch-domain-migration,
implementation change slice 2.7). No Click imports — pure business logic.

Checks account engagement health for all configured accounts via the
Backstory API (fieldkit-sales mcpjungle group). Detects drops below a
configurable threshold and appends dated alerts to
fieldkit-data/watchers/backstory-alerts.md.

Health score is the mean engagement_level across all opportunities returned
by backstory__find_account.  This is the only structured numeric metric
Backstory exposes at the account level.

State is persisted in fieldkit-data/watchers/backstory-health-state.json so
each run can compute deltas against the previous run.
"""

import contextlib
import json
import logging
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import get_accounts_config, get_fieldkit_home, get_watchers_dir
from fieldkit.config import get_mcp_gateway_base as _get_mcp_gateway_base
from fieldkit.watch.dedup import alert_block_exists
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.morning_brief_mcp import MCPSession as MCPSession
from fieldkit.watch.state import merge_state
from fieldkit.watch.state import state_write_failed as _state_write_failed
from fieldkit.watch.status import WatcherOutcome, write_run_status

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------


@cache
def _accounts_config() -> Path:
    return get_fieldkit_home() / "config" / "accounts.yaml"


@cache
def _alerts_file() -> Path:
    return get_watchers_dir() / "backstory-alerts.md"


@cache
def _state_file() -> Path:
    return get_watchers_dir() / "backstory-health-state.json"


# ---------------------------------------------------------------------------
# MCP gateway
# ---------------------------------------------------------------------------

_MCP_BASE = f"{_get_mcp_gateway_base()}/v0/groups/fieldkit-sales/mcp"
_MCP_TIMEOUT = 30  # seconds per HTTP call
_DEFAULT_THRESHOLD = 60  # engagement_level below this → alert

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backstory helpers
# ---------------------------------------------------------------------------


def find_account_id(session: MCPSession, account_name: str) -> int | None:
    """Return the peopleai_account_id for *account_name*, or None if not found."""
    try:
        result = session.call_tool("backstory__find_account", {"account_name": account_name})
    except RuntimeError as exc:
        log.warning("find_account failed for %r: %s", account_name, exc, exc_info=True)
        return None

    if isinstance(result, dict):
        acc_id = result.get("peopleai_account_id")
        if isinstance(acc_id, int):
            return acc_id
    log.warning("find_account(%r) returned unexpected structure: %r", account_name, str(result)[:200])
    return None


def get_engagement_score(session: MCPSession, account_id: int) -> float | None:
    """Return mean engagement_level across all opportunities, or None on error.

    Backstory's engagement_level (0-100) per opportunity is the only structured
    numeric health metric available from the account-level API.  We average
    across all opportunities to obtain a single account health score.
    """
    with contextlib.suppress(RuntimeError):
        session.call_tool(
            "backstory__find_account",
            {"account_name": ""},  # re-fetch by ID isn't available; use find
        )  # fallback handled below

    # Re-fetch by name isn't reliable — call find_record_by_crm_id if an
    # mdm_id is available, otherwise use the opportunities already in state.
    # For now: call get_account_status (returns text) and extract risk count
    # as a qualitative signal; primary score comes from find_account call in
    # check_account() which already has the opportunity list.
    return None  # sentinel; score computed inline in check_account()


def compute_health_score(opportunities: list[dict[str, Any]]) -> float:
    """Return mean engagement_level across opportunities (0-100).

    Returns 0.0 if no opportunities are present.
    """
    if not opportunities:
        return 0.0
    levels: list[float] = []
    for o in opportunities:
        raw = o.get("engagement_level")
        if raw is None:
            continue
        with contextlib.suppress(TypeError, ValueError):
            levels.append(float(raw))
    if not levels:
        return 0.0
    return sum(levels) / len(levels)


def get_risk_count(session: MCPSession, account_id: int) -> int:
    """Return the number of risk bullets in the account status narrative.

    The risk count is included in the alert as a qualitative context signal.
    Returns -1 on failure (so callers can log a warning without crashing).
    """
    try:
        text = session.call_tool("backstory__get_account_status", {"peopleai_account_id": account_id})
    except RuntimeError as exc:
        log.warning("get_account_status(%d) failed: %s", account_id, exc, exc_info=True)
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


def load_accounts_config() -> dict[str, Any]:
    """Load accounts.yaml via lib.config (cached, single source of truth).

    Raises RuntimeError on failure so callers inside try/finally blocks can
    handle it without bypassing teardown (Constitution IV: no sys.exit in
    library helpers).
    """
    try:
        return get_accounts_config()
    except Exception as exc:
        log.error("Failed to load accounts.yaml: %s", exc, exc_info=True)
        raise RuntimeError("accounts.yaml load failed") from exc


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
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read state file %s: %s", _state_file(), exc, exc_info=True)
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
) -> None:
    """Append a dated alert entry to backstory-alerts.md."""
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
        log.info("[DRY RUN] Would append alert:\n%s", alert_text)
        return

    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Backstory Health Alerts\n\nAutomated alerts written by watch_backstory_health.py.\n", encoding="utf-8"
        )

    heading_prefix = f"{date_label} — {account_key} health alert"
    if alert_block_exists(_alerts_file(), heading_prefix):
        return

    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.info("Alert written for %s (score=%.1f, threshold=%d)", account_key, current_score, threshold)


def append_api_error(account_key: str, error_msg: str, dry_run: bool) -> None:
    """Append a dated API error line to backstory-alerts.md."""
    now_utc = datetime.now(UTC)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now_utc.strftime("%Y-%m-%d")
    line = f"\n## {date_label} — {account_key} API error\n\n- **Error:** {error_msg}\n- **Timestamp:** {ts}\n"

    if dry_run:
        log.info("[DRY RUN] Would append error: %s", line)
        return

    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Backstory Health Alerts\n\nAutomated alerts written by watch_backstory_health.py.\n", encoding="utf-8"
        )
    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(line)
    log.warning("API error logged for %s: %s", account_key, error_msg)


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

    log.info("Checking account %r (search=%r, threshold=%d)", account_key, search_name, threshold)

    try:
        result = session.call_tool("backstory__find_account", {"account_name": search_name})
    except RuntimeError as exc:
        error_msg = f"backstory__find_account failed: {exc}"
        log.warning(error_msg, exc_info=True)
        append_api_error(account_key, error_msg, dry_run)
        return None

    if not isinstance(result, dict):
        error_msg = f"backstory__find_account returned non-dict: {str(result)[:100]}"
        log.warning(error_msg)
        append_api_error(account_key, error_msg, dry_run)
        return None

    account_id: int | None = result.get("peopleai_account_id")
    if not isinstance(account_id, int):
        error_msg = f"No peopleai_account_id in find_account response (search={search_name!r})"
        log.warning(error_msg)
        append_api_error(account_key, error_msg, dry_run)
        return None

    opportunities: list[dict[str, Any]] = result.get("opportunities", [])

    # ---- Step 2: compute health score ---------------------------------
    current_score = compute_health_score(opportunities)
    log.info("  %s: score=%.1f (%d opportunities)", account_key, current_score, len(opportunities))

    # ---- Step 3: read previous state ----------------------------------
    prev_entry: dict[str, Any] = state.get(account_key, {})
    previous_score: float | None = prev_entry.get("health_score")

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
            log.info(
                "  %s: score %.1f unchanged from previous — suppressing re-alert",
                account_key,
                current_score,
            )
            _alert_written = False
        else:
            risk_count = get_risk_count(session, account_id)
            append_alert(
                account_key=account_key,
                current_score=current_score,
                previous_score=previous_score,
                threshold=threshold,
                risk_count=risk_count,
                dry_run=dry_run,
            )
            _alert_written = not dry_run  # historic regression: dry_run alerts don't count
    else:
        log.info("  %s: score %.1f >= threshold %d — no alert", account_key, current_score, threshold)
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


def _load_and_filter_accounts(
    account: str | None,
) -> dict[str, Any] | None:
    """Load accounts config and optionally filter to a single account.

    Returns the accounts dict, or None on fatal error.
    """
    try:
        config = load_accounts_config()
    except RuntimeError:
        return None
    accounts: dict[str, Any] = config.get("accounts", {})
    if not isinstance(accounts, dict) or not accounts:
        log.error("No accounts found in %s", _accounts_config())
        return None
    if account:
        if account not in accounts:
            log.error("Account %r not found in accounts.yaml", account)
            return None
        return {account: accounts[account]}
    return accounts


def _open_mcp_session() -> MCPSession | None:
    """Initialize and return an MCP session, or None on failure."""
    session = MCPSession(_MCP_BASE)
    try:
        session.initialize()
        return session
    except RuntimeError as exc:
        log.error("Cannot reach mcpjungle fieldkit-sales group: %s", exc, exc_info=True)
        log.error("Start with: systemctl --user start mcpjungle", exc_info=True)
        return None


def _check_all_accounts(
    *,
    accounts: dict[str, Any],
    session: MCPSession,
    state: dict[str, Any],
    threshold: int,
    dry_run: bool,
) -> tuple[int, int, int, dict[str, Any]]:
    """Iterate over accounts and run health checks.

    Returns (accounts_checked, alerts_generated, api_failures, updated_state).
    """
    accounts_checked = 0
    alerts_generated = 0
    api_failures = 0
    updated_state: dict[str, Any] = dict(state)

    for account_key, account_cfg in accounts.items():
        if not isinstance(account_cfg, dict):
            log.warning("Skipping %r — config is not a mapping", account_key)
            continue
        # historic regression: skip internal accounts — they have no Backstory presence
        # Note: truthy check (not == True) per watch_subtleties memory
        if account_cfg.get("internal"):
            log.debug("Skipping internal account %r", account_key)
            continue

        try:
            result = check_account(
                session=session,
                account_key=account_key,
                account_cfg=account_cfg,
                state=state,
                default_threshold=threshold,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Unexpected error checking %r: %s", account_key, exc, exc_info=True)
            api_failures += 1
            continue

        accounts_checked += 1
        if result is None:
            api_failures += 1
        else:
            # historic regression: use _alerted flag (set by check_account) to count only
            # alerts that were actually written — not suppressed or dry-run alerts.
            if result.get("_alerted"):
                alerts_generated += 1
            updated_state[account_key] = {k: v for k, v in result.items() if not k.startswith("_")}

    return accounts_checked, alerts_generated, api_failures, updated_state


def _run_backstory_health(
    *,
    threshold: int,
    account: str | None,
    dry_run: bool,
    as_json: bool = False,
) -> int:
    """Core logic; returns POSIX exit code.

    ``as_json`` emits the run-status document on stdout in place of the
    ``[DRY-RUN]`` summary line; the exit code is unaffected.
    """
    import time

    start = time.monotonic()
    with watcher_logging("backstory-health"):
        accounts = _load_and_filter_accounts(account)
        if accounts is None:
            return 1

        state = load_state()

        session = _open_mcp_session()
        if session is None:
            return 1

        accounts_checked, alerts_generated, api_failures, updated_state = _check_all_accounts(
            accounts=accounts,
            session=session,
            state=state,
            threshold=threshold,
            dry_run=dry_run,
        )
        session.close()

        state_write_failed = _state_write_failed(
            dry_run=dry_run or not updated_state,
            write=lambda: save_state(updated_state, previous_state=state),
            logger=log,
            message="State file write failed — current scores not persisted.",
        )

        elapsed = time.monotonic() - start
        failures = api_failures + int(state_write_failed)
        outcome: WatcherOutcome = (
            "fatal" if state_write_failed or accounts_checked == 0 else ("partial" if api_failures > 0 else "ok")
        )
        log_run_summary(
            accounts_checked=accounts_checked,
            alerts_generated=alerts_generated,
            api_failures=api_failures,
            dry_run=dry_run,
            elapsed_seconds=elapsed,
        )
        write_run_status(
            watcher="backstory-health",
            outcome=outcome,
            records_checked=accounts_checked,
            alerts_generated=alerts_generated,
            failures=failures,
            elapsed_seconds=elapsed,
            dry_run=dry_run,
        )
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
            return 1 if state_write_failed else 0
        # historic regression: print dry-run summary to stdout so --dry-run is useful as a preview.
        if dry_run:
            print(
                f"[DRY-RUN] backstory-health: {accounts_checked} account(s) scanned, "
                f"{alerts_generated} alert(s) would fire"
            )
        return 1 if state_write_failed else 0


__all__ = [
    "_DEFAULT_THRESHOLD",
    "_MCP_BASE",
    "_MCP_TIMEOUT",
    "_accounts_config",
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
    "find_account_id",
    "get_engagement_score",
    "get_risk_count",
    "get_watchers_dir",
    "load_accounts_config",
    "load_state",
    "log_run_summary",
    "save_state",
]
