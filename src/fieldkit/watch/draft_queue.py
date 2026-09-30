#!/usr/bin/env python3
"""Draft-queue watcher — domain logic for processing stale Gmail draft data.

Moved from ``commands/watch/draft_queue.py`` (watch-domain-migration, implementation change).
Contains parsing, alert persistence, and the optional MCP-backed collection
orchestration. The provider is used only when its full endpoint is configured.
"""

import json
import logging
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import ConfigError, get_mcp_endpoint, get_user_email_from_env
from fieldkit.config import get_watchers_dir as get_watchers_dir
from fieldkit.errors import AuthError
from fieldkit.util.atomic import atomic_text_write
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.status import WatcherOutcome, WatcherRunResult, write_run_status

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@cache
def _alerts_file() -> Path:
    return get_watchers_dir() / "draft-queue-alerts.md"


# ---------------------------------------------------------------------------
# MCP request policy
# ---------------------------------------------------------------------------

_MCP_TIMEOUT = 30  # seconds per HTTP call

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Draft data helpers
# ---------------------------------------------------------------------------


def _extract_header(msg: dict[str, Any], name: str) -> str:
    """Extract a named header value from a Gmail message dict."""
    # Handles both flat {"subject": "..."} and {"payload": {"headers": [...]}} shapes
    flat_val = msg.get(name.lower()) or msg.get(name)
    if isinstance(flat_val, str):
        return flat_val

    payload = msg.get("payload", {})
    if isinstance(payload, dict):
        headers = payload.get("headers", [])
        if not isinstance(headers, list):
            return ""
        for h in headers:
            if not isinstance(h, dict):
                continue
            header_name = h.get("name")
            header_value = h.get("value")
            if isinstance(header_name, str) and header_name.lower() == name.lower():
                return header_value if isinstance(header_value, str) else ""
    return ""


def _reject_semantic_duplicates(msg: dict[str, Any]) -> None:
    """Reject provider aliases that leave identity or routing ambiguous."""
    for first, second in (("id", "draft_id"), ("subject", "Subject"), ("to", "To")):
        if first in msg and second in msg:
            raise RuntimeError("MCP returned an invalid draft search response")

    payload = msg.get("payload")
    if not isinstance(payload, dict) or "headers" not in payload:
        return
    headers = payload["headers"]
    if not isinstance(headers, list):
        return
    seen: set[str] = set()
    for header in headers:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str):
            continue
        name = header["name"].casefold()
        if name in seen:
            raise RuntimeError("MCP returned an invalid draft search response")
        seen.add(name)
    for flat_name in ("subject", "to"):
        if (flat_name in msg or flat_name.title() in msg) and flat_name in seen:
            raise RuntimeError("MCP returned an invalid draft search response")


