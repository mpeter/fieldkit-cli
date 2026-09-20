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
  the script exits 0 so launchd/cron does not mark the job as permanently
  failed and stop scheduling it.

Usage:
    fieldkit watch run slack-threads
    fieldkit watch run slack-threads --threshold-hours 24
    fieldkit watch run slack-threads --account globalpay
    fieldkit watch run slack-threads --limit 50
    fieldkit watch run slack-threads --dry-run

Exit codes:
    0  All accounts checked (or auth error handled gracefully).
    1  Fatal configuration or filesystem error (not auth).
"""

import datetime
import json
import logging
import subprocess
import tempfile
import time
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from fieldkit.config import (
    TIMEOUT_MCP_TOOL,
    get_accounts_config,
    get_config_path,
    get_fieldkit_home,
    get_watchers_dir,
)
from fieldkit.config.retry import transient_retry
from fieldkit.errors import FieldkitError
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
from fieldkit.watch.status import WatcherOutcome, write_run_status

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
    "login",  # "Workspace not found" still needs login
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loading
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


@cache
def _accounts_config() -> Path:
    """Return path to accounts.yaml config file."""
    return get_config_path("accounts.yaml")


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
    combined = output.lower()
    return any(signal in combined for signal in _AUTH_ERROR_SIGNALS)


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
    with tempfile.TemporaryFile() as stdout_tmp:
        proc = subprocess.run(
            cmd,
            stdout=stdout_tmp,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_MCP_TOOL,
            check=False,
        )
        stdout_tmp.seek(0)
        raw_stdout: bytes = stdout_tmp.read()
    return proc.returncode, raw_stdout, (proc.stderr or b"")


def run_slack_search(
    query: str,
    *,
    limit: int,
    sort: str = "timestamp",
    sort_dir: str = "desc",
) -> dict[str, Any]:
    """Run ``slackcli search messages <query> --json`` and return parsed JSON.

    slackcli can return large JSON payloads (>64 KB) for accounts with many
    messages. Python's subprocess pipe buffer is 65536 bytes on Linux, which
    causes truncation when ``capture_output=True``. We work around this by
    redirecting stdout to a temporary file and reading it back after the
    process exits.

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
    log.debug("Running: %s", " ".join(cmd))

    try:
        # Use a temp file for stdout to avoid the 64 KB pipe-buffer truncation
        # that occurs with capture_output=True on large JSON responses.
        returncode, raw_stdout, stderr_bytes = _run_slack_search_once(cmd)
    except FileNotFoundError as exc:
        raise RuntimeError("slackcli not found — install with: brew install shaharia-lab/tap/slackcli") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("slackcli timed out after 60s") from exc

    stderr_text: str = stderr_bytes.decode("utf-8", errors="replace")

    if returncode != 0:
        # Auth errors are signalled by non-zero exit + recognisable message text
        combined = raw_stdout.decode("utf-8", errors="replace") + stderr_text
        if _is_auth_error(returncode, combined):
            raise SlackAuthError(f"slackcli auth error (exit {returncode}): {combined.strip()[:200]}")
        raise RuntimeError(f"slackcli exited {returncode}: {combined.strip()[:300]}")

    # stdout should be pure JSON when --json is passed; stderr has the progress line
    stdout_text = raw_stdout.decode("utf-8", errors="replace").strip()
    if not stdout_text:
        # Empty output means no results
        return {"query": query, "total": 0, "matches": []}

    try:
        result: dict[str, Any] = json.loads(stdout_text)
        return result
    except json.JSONDecodeError as exc:
        # Only treat as auth error if the process actually failed (returncode != 0).
        # A parse failure on exit code 0 means malformed output — surface as RuntimeError.
        raise RuntimeError(f"slackcli returned non-JSON output (exit {returncode}): {stdout_text[:200]}") from exc


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
        "Searching Slack for account=%r query=%r limit=%d",
        account_key,
        search_query,
        search_limit,
    )

    result = run_slack_search(
        search_query,
        limit=search_limit,
        sort="timestamp",
        sort_dir="desc",
    )

    matches: list[dict[str, Any]] = result.get("matches") or []
    total_found: int = result.get("total", 0)
    log.info(
        "account=%r: %d total Slack messages, inspecting %d",
        account_key,
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
                "Skipping thread in internal channel %r for account %r",
                ch_name,
                account_key,
            )
            continue

        if not _channel_matches_account(ch_name, fallback_channels):
            log.debug(
                "Skipping thread in channel %r — no channel-name match for account %r",
                ch_name,
                account_key,
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
        "account=%r: found %d unanswered thread(s) older than %dh",
        account_key,
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
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read state file %s: %s", _state_file(), exc, exc_info=True)
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


def write_auth_error_alert(error_message: str, *, dry_run: bool) -> None:
    """Write a named auth-error entry to the alerts markdown file.

    The entry is structured so that a reader can grep for 'auth expired' and
    take the corrective action (``slackcli auth login``).
    """
    now_utc = datetime.datetime.now(datetime.UTC)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now_utc.strftime("%Y-%m-%d")

    auth_error_heading = f"{date_label} — Slack auth expired"
    lines = [
        "",
        f"## {auth_error_heading}",
        "",
        "- **Status:** `auth-error`",
        "- **Action required:** Run `slackcli auth login` or `slackcli auth login-browser`",
        f"- **Detected at:** {ts}",
        f"- **Detail:** {error_message[:200]}",
        "",
    ]
    alert_text = "\n".join(lines)

    if dry_run:
        log.info("[DRY RUN] Would write auth error alert:\n%s", alert_text)
        return

    # historic regression: skip if this exact auth-error heading was already written today.
    if alert_block_exists(_alerts_file(), auth_error_heading):
        log.info("Auth error alert already present for %s — skipping duplicate write", date_label)
        return

    _ensure_alerts_header()
    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.warning("Slack auth error alert written to %s", _alerts_file())


def append_thread_alert(thread: SlackAccountThread, *, dry_run: bool) -> None:
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
        log.info("[DRY RUN] Would append thread alert:\n%s", alert_text)
        return

    # historic regression: skip if an alert for this account/channel was already written today.
    if alert_block_exists(_alerts_file(), heading_prefix):
        log.info(
            "Thread alert already present for %s/#%s on %s — skipping duplicate write",
            account,
            channel,
            date_label,
        )
        return

    _ensure_alerts_header()
    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.info(
        "Thread alert written: %s/#%s age=%.0fh sender=%s",
        account,
        channel,
        age_hours,
        sender,
    )


def append_run_summary(
    *,
    run_ts: str,
    checked_accounts: int,
    total_alerts: int,
    auth_error: bool,
    dry_run: bool,
) -> None:
    """Append a run-summary HTML comment to the alerts file."""
    status = "auth-error" if auth_error else ("ok" if total_alerts == 0 else "alerts")
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
) -> tuple[int, int, bool, bool]:
    """Scan all (or a single filtered) account for stale Slack threads.

    Returns (checked_accounts, total_alerts, auth_error, state_write_failed).
    Mutates *updated_state* in-place with per-account results.
    """
    checked_accounts = 0
    total_alerts = 0
    auth_error = False
    state_write_failed = False

    # historic regression: load merged bot patterns from config once for all accounts
    bot_patterns = load_bot_patterns(config or {})

    for account_key, account_cfg in accounts.items():
        if account_filter and account_key != account_filter:
            continue
        if not isinstance(account_cfg, dict):
            log.warning("Skipping %r — account config is not a mapping", account_key)
            continue
        if account_cfg.get("slack_watch") is False or account_cfg.get("internal"):
            log.info("Skipping %r — marked as internal/slack_watch=false", account_key)
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
        except SlackAuthError as exc:
            log.error("Slack auth error: %s", exc.message, exc_info=True)
            auth_error = True
            write_auth_error_alert(exc.message, dry_run=dry_run)
            state_write_failed = _save_auth_error_state(
                updated_state, previous_state, exc.message, run_ts, dry_run=dry_run
            )
            break
        except RuntimeError as exc:
            # historic regression: non-auth slackcli failures (e.g. binary not found, timeout,
            # malformed JSON) must also surface an alert so the morning brief
            # reflects the failure — not silently skip the account.
            log.error("Slack search failed for account=%r: %s", account_key, exc, exc_info=True)
            auth_error = True
            write_auth_error_alert(str(exc), dry_run=dry_run)
            break

        checked_accounts += 1
        for thread in stale_threads:
            total_alerts += 1
            append_thread_alert(thread, dry_run=dry_run)

        # implementation note: record effective_limit (not global limit) so state reflects
        # the actual per-account search budget used.
        updated_state[account_key] = _build_account_state_entry(
            account_key, run_ts, threshold_hours, effective_limit, stale_threads
        )

    return checked_accounts, total_alerts, auth_error, state_write_failed


