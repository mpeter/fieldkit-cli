#!/usr/bin/env python3
"""Slack thread age watcher — domain module.

Moved from ``commands/watch/slack_threads.py`` (watch-domain-migration,
implementation change slice 2.10). No Click imports — pure business logic.


Searches Slack for account-related messages and identifies threads where the
most recent activity is >48 hours ago and was NOT sent by the current user
(i.e., threads that may be awaiting a reply).

Alerts are appended to:
  fieldkit-data/watchers/slack-thread-alerts.md

State is persisted as a JSON sidecar:
  fieldkit-data/watchers/slack-thread-state.json

Auth handling:
  If slackcli returns an auth error (non-zero exit, or error output), a named
  error entry is written to both the markdown alert and the JSON sidecar, and
  the command exits 2 so callers do not retry without user action.

Usage:
    fieldkit watch run slack-threads
    fieldkit watch run slack-threads --threshold-hours 24
    fieldkit watch run slack-threads --account globalpay
    fieldkit watch run slack-threads --limit 50
    fieldkit watch run slack-threads --dry-run

Exit codes:
    0  All selected accounts checked successfully.
    1  Provider or persistence failure; retry may help.
    2  Slack authentication requires user action.
    3  Invalid or missing configuration/account selection.
"""

import datetime
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml

from fieldkit.config import (
    TIMEOUT_MCP_TOOL,
    TIMEOUT_PROCESS_KILL_GRACE,
    get_accounts_config,
    get_fieldkit_home,
    get_watchers_dir,
)
from fieldkit.config.retry import transient_retry
from fieldkit.errors import FieldkitError
from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process_bytes
from fieldkit.watch.dedup import alert_block_exists
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.slack_thread_classification import (
    _INTERNAL_CHANNEL_PATTERNS,
    SlackAccountThread,
    _channel_matches_account,
    classify_message,
    load_bot_patterns,
)
from fieldkit.watch.state import merge_state
from fieldkit.watch.state import state_write_failed as _state_write_failed
from fieldkit.watch.status import RunStatusWriteResult, WatcherOutcome, WatcherRunResult, write_run_status

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------


@cache
def _identity_config() -> Path:
    return get_fieldkit_home() / "config" / "identity.yaml"


@cache
def _alerts_file() -> Path:
    return get_watchers_dir() / "slack-thread-alerts.md"


@cache
def _state_file() -> Path:
    return get_watchers_dir() / "slack-thread-state.json"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLD_HOURS = 48
_DEFAULT_SEARCH_LIMIT = 50  # messages per account query
_SLACKCLI = "slackcli"
_MAX_SLACKCLI_STREAM_BYTES = 8 * 1024 * 1024

