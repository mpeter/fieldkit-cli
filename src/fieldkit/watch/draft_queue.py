#!/usr/bin/env python3
"""Draft-queue watcher — domain logic for processing stale Gmail draft data.

Moved from ``commands/watch/draft_queue.py`` (watch-domain-migration, implementation change).
Contains the pure business logic for parsing draft responses and writing alerts.
No Click or MCP imports — CLI wiring and MCP integration stay in
``commands/watch/draft_queue.py``.

Note: _run_draft_queue stays in commands/watch/draft_queue.py until slice 2.5
(morning_brief_mcp.py migration) provides MCPSession in the domain layer.
``fieldkit.watch`` cannot depend on ``fieldkit.commands`` (tach boundary).

Module-level constant _MCP_BASE is computed from get_mcp_gateway_base() at
import time (frozen). Tests must mock get_mcp_gateway_base before importing
this module, or patch _MCP_BASE directly.
"""

import json
import logging
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import get_mcp_gateway_base as _get_mcp_gateway_base
from fieldkit.config import get_user_email_from_env
from fieldkit.config import get_watchers_dir as get_watchers_dir
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.status import WatcherOutcome, write_run_status

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@cache
def _alerts_file() -> Path:
    return get_watchers_dir() / "draft-queue-alerts.md"


# ---------------------------------------------------------------------------
# MCP gateway constant (frozen at import time — see module docstring)
# ---------------------------------------------------------------------------

_MCP_BASE = f"{_get_mcp_gateway_base()}/v0/groups/fieldkit-mail/mcp"
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
        headers: list[dict[str, str]] = payload.get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
    return ""


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
    except (TypeError, ValueError, OSError):
        return "unknown age"


def parse_drafts(raw: Any) -> list[dict[str, str]]:
    """Parse the MCP search response into a list of draft dicts.

    Each dict has: subject, to, age, draft_id.
    Handles missing fields gracefully.
    """
    drafts: list[dict[str, str]] = []

    messages: list[Any] = []
    if isinstance(raw, list):
        messages = raw
    elif isinstance(raw, dict):
        # Some tools return {"messages": [...]} or {"results": [...]}
        messages = raw.get("messages") or raw.get("results") or []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        subject = _extract_header(msg, "Subject") or "(no subject)"
        to = _extract_header(msg, "To") or "(unknown recipient)"
        internal_date = msg.get("internalDate") or msg.get("internal_date")
        try:
            ms = int(internal_date) if internal_date is not None else None
        except (TypeError, ValueError):
            ms = None
        age = _format_age(ms)
        draft_id = str(msg.get("id") or msg.get("draft_id") or "")
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
        log.info("[DRY RUN] Would write to %s", _alerts_file())
        print(alert_text)
        return

    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Draft Queue Alerts\n\nAutomated alerts written by draft_queue.py.\n",
            encoding="utf-8",
        )

    # Remove any existing section for today before appending the latest snapshot.
    existing = _alerts_file().read_text(encoding="utf-8")
    heading = f"\n## {date_label}"
    if heading in existing:
        # Find the start of today's section and the start of the next section (or EOF).
        start = existing.index(heading)
        rest_after = existing[start + 1 :]  # skip the leading \n of this heading
        next_heading = rest_after.find("\n## ", 1)
        existing = existing[:start] if next_heading == -1 else existing[:start] + rest_after[next_heading:]
        _alerts_file().write_text(existing, encoding="utf-8")
        log.info("Replaced existing today section (%s) in draft-queue-alerts.md", date_label)
    else:
        log.info("First write for %s to draft-queue-alerts.md", date_label)

    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
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


def _run_draft_queue(*, dry_run: bool, account: str | None = None, as_json: bool = False) -> int:
    """Core watcher logic; returns POSIX exit code.

    ``as_json`` emits the run-status document on stdout at every terminal point —
    including the fatal ones, which is exactly when a caller needs the detail.
    The exit code is unaffected.
    """
    import time

    from fieldkit.watch.morning_brief_mcp import MCPSession

    start = time.monotonic()
    with watcher_logging("draft-queue"):
        if dry_run:
            log.info("[DRY RUN] Skipping MCP call; reporting zero stale drafts.")
            write_alerts([], dry_run=True)
            elapsed = time.monotonic() - start
            write_run_status(
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
            return 0

        # --- Resolve user email BEFORE opening any MCP session (B8) ---
        # historic regression: user_google_email is required by search_gmail_messages.
        # Resolve from FIELDKIT_USER_EMAIL env var (required; empty string → error below).
        # Checking here avoids an unnecessary MCP handshake when the var is missing.
        user_email = _resolve_user_email()
        if not user_email:
            log.error(
                "Cannot determine user_google_email for MCP call. "
                "Set FIELDKIT_USER_EMAIL to your Google Workspace email address."
            )
            elapsed = time.monotonic() - start
            write_run_status(
                watcher="draft-queue",
                outcome="fatal",
                records_checked=0,
                alerts_generated=0,
                failures=1,
                elapsed_seconds=elapsed,
                dry_run=False,
            )
            if as_json:
                _emit_run_json(outcome="fatal", checked=0, alerts=0, failures=1, elapsed=elapsed, dry_run=False)
            return 1

        # --- Open MCP session ---
        session = MCPSession(_MCP_BASE)
        try:
            session.initialize()
        except RuntimeError as exc:
            log.error(
                "Cannot reach the configured mail MCP gateway: %s\n"
                "Verify the configured MCP gateway endpoint is available.",
                exc,
            )
            elapsed = time.monotonic() - start
            write_run_status(
                watcher="draft-queue",
                outcome="fatal",
                records_checked=0,
                alerts_generated=0,
                failures=1,
                elapsed_seconds=elapsed,
                dry_run=False,
            )
            if as_json:
                _emit_run_json(outcome="fatal", checked=0, alerts=0, failures=1, elapsed=elapsed, dry_run=False)
            return 1

        # --- Search stale drafts ---
        drafts: list[dict[str, str]] = []
        failures = 0
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
        except RuntimeError as exc:
            log.error("Failed to search drafts via MCP: %s", exc, exc_info=True)
            failures = 1
        finally:
            session.close()

        # --- Write alerts ---
        if failures == 0:
            write_alerts(drafts, dry_run=False)

        # --- Status & summary ---
        elapsed = time.monotonic() - start
        outcome: WatcherOutcome = "fatal" if failures > 0 else "ok"
        log.info(
            "Run summary: drafts_found=%d failures=%d elapsed=%.1fs",
            len(drafts),
            failures,
            elapsed,
        )
        write_run_status(
            watcher="draft-queue",
            outcome=outcome,
            records_checked=len(drafts),
            alerts_generated=len(drafts),
            failures=failures,
            elapsed_seconds=elapsed,
            dry_run=False,
        )
        if as_json:
            _emit_run_json(
                outcome=outcome,
                checked=len(drafts),
                alerts=len(drafts),
                failures=failures,
                elapsed=elapsed,
                dry_run=False,
            )
        return 1 if failures > 0 else 0