def _persist_and_summarise(
    *,
    updated_state: dict[str, Any],
    previous_state: dict[str, Any],
    auth_error: bool,
    dry_run: bool,
    run_ts: str,
    checked_accounts: int,
    total_alerts: int,
    elapsed: float,
) -> bool:
    """Persist state and return whether storage failed before appending its summary."""
    state_write_failed = _state_write_failed(
        dry_run=dry_run or auth_error,
        write=lambda: save_state(updated_state, previous_state=previous_state),
        logger=log,
        message="State file write failed — thread state not persisted.",
    )

    append_run_summary(
        run_ts=run_ts,
        checked_accounts=checked_accounts,
        total_alerts=total_alerts,
        auth_error=auth_error,
        dry_run=dry_run,
    )

    log.info(
        "Run complete: accounts=%d alerts=%d auth_error=%s elapsed=%.1fs dry_run=%s",
        checked_accounts,
        total_alerts,
        auth_error,
        elapsed,
        dry_run,
    )
    return state_write_failed


def _slack_outcome(*, auth_error: bool, checked_accounts: int, state_write_failed: bool) -> WatcherOutcome:
    """Classify Slack outcomes while keeping persistence failures fail-closed."""
    if state_write_failed or (auth_error and checked_accounts == 0):
        return "fatal"
    if auth_error:
        return "partial"
    return "ok" if checked_accounts > 0 else "fatal"