# Auth-error signals in slackcli output (exit code 1 and these substrings)
_AUTH_ERROR_SIGNALS = (
    "not authenticated",
    "auth_required",
    "invalid_auth",
    "token_revoked",
    "missing_scope",
    "account_inactive",
    "no_permission",
    "not_authed",
    "token expired",
    "login required",
)
_AUTH_ERROR_PATTERN = re.compile(
    r"(?<![a-z0-9_])(?:" + "|".join(re.escape(signal) for signal in _AUTH_ERROR_SIGNALS) + r")(?![a-z0-9_])",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

SlackFailureKind = Literal["auth", "provider"]


@dataclass(frozen=True)
class SlackRunOutcome:
    """Complete watcher result for rendering and process-status mapping."""

    run: WatcherRunResult
    records_checked: int
    alerts_generated: int
    failures: int
    auth_error: bool
    provider_error: bool
    elapsed_seconds: float
    dry_run: bool


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_current_username() -> str | None:
    """Derive the Slack username for the current user from identity.yaml or env.

    Returns None if it cannot be determined — callers fall back to a
    ``from:me`` Slack search operator rather than username matching.
    """
    # Prefer env override (useful for testing)
    import os

    env_val = os.environ.get("SLACK_USERNAME")
    if env_val:
        return env_val.strip().lower()

    if not _identity_config().exists():
        return None
    try:
        with _identity_config().open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None

    # identity.yaml doesn't have a slack_username field by default, but callers
    # can add one. Fall back to the system username as a heuristic.
    slack_username: str | None = None
    identity = data.get("identity", {})
    if isinstance(identity, dict):
        slack_username = identity.get("slack_username") or identity.get("username")

    if slack_username:
        return str(slack_username).strip().lower()

    # System fallback: strip domain from email if present
    import getpass

    return getpass.getuser().lower()


# ---------------------------------------------------------------------------
# slackcli interaction
# ---------------------------------------------------------------------------


class SlackAuthError(FieldkitError):
    """Raised when slackcli returns an authentication / permission error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _is_auth_error(returncode: int, output: str) -> bool:
    """Return True if the slackcli output indicates an auth/token failure."""
    if returncode == 0:
        return False
    return _AUTH_ERROR_PATTERN.search(output) is not None


def _is_slackcli_hang(exc: BaseException) -> bool:
    """True only for a slackcli invocation that hung and was killed.

    FileNotFoundError (binary missing) and a non-zero exit are permanent — the
    former is an environment problem and the latter is an answer, not a failure to
    get one. Neither is retried.
    """
    return isinstance(exc, subprocess.TimeoutExpired)


@transient_retry(_is_slackcli_hang, log)
def _run_slack_search_once(cmd: list[str]) -> tuple[int, bytes, bytes]:
    """Run one slackcli **search** and return (returncode, stdout, stderr).

    Named for search deliberately. Retrying a subprocess timeout is only safe
    because the sole caller, ``run_slack_search()``, reads: a killed search may have
    queried Slack but changed nothing, so repeating it is harmless. That is NOT true
    of a slackcli invocation that posts a message or updates a thread — a timeout
    there could have delivered before being killed, and a retry would double-post.

    Do not reuse this helper for a write. Give a write path its own function with
    ``connect_retry``-equivalent semantics, or no retry at all.

    Module-level so the decorator does not bind to self (historic regression).
    """
    try:
        result = run_bounded_process_bytes(
            cmd,
            timeout=TIMEOUT_MCP_TOOL,
            stdout_limit=_MAX_SLACKCLI_STREAM_BYTES,
            stderr_limit=_MAX_SLACKCLI_STREAM_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError as exc:
        if exc.reason == "timeout":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=TIMEOUT_MCP_TOOL) from None
        if exc.reason == "start":
            raise FileNotFoundError("slackcli") from None
        if exc.reason == "pipes":
            raise RuntimeError("slackcli output pipes were not available") from None
        if exc.reason == "overflow":
            raise RuntimeError("slackcli output exceeded the bounded capture limit") from None
        raise RuntimeError("slackcli process cleanup failed") from None
    return result.returncode, result.stdout, result.stderr


def run_slack_search(
    query: str,
    *,
    limit: int,
    sort: str = "timestamp",
    sort_dir: str = "desc",
) -> dict[str, Any]:
    """Run ``slackcli search messages <query> --json`` and return parsed JSON.

    Both subprocess pipes are drained concurrently into bounded memory. This
    handles responses larger than an OS pipe buffer without creating files.

    Raises:
        SlackAuthError: if slackcli exits non-zero with an auth-related signal.
        RuntimeError: for unexpected non-zero exits that are not auth errors,
            or when the output cannot be parsed as JSON.
    """
    cmd = [
        _SLACKCLI,
        "search",
        "messages",
        query,
        "--json",
        "--limit",
        str(limit),
        "--sort",
        sort,
        "--sort-dir",
        sort_dir,
    ]
    log.debug("Running bounded Slack message search: limit=%d sort=%s direction=%s", limit, sort, sort_dir)

    try:
        returncode, raw_stdout, stderr_bytes = _run_slack_search_once(cmd)
    except FileNotFoundError as exc:
        raise RuntimeError("slackcli not found — install with: brew install shaharia-lab/tap/slackcli") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"slackcli timed out after {TIMEOUT_MCP_TOOL:g}s") from exc

    stderr_text: str = stderr_bytes.decode("utf-8", errors="replace")

    if returncode != 0:
        # Auth errors are signalled by non-zero exit + recognisable message text
        combined = raw_stdout.decode("utf-8", errors="replace") + stderr_text
        if _is_auth_error(returncode, combined):
            raise SlackAuthError(f"slackcli authentication failed (exit {returncode})")
        raise RuntimeError(f"slackcli search failed (exit {returncode})")

    # stdout should be pure JSON when --json is passed; stderr has the progress line
    stdout_text = raw_stdout.decode("utf-8", errors="replace").strip()
    if not stdout_text:
        # slackcli 0.13.0 search/messages returns without JSON for zero matches.
        # Only the successful process path admits this documented empty response.
        return {"query": query, "total": 0, "matches": []}

    try:
        result: dict[str, Any] = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        # Only treat as auth error if the process actually failed (returncode != 0).
        # A parse failure on exit code 0 means malformed output — surface as RuntimeError.
        raise RuntimeError(f"slackcli returned invalid JSON (exit {returncode})") from exc
    if (
        not isinstance(result, dict)
        or "error" in result
        or type(result.get("total")) is not int
        or result["total"] < 0
        or not isinstance(result.get("matches"), list)
        or any(not isinstance(message, dict) for message in result["matches"])
    ):
        raise RuntimeError("slackcli returned an invalid search response")
    return result


def scan_account_threads(
    account_key: str,
    account_cfg: dict[str, Any],
    *,
    current_username: str | None,
    threshold_hours: int,
    search_limit: int,
    now_utc: datetime.datetime,
    bot_patterns: frozenset[str] | None = None,
) -> list[SlackAccountThread]:
    """Search Slack for an account and return unanswered-thread results.

    Uses the ``keywords`` list from account config for the search query,
    falling back to the account key name if no keywords are configured.

    historic regression: If ``account_channels`` is configured in the account config,
    only threads whose channel name matches one of those channel keywords are
    attributed to this account. This prevents misattribution when a body
    keyword appears in an unrelated channel.

    Raises SlackAuthError on Slack auth failures (caller handles gracefully).
    """
    keywords: list[str] = account_cfg.get("keywords") or []
    # Use the first keyword as the primary search term — it's the most canonical
    search_query = keywords[0] if keywords else account_key.replace("-", " ")

    # historic regression: channel names to use for attribution filtering (optional)
    account_channels: list[str] = account_cfg.get("account_channels") or []

    log.info(
        "Searching Slack: query_chars=%d limit=%d",
        len(search_query),
        search_limit,
    )

    result = run_slack_search(
        search_query,
        limit=search_limit,
        sort="timestamp",
        sort_dir="desc",
    )

    matches: list[dict[str, Any]] = result["matches"]
    total_found: int = result["total"]
    log.info(
        "Slack search: total=%d inspecting=%d",
        total_found,
        len(matches),
    )

    # historic regression: when account_channels is empty, fall back to the account key itself
    # (hyphens replaced with spaces) so attribution filtering is always active.
    # This prevents keyword hits in unrelated channels from being attributed to
    # this account when no explicit channel list is configured.
    fallback_channels: list[str] = account_channels or [account_key.replace("-", " ")]

    stale_threads: list[SlackAccountThread] = []
    for msg in matches:
        # historic regression: filter by channel name — use configured channels or the fallback.
        # Skip threads where the channel name doesn't match — they belong to another account.
        channel: dict[str, Any] = msg.get("channel") or {}
        ch_name = str(channel.get("name") or channel.get("id") or "").lower()

        # implementation change: skip messages in internal team channels — they are never
        # customer-facing and should not surface in the morning brief.
        if any(pattern in ch_name for pattern in _INTERNAL_CHANNEL_PATTERNS):
            log.debug(
                "Skipping thread in an internal channel",
            )
            continue

        if not _channel_matches_account(ch_name, fallback_channels):
            log.debug(
                "Skipping thread without an account channel match",
            )
            continue

        classified = classify_message(
            msg,
            current_username=current_username,
            threshold_hours=threshold_hours,
            now_utc=now_utc,
            extra_bot_patterns=bot_patterns,
        )
        if classified is not None:
            account_thread: SlackAccountThread = {**classified, "account": account_key}
            stale_threads.append(account_thread)

    log.info(
        "Slack account scan: unanswered=%d threshold_hours=%d",
        len(stale_threads),
        threshold_hours,
    )
    return stale_threads


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    """Load persisted thread state; return {} if missing or unreadable."""
    if not _state_file().exists():
        return {}
    try:
        with _state_file().open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        log.warning("Could not read Slack watcher state")
        return {}


def save_state(state: dict[str, Any], *, previous_state: dict[str, Any] | None = None) -> None:
    """Persist thread-state changes without overwriting concurrent updates."""
    merge_state(_state_file(), state, previous_state or {})


# ---------------------------------------------------------------------------
# Alert writer
# ---------------------------------------------------------------------------


def _ensure_alerts_header() -> None:
    """Create the alerts file with a header if it doesn't exist."""
    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Slack Thread Age Alerts\n\nAutomated alerts written by watch_slack_threads.py.\n", encoding="utf-8"
        )