def _format_age(internal_date_ms: int | None) -> str:
    """Return a human-readable age string like '2d 3h' from internalDate ms."""
    if internal_date_ms is None:
        return "unknown age"
    try:
        sent_dt = datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=UTC)
        delta = datetime.now(UTC) - sent_dt
        total_hours = int(delta.total_seconds() // 3600)
        days = total_hours // 24
        hours = total_hours % 24
        if days > 0:
            return f"{days}d {hours}h"
        return f"{hours}h"
    except (OverflowError, TypeError, ValueError, OSError):
        return "unknown age"


def parse_drafts(raw: Any) -> list[dict[str, str]]:
    """Parse the MCP search response into a list of draft dicts.

    Each dict has: subject, to, age, draft_id.
    Missing message fields receive display defaults. A malformed provider
    envelope fails closed so an outage cannot be reported as an empty queue.
    """
    drafts: list[dict[str, str]] = []

    messages: list[Any]
    if isinstance(raw, list):
        messages = raw
    elif isinstance(raw, dict):
        keys = [key for key in ("messages", "results") if key in raw]
        if len(keys) != 1 or set(raw) != {keys[0]} or not isinstance(raw[keys[0]], list):
            raise RuntimeError("MCP returned an invalid draft search response")
        messages = raw[keys[0]]
    else:
        raise RuntimeError("MCP returned an invalid draft search response")

    for msg in messages:
        if not isinstance(msg, dict):
            raise RuntimeError("MCP returned an invalid draft search response")
        _reject_semantic_duplicates(msg)
        for field in ("subject", "Subject", "to", "To", "id", "draft_id"):
            if field in msg and not isinstance(msg[field], str):
                raise RuntimeError("MCP returned an invalid draft search response")
        identity = msg.get("id") or msg.get("draft_id")
        if not isinstance(identity, str) or not identity.strip():
            raise RuntimeError("MCP returned an invalid draft search response")
        payload = msg.get("payload")
        if payload is not None:
            if not isinstance(payload, dict):
                raise RuntimeError("MCP returned an invalid draft search response")
            if "headers" in payload:
                headers = payload["headers"]
                if not isinstance(headers, list):
                    raise RuntimeError("MCP returned an invalid draft search response")
                for header in headers:
                    if (
                        not isinstance(header, dict)
                        or not isinstance(header.get("name"), str)
                        or not isinstance(header.get("value"), str)
                    ):
                        raise RuntimeError("MCP returned an invalid draft search response")
        subject = _extract_header(msg, "Subject") or "(no subject)"
        to = _extract_header(msg, "To") or "(unknown recipient)"
        if "internalDate" in msg and "internal_date" in msg:
            raise RuntimeError("MCP returned an invalid draft search response")
        internal_date = msg["internalDate"] if "internalDate" in msg else msg.get("internal_date")
        if internal_date is None:
            ms = None
        else:
            try:
                if isinstance(internal_date, bool) or not isinstance(internal_date, int | str):
                    raise ValueError
                if isinstance(internal_date, str) and (not internal_date.isascii() or not internal_date.isdecimal()):
                    raise ValueError
                ms = int(internal_date)
                timestamp = datetime.fromtimestamp(ms / 1000, tz=UTC)
                if ms < 0 or timestamp > datetime.now(UTC):
                    raise ValueError
            except (OverflowError, TypeError, ValueError, OSError):
                raise RuntimeError("MCP returned an invalid draft search response") from None
        age = _format_age(ms)
        draft_id = identity
        drafts.append({"subject": subject, "to": to, "age": age, "draft_id": draft_id})

    return drafts


# ---------------------------------------------------------------------------
# Alert writer
# ---------------------------------------------------------------------------


def write_alerts(drafts: list[dict[str, str]], *, dry_run: bool) -> None:
    """Write (or preview) the draft-queue-alerts.md file."""
    now_utc = datetime.now(UTC)
    date_label = now_utc.strftime("%Y-%m-%d")
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = [
        f"\n## {date_label}",
        "",
        "### Pending Outbox",
        "",
        f"*{len(drafts)} draft(s) older than 24h — checked at {ts}*",
        "",
    ]

    if drafts:
        lines.append("| Subject | To | Age | Draft ID |")
        lines.append("|---------|-----|-----|----------|")
        for d in drafts:
            subject = d["subject"].replace("|", "\\|")
            to = d["to"].replace("|", "\\|")
            lines.append(f"| {subject} | {to} | {d['age']} | {d['draft_id']} |")
    else:
        lines.append("No stale drafts found.")

    lines.append("")
    alert_text = "\n".join(lines)

    if dry_run:
        log.info("[DRY RUN] Would update the draft queue alert snapshot")
        print(alert_text)
        return

    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    header = "# Draft Queue Alerts\n\nAutomated alerts written by draft_queue.py.\n"
    existing = _alerts_file().read_text(encoding="utf-8") if _alerts_file().exists() else header

    # Remove any existing section for today before appending the latest snapshot.
    heading = f"\n## {date_label}"
    if heading in existing:
        # Find the start of today's section and the start of the next section (or EOF).
        start = existing.index(heading)
        rest_after = existing[start + 1 :]  # skip the leading \n of this heading
        next_heading = rest_after.find("\n## ", 1)
        existing = existing[:start] if next_heading == -1 else existing[:start] + rest_after[next_heading:]
        log.info("Replaced existing today section (%s) in draft-queue-alerts.md", date_label)
    else:
        log.info("First write for %s to draft-queue-alerts.md", date_label)

    atomic_text_write(_alerts_file(), existing + alert_text)
    log.info("draft-queue-alerts.md updated: %d stale draft(s) written", len(drafts))


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _resolve_user_email() -> str:
    """Resolve the user's email for draft-queue attribution.

    Uses FIELDKIT_USER_EMAIL env var (preferred) or USER + email_domain from config.
    Note: the config-file ``email:`` fallback is not used here; set FIELDKIT_USER_EMAIL
    for multi-tenant deployments where config.yaml is not available.
    """
    return get_user_email_from_env() or ""


# ---------------------------------------------------------------------------
# Core logic (unblocked by slice 2.5: MCPSession now in fieldkit.watch)
# ---------------------------------------------------------------------------


def _emit_run_json(
    *, outcome: WatcherOutcome, checked: int, alerts: int, failures: int, elapsed: float, dry_run: bool
) -> None:
    """Emit the run-status document on stdout.

    historic regression: ``outcome`` is reported verbatim; only ``"fatal"`` denotes a failed run.
    """
    print(
        json.dumps(
            {
                "watcher": "draft-queue",
                "outcome": outcome,
                "records_checked": checked,
                "alerts_generated": alerts,
                "failures": failures,
                "elapsed_seconds": round(elapsed, 1),
                "dry_run": dry_run,
            },
            indent=2,
            default=str,
        )
    )


def _run_draft_queue(*, dry_run: bool, account: str | None = None, as_json: bool = False) -> WatcherRunResult:
    """Core watcher logic; returns completion and persistence facts.

    ``as_json`` emits the run-status document on stdout at every terminal point —
    including the fatal ones, which is exactly when a caller needs the detail.
    The exit code is unaffected.
    """
    import time

    from fieldkit.watch.mcp import MCPSession

    start = time.monotonic()
    with watcher_logging("draft-queue", enabled=not dry_run):
        if dry_run:
            log.info("[DRY RUN] Skipping MCP call; reporting zero stale drafts.")
            write_alerts([], dry_run=True)
            elapsed = time.monotonic() - start
            status_write = write_run_status(
                watcher="draft-queue",
                outcome="ok",
                records_checked=0,
                alerts_generated=0,
                failures=0,
                elapsed_seconds=elapsed,
                dry_run=True,
            )
            if as_json:
                _emit_run_json(outcome="ok", checked=0, alerts=0, failures=0, elapsed=elapsed, dry_run=True)
            return WatcherRunResult("ok", True, status_write)

        endpoint = get_mcp_endpoint("draft_queue")
        if endpoint is None:
            raise ConfigError("Config key 'mcp_endpoints.draft_queue' is required for the draft-queue watcher")

        # --- Resolve user email BEFORE opening any MCP session (B8) ---
        # historic regression: user_google_email is required by search_gmail_messages.
        # Resolve from FIELDKIT_USER_EMAIL env var (required; empty string → error below).
        # Checking here avoids an unnecessary MCP handshake when the var is missing.
        user_email = _resolve_user_email()
        if not user_email:
            raise ConfigError("Draft queue requires a user email; set FIELDKIT_USER_EMAIL or configure email_domain")

        # --- Open MCP session ---
        session = MCPSession(endpoint)
        try:
            session.initialize()
        except (AuthError, ConfigError):
            session.close()
            raise
        except RuntimeError:
            session.close()
            log.error("The configured draft-queue MCP endpoint is unavailable; retry later")
            elapsed = time.monotonic() - start
            status_write = write_run_status(
                watcher="draft-queue",
                outcome="fatal",
                records_checked=0,
                alerts_generated=0,
                failures=1,
                elapsed_seconds=elapsed,
                dry_run=False,
            )
            if as_json:
                _emit_run_json(
                    outcome="fatal",
                    checked=0,
                    alerts=0,
                    failures=1 + int(status_write == "failed"),
                    elapsed=elapsed,
                    dry_run=False,
                )
            return WatcherRunResult("fatal", False, status_write)

        # --- Search stale drafts ---
        drafts: list[dict[str, str]] = []
        failures = 0
        completed = False
        try:
            raw = session.call_tool(
                "google_workspace__search_gmail_messages",
                # historic regression: added user_google_email (required); page_size is the
                # correct param name for search_gmail_messages (not max_results).
                {
                    "user_google_email": user_email,
                    "query": "in:drafts older_than:1d",
                    "page_size": 50,
                },
            )
            log.debug("Raw MCP response type=%s", type(raw).__name__)
            all_drafts = parse_drafts(raw)
            if account is not None:
                from fieldkit.ingest.router import route_by_domains

                drafts = [d for d in all_drafts if account in route_by_domains([d.get("to", "")]).accounts]
                log.info(
                    "Found %d stale draft(s) (%d total, filtered to account=%s)",
                    len(drafts),
                    len(all_drafts),
                    account,
                )
            else:
                drafts = all_drafts
            log.info("Found %d stale draft(s)", len(drafts))
            completed = True
        except (AuthError, ConfigError):
            raise
        except RuntimeError:
            log.error("Draft search failed at the configured MCP endpoint")
            failures = 1
        finally:
            session.close()

        # --- Write alerts ---
        alerts_generated = 0
        if failures == 0 and account is None:
            try:
                write_alerts(drafts, dry_run=False)
                alerts_generated = len(drafts)
            except OSError:
                log.error("Draft alert snapshot persistence failed")
                failures = 1
        elif failures == 0:
            log.info("Scoped draft check completed; the global alert snapshot was not changed")

        # --- Status & summary ---
        elapsed = time.monotonic() - start
        outcome: WatcherOutcome = "fatal" if failures > 0 else "ok"
        log.info(
            "Run summary: drafts_found=%d failures=%d elapsed=%.1fs",
            len(drafts),
            failures,
            elapsed,
        )
        status_write = write_run_status(
            watcher="draft-queue",
            outcome=outcome,
            records_checked=len(drafts),
            alerts_generated=alerts_generated,
            failures=failures,
            elapsed_seconds=elapsed,
            dry_run=False,
        )
        if status_write == "failed":
            outcome = "fatal"
            failures += 1
        if as_json:
            _emit_run_json(
                outcome=outcome,
                checked=len(drafts),
                alerts=alerts_generated,
                failures=failures,
                elapsed=elapsed,
                dry_run=False,
            )
        return WatcherRunResult(outcome, completed, status_write)