def _run_slack_threads(
    *,
    threshold_hours: int,
    account: str | None,
    limit: int,
    limit_per_account: int | None = None,
    dry_run: bool,
    as_json: bool = False,
) -> int:
    """Core logic; returns POSIX exit code.

    ``as_json`` emits the run-status document on stdout; the exit code is unaffected.
    """
    start = time.monotonic()
    now_utc = datetime.datetime.now(datetime.UTC)
    run_ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    with watcher_logging("slack-threads"):
        # ---- Load config --------------------------------------------------
        try:
            config = load_accounts_config()
        except RuntimeError:
            return 1
        accounts: dict[str, Any] = config.get("accounts", {})
        if not isinstance(accounts, dict) or not accounts:
            log.error("No accounts found in %s", get_config_path("accounts.yaml"))
            return 1

        if account and account not in accounts:
            log.error("Account %r not found in accounts.yaml", account)
            return 1

        # ---- Resolve current user ----------------------------------------
        current_username = load_current_username()
        log.info("Current Slack username: %s", current_username or "(unknown — will not filter self-messages)")

        # ---- Load prior state -------------------------------------------
        state = load_state()
        updated_state: dict[str, Any] = dict(state)

        # ---- Scan accounts -----------------------------------------------
        checked_accounts, total_alerts, auth_error, auth_state_write_failed = _scan_all_accounts(
            accounts,
            account_filter=account,
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
        state_write_failed = auth_state_write_failed or _persist_and_summarise(
            updated_state=updated_state,
            previous_state=state,
            auth_error=auth_error,
            dry_run=dry_run,
            run_ts=run_ts,
            checked_accounts=checked_accounts,
            total_alerts=total_alerts,
            elapsed=elapsed,
        )

        # Always exit 0 — auth errors are handled as named artifacts, not failures.
        # Fatal config/fs errors return 1 from the caller.
        #
        # historic regression: when auth_error fires before any account is checked (checked_accounts == 0),
        # the run produced zero useful data — classify as "fatal", not "partial".
        # "partial" implies some accounts were checked successfully; "fatal" signals total failure.
        outcome = _slack_outcome(
            auth_error=auth_error,
            checked_accounts=checked_accounts,
            state_write_failed=state_write_failed,
        )
        write_run_status(
            watcher="slack-threads",
            outcome=outcome,
            records_checked=checked_accounts,
            alerts_generated=total_alerts,
            failures=int(auth_error or state_write_failed),
            elapsed_seconds=elapsed,
            dry_run=dry_run,
        )
        if as_json:
            # The run happened — emit the outcome even when it is partial/fatal,
            # which is exactly when a caller needs the detail. historic regression: `outcome`
            # is reported verbatim; only "fatal" denotes a failed run, and the
            # exit code stays 0 either way.
            print(
                json.dumps(
                    {
                        "watcher": "slack-threads",
                        "outcome": outcome,
                        "records_checked": checked_accounts,
                        "alerts_generated": total_alerts,
                        "failures": int(auth_error or state_write_failed),
                        "auth_error": auth_error,
                        "elapsed_seconds": round(elapsed, 1),
                        "dry_run": dry_run,
                    },
                    indent=2,
                    default=str,
                )
            )
        return 1 if state_write_failed else 0


__all__ = [
    "_AUTH_ERROR_SIGNALS",
    "_DEFAULT_SEARCH_LIMIT",
    "_DEFAULT_THRESHOLD_HOURS",
    "_SLACKCLI",
    "SlackAuthError",
    "_accounts_config",
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
    "load_accounts_config",
    "load_current_username",
    "load_state",
    "run_slack_search",
    "save_state",
    "scan_account_threads",
    "write_auth_error_alert",
]