def write_slack_error_alert(*, dry_run: bool, failure_kind: SlackFailureKind) -> bool:
    """Write a named, sanitized Slack failure entry to the alerts file.

    The entry is structured so that a reader can grep for 'auth expired' and
    take the corrective action (``slackcli auth login``).
    """
    now_utc = datetime.datetime.now(datetime.UTC)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now_utc.strftime("%Y-%m-%d")

    is_auth = failure_kind == "auth"
    detail = (
        "Slack authentication failed; run slackcli auth login."
        if is_auth
        else "Slack search failed; inspect the watcher log and retry."
    )
    auth_error_heading = f"{date_label} — Slack {'auth expired' if is_auth else 'search failed'}"
    lines = [
        "",
        f"## {auth_error_heading}",
        "",
        f"- **Status:** `{'auth-error' if is_auth else 'provider-error'}`",
        (
            "- **Action required:** Run `slackcli auth login` or `slackcli auth login-browser`"
            if is_auth
            else "- **Action required:** Inspect the watcher log and retry the Slack search"
        ),
        f"- **Detected at:** {ts}",
        f"- **Detail:** {detail}",
        "",
    ]
    alert_text = "\n".join(lines)

    if dry_run:
        log.info("[DRY RUN] Would write one Slack authentication alert")
        return False

    # historic regression: skip if this exact auth-error heading was already written today.
    if alert_block_exists(_alerts_file(), auth_error_heading):
        log.info("Auth error alert already present for today — skipping duplicate write")
        return False

    _ensure_alerts_header()
    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.warning("Slack authentication alert written")
    return True


def append_thread_alert(thread: SlackAccountThread, *, dry_run: bool) -> bool:
    """Append a stale-thread alert entry to slack-thread-alerts.md."""
    now_utc = datetime.datetime.now(datetime.UTC)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now_utc.strftime("%Y-%m-%d")

    account = thread["account"]
    channel = thread["channel"]
    age_hours = thread["age_hours"]
    sender = thread["sender_username"]
    permalink = thread["permalink"]
    text_snippet = thread.get("text_snippet") or ""
    msg_dt = thread["message_dt"]

    # historic regression: heading prefix used for dedup check and as the heading text.
    # Format must match what alert_block_exists() scans for (prefix of the ## line).
    heading_prefix = f"{date_label} — {account} / #{channel}"
    lines = [
        "",
        f"## {heading_prefix} — {age_hours:.0f}h old",
        "",
        f"- **Account:** `{account}`",
        f"- **Channel:** `#{channel}`",
        f"- **Last sender:** `{sender}`",
        f"- **Age:** {age_hours:.0f} hours",
        f"- **Message sent at:** {msg_dt}",
        f"- **Thread URL:** {permalink}" if permalink else "- **Thread URL:** (unavailable)",
    ]
    if text_snippet:
        lines.append(f"- **Preview:** {text_snippet}")
    lines += [
        f"- **Detected at:** {ts}",
        "",
    ]
    alert_text = "\n".join(lines)

    if dry_run:
        log.info("[DRY RUN] Would append one stale-thread alert")
        return False

    # historic regression: skip if an alert for this account/channel was already written today.
    if alert_block_exists(_alerts_file(), heading_prefix):
        log.info("Thread alert already present for today — skipping duplicate write")
        return False

    _ensure_alerts_header()
    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.info("Thread alert written: age_hours=%.0f", age_hours)
    return True


def append_run_summary(
    *,
    run_ts: str,
    checked_accounts: int,
    total_alerts: int,
    failure_kind: SlackFailureKind | None,
    dry_run: bool,
) -> None:
    """Append a run-summary HTML comment to the alerts file."""
    status = f"{failure_kind}-error" if failure_kind is not None else ("ok" if total_alerts == 0 else "alerts")
    summary = (
        f"\n<!-- run: {run_ts} | accounts={checked_accounts}"
        f" | alerts={total_alerts} | status={status} | dry_run={dry_run} -->\n"
    )
    if dry_run:
        log.info("[DRY RUN] Run summary: %s", summary.strip())
        return
    _ensure_alerts_header()
    alerts_path = _alerts_file()
    # historic regression: deduplicate — skip if this exact comment was already written
    # (can happen when _append_run_summary is called twice in the same execution).
    existing = alerts_path.read_text(encoding="utf-8") if alerts_path.exists() else ""
    if summary.strip() in existing:
        log.debug("Run summary already present — skipping duplicate write")
        return
    with alerts_path.open("a", encoding="utf-8") as fh:
        fh.write(summary)


def _save_auth_error_state(
    updated_state: dict[str, Any],
    previous_state: dict[str, Any],
    message: str,
    run_ts: str,
    *,
    dry_run: bool,
) -> bool:
    """Record an auth error and report whether persisting it failed."""
    if dry_run:
        return False
    updated_state["__auth_error__"] = {
        "error": "auth_expired",
        "message": message[:300],
        "detected_at": run_ts,
    }
    return _state_write_failed(
        dry_run=False,
        write=lambda: save_state(updated_state, previous_state=previous_state),
        logger=log,
        message="State file write failed after auth error.",
    )


def _build_account_state_entry(
    account_key: str,
    run_ts: str,
    threshold_hours: int,
    limit: int,
    stale_threads: list[SlackAccountThread],
) -> dict[str, Any]:
    """Build the state dict for one account after scanning."""
    return {
        "account": account_key,
        "checked_at": run_ts,
        "threshold_hours": threshold_hours,
        "messages_inspected": limit,
        "unanswered_threads": len(stale_threads),
        "threads": [
            {
                "channel": t["channel"],
                "ts": t["ts"],
                "message_dt": t["message_dt"],
                "age_hours": t["age_hours"],
                "sender": t["sender_username"],
                "permalink": t["permalink"],
            }
            for t in stale_threads
        ],
    }


def _scan_all_accounts(
    accounts: dict[str, Any],
    *,
    account_filter: str | None,
    current_username: str | None,
    threshold_hours: int,
    limit: int,
    limit_per_account: int | None = None,
    now_utc: datetime.datetime,
    run_ts: str,
    updated_state: dict[str, Any],
    previous_state: dict[str, Any],
    dry_run: bool,
    config: dict[str, Any] | None = None,
) -> tuple[int, int, SlackFailureKind | None, int]:
    """Scan all (or a single filtered) account for stale Slack threads.

    Returns checked accounts, published alerts, interruption kind, and persistence failures.
    Mutates *updated_state* in-place with per-account results.
    """
    checked_accounts = 0
    total_alerts = 0
    failure_kind: SlackFailureKind | None = None
    persistence_failures = 0

    # historic regression: load merged bot patterns from config once for all accounts
    bot_patterns = load_bot_patterns(config or {})

    for account_key, account_cfg in accounts.items():
        if account_filter and account_key != account_filter:
            continue
        if not isinstance(account_cfg, dict):
            log.warning("Skipping an account whose configuration is not a mapping")
            continue
        if account_cfg.get("slack_watch") is False or account_cfg.get("internal"):
            log.info("Skipping an account excluded from Slack watching")
            continue

        try:
            # implementation note: apply per-account limit when set; the effective limit is
            # the smaller of the global limit and the per-account cap.
            effective_limit = min(limit, limit_per_account) if limit_per_account is not None else limit
            stale_threads = scan_account_threads(
                account_key,
                account_cfg,
                current_username=current_username,
                threshold_hours=threshold_hours,
                search_limit=effective_limit,
                now_utc=now_utc,
                bot_patterns=bot_patterns,
            )
        except SlackAuthError:
            log.error("Slack authentication failed")
            failure_kind = "auth"
            safe_message = "Slack authentication failed; run slackcli auth login."
            published = False

            def publish_auth_alert() -> None:
                nonlocal published
                published = write_slack_error_alert(dry_run=False, failure_kind="auth")

            persistence_failures += int(
                _state_write_failed(
                    dry_run=dry_run,
                    write=publish_auth_alert,
                    logger=log,
                    message="Slack authentication alert write failed.",
                )
            )
            total_alerts += int(published)
            auth_state_failed = _save_auth_error_state(
                updated_state, previous_state, safe_message, run_ts, dry_run=dry_run
            )
            persistence_failures += int(auth_state_failed)
            break
        except RuntimeError:
            # historic regression: non-auth slackcli failures (e.g. binary not found, timeout,
            # malformed JSON) must also surface an alert so the morning brief
            # reflects the failure — not silently skip the account.
            log.error("Slack search failed")
            failure_kind = "provider"
            published = False

            def publish_provider_alert() -> None:
                nonlocal published
                published = write_slack_error_alert(dry_run=False, failure_kind="provider")

            persistence_failures += int(
                _state_write_failed(
                    dry_run=dry_run,
                    write=publish_provider_alert,
                    logger=log,
                    message="Slack provider alert write failed.",
                )
            )
            total_alerts += int(published)
            break

        checked_accounts += 1
        for thread in stale_threads:
            published = False

            def publish_thread_alert(thread: SlackAccountThread = thread) -> None:
                nonlocal published
                published = append_thread_alert(thread, dry_run=False)

            alert_failed = _state_write_failed(
                dry_run=dry_run,
                write=publish_thread_alert,
                logger=log,
                message="Slack thread alert write failed.",
            )
            persistence_failures += int(alert_failed)
            total_alerts += int(published)

        # implementation note: record effective_limit (not global limit) so state reflects
        # the actual per-account search budget used.
        updated_state[account_key] = _build_account_state_entry(
            account_key, run_ts, threshold_hours, effective_limit, stale_threads
        )

    return checked_accounts, total_alerts, failure_kind, persistence_failures


def _persist_and_summarise(
    *,
    updated_state: dict[str, Any],
    previous_state: dict[str, Any],
    failure_kind: SlackFailureKind | None,
    dry_run: bool,
    run_ts: str,
    checked_accounts: int,
    total_alerts: int,
    elapsed: float,
) -> int:
    """Persist state and return whether storage failed before appending its summary."""
    state_write_failed = _state_write_failed(
        dry_run=dry_run or failure_kind is not None,
        write=lambda: save_state(updated_state, previous_state=previous_state),
        logger=log,
        message="State file write failed — thread state not persisted.",
    )

    summary_write_failed = _state_write_failed(
        dry_run=dry_run,
        write=lambda: append_run_summary(
            run_ts=run_ts,
            checked_accounts=checked_accounts,
            total_alerts=total_alerts,
            failure_kind=failure_kind,
            dry_run=False,
        ),
        logger=log,
        message="Slack run summary write failed.",
    )

    log.info(
        "Run complete: accounts=%d alerts=%d failure_kind=%s elapsed=%.1fs dry_run=%s",
        checked_accounts,
        total_alerts,
        failure_kind or "none",
        elapsed,
        dry_run,
    )
    return int(state_write_failed) + int(summary_write_failed)


def _slack_outcome(
    *, failure_kind: SlackFailureKind | None, checked_accounts: int, state_write_failed: bool
) -> WatcherOutcome:
    """Classify Slack outcomes while keeping persistence failures fail-closed."""
    if state_write_failed or (failure_kind is not None and checked_accounts == 0):
        return "fatal"
    if failure_kind is not None:
        return "partial"
    return "ok" if checked_accounts > 0 else "fatal"


def _selected_slack_accounts(accounts: dict[str, Any], account_filter: str | None) -> dict[str, dict[str, Any]]:
    """Validate selected account config and return accounts eligible for scanning."""
    selected = {account_filter: accounts[account_filter]} if account_filter else accounts
    eligible: dict[str, dict[str, Any]] = {}
    for account_key, account_cfg in selected.items():
        if not isinstance(account_key, str) or not account_key.strip() or not isinstance(account_cfg, dict):
            raise ValueError("selected Slack account configuration is invalid")
        for field in ("keywords", "account_channels"):
            values = account_cfg.get(field)
            if values is not None and (
                not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values)
            ):
                raise ValueError(f"selected Slack account {field} must be a list of non-empty strings")
        for field in ("slack_watch", "internal"):
            value = account_cfg.get(field)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"selected Slack account {field} must be a boolean")
        if account_cfg.get("slack_watch") is False or account_cfg.get("internal") is True:
            continue
        eligible[account_key] = account_cfg
    if not eligible:
        raise ValueError("no selected accounts are eligible for Slack watching")
    return eligible


def _run_slack_threads(
    *,
    threshold_hours: int,
    account: str | None,
    limit: int,
    limit_per_account: int | None = None,
    dry_run: bool,
) -> SlackRunOutcome:
    """Run the watcher and return its complete domain outcome."""
    start = time.monotonic()
    if threshold_hours <= 0 or limit <= 0 or (limit_per_account is not None and limit_per_account <= 0):
        log.error("Slack watcher numeric options must be positive integers")
        return _invalid_run_outcome(start=start, dry_run=dry_run)
    try:
        config = get_accounts_config(strict=True)
    except RuntimeError:
        return _invalid_run_outcome(start=start, dry_run=dry_run)
    accounts: dict[str, Any] = config.get("accounts", {})
    if not isinstance(accounts, dict) or not accounts:
        log.error("No accounts found in accounts.yaml")
        return _invalid_run_outcome(start=start, dry_run=dry_run)

    if account and account not in accounts:
        log.error("Requested account is not configured")
        return _invalid_run_outcome(start=start, dry_run=dry_run)
    try:
        selected_accounts = _selected_slack_accounts(accounts, account)
    except ValueError as exc:
        log.error("Invalid Slack watcher account selection: %s", exc)
        return _invalid_run_outcome(start=start, dry_run=dry_run)

    now_utc = datetime.datetime.now(datetime.UTC)
    run_ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    with watcher_logging("slack-threads", enabled=not dry_run):
        # ---- Resolve current user ----------------------------------------
        current_username = load_current_username()
        log.info("Slack identity configured: %s", current_username is not None)

        # ---- Load prior state -------------------------------------------
        state = load_state()
        updated_state: dict[str, Any] = dict(state)

        # ---- Scan accounts -----------------------------------------------
        checked_accounts, total_alerts, failure_kind, scan_persistence_failures = _scan_all_accounts(
            selected_accounts,
            account_filter=None,
            current_username=current_username,
            threshold_hours=threshold_hours,
            limit=limit,
            limit_per_account=limit_per_account,
            now_utc=now_utc,
            run_ts=run_ts,
            updated_state=updated_state,
            previous_state=state,
            dry_run=dry_run,
            config=config,
        )

        # ---- Persist state + run summary ---------------------------------
        elapsed = time.monotonic() - start
        persistence_failures = scan_persistence_failures + _persist_and_summarise(
            updated_state=updated_state,
            previous_state=state,
            failure_kind=failure_kind,
            dry_run=dry_run,
            run_ts=run_ts,
            checked_accounts=checked_accounts,
            total_alerts=total_alerts,
            elapsed=elapsed,
        )

        # Preserve a detailed run outcome while returning the canonical process
        # status: auth=2, retryable provider/persistence failure=1, success=0.
        outcome = _slack_outcome(
            failure_kind=failure_kind,
            checked_accounts=checked_accounts,
            state_write_failed=persistence_failures > 0,
        )
        failures = int(failure_kind is not None) + persistence_failures
        status_result: RunStatusWriteResult | None = None
        if not dry_run:
            status_result = write_run_status(
                watcher="slack-threads",
                outcome=outcome,
                records_checked=checked_accounts,
                alerts_generated=total_alerts,
                failures=failures,
                elapsed_seconds=elapsed,
                dry_run=False,
            )
            if status_result == "failed":
                failures += 1
                outcome = "fatal"
        return SlackRunOutcome(
            run=WatcherRunResult(outcome, failure_kind is None, status_result, 2 if failure_kind == "auth" else 1),
            records_checked=checked_accounts,
            alerts_generated=total_alerts,
            failures=failures,
            auth_error=failure_kind == "auth",
            provider_error=failure_kind == "provider",
            elapsed_seconds=round(elapsed, 1),
            dry_run=dry_run,
        )


def _invalid_run_outcome(*, start: float, dry_run: bool) -> SlackRunOutcome:
    """Return a sanitized invalid-data outcome without touching runtime state."""
    return SlackRunOutcome(
        run=WatcherRunResult("fatal", False, None, 3),
        records_checked=0,
        alerts_generated=0,
        failures=1,
        auth_error=False,
        provider_error=False,
        elapsed_seconds=round(time.monotonic() - start, 1),
        dry_run=dry_run,
    )


__all__ = [
    "_AUTH_ERROR_SIGNALS",
    "_DEFAULT_SEARCH_LIMIT",
    "_DEFAULT_THRESHOLD_HOURS",
    "_SLACKCLI",
    "SlackAuthError",
    "SlackRunOutcome",
    "_alerts_file",
    "_build_account_state_entry",
    "_ensure_alerts_header",
    "_identity_config",
    "_is_auth_error",
    "_persist_and_summarise",
    "_run_slack_search_once",
    "_run_slack_threads",
    "_save_auth_error_state",
    "_scan_all_accounts",
    "_state_file",
    "append_run_summary",
    "append_thread_alert",
    "get_watchers_dir",
    "load_current_username",
    "load_state",
    "run_slack_search",
    "save_state",
    "scan_account_threads",
    "write_slack_error_alert",
]
