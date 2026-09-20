"""Morning brief watcher — domain module.

Moved from ``commands/watch/morning_brief.py`` (watch-domain-migration,
implementation change slice 2.9). No Click imports — pure business logic.

Note: _collect_pipeline_review, _run_generate, and _run_generate_inner live in
commands/brief/generate.py because they depend on fieldkit.commands.pipeline.*
(tach boundary: fieldkit.watch cannot depend on fieldkit.commands).

This module provides:
- Path helpers (get_watchers_dir, alert file paths)
- _collect_alert_source
- _collect_calendar_meetings
- _write_brief_to_disk
"""

import logging
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import get_watchers_dir
from fieldkit.util.atomic import assert_nonzero_write
from fieldkit.watch._morning_brief_types import SourceNotReady
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.morning_brief_collect import (
    _accounts_dir,
    _email_domain,
    _parse_internal_domains,
    detect_cross_account_signals,
    extract_today_alerts,
    fetch_external_meetings,
    get_latest_pursuit_files,
    resolve_user_email,
)
from fieldkit.watch.morning_brief_mcp import _MCP_CALENDAR_BASE, MCPSession
from fieldkit.watch.morning_brief_render import (
    _collect_degraded_sources,
    _fmt_time,
    _render_cross_account_section,
    _render_degraded_section,
    _render_meetings_section,
)
from fieldkit.watch.status import write_run_status

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


@cache
def _backstory_alerts_file() -> Path:
    return get_watchers_dir() / "backstory-alerts.md"


@cache
def _pursuit_stall_alerts_file() -> Path:
    return get_watchers_dir() / "pursuit-stall-alerts.md"


@cache
def _slack_alerts_file() -> Path:
    return get_watchers_dir() / "slack-thread-alerts.md"


@cache
def _contract_expiry_alerts_file() -> Path:
    return get_watchers_dir() / "contract-expiry-alerts.md"


@cache
def _draft_queue_alerts_file() -> Path:
    return get_watchers_dir() / "draft-queue-alerts.md"


# ---------------------------------------------------------------------------
# Collection orchestration
# ---------------------------------------------------------------------------


def _collect_alert_source(
    alert_file: Path,
    target_date: date,
    label: str,
    *,
    not_found_msg: str | None = None,
    dry_run: bool = False,
) -> list[str] | SourceNotReady | str:
    """Extract today's alert blocks from *alert_file*, returning a string on error.

    Uses a 3-day lookback so that alerts written late on a previous day (or when
    the watcher ran on day N-1 or N-2) are still surfaced in the morning brief.

    Args:
        alert_file: Path to the alert markdown file.
        target_date: Date to extract alerts for.
        label: Human-readable label for this source (used in log messages).
        not_found_msg: When set, the source is optional — return this message
            instead of an error string when the file is missing.
        dry_run: When True and the file is missing but ``not_found_msg`` is
            provided (i.e., the source is optional), emit ``log.debug`` instead
            of ``log.warning``.  Optional sources are expected to be absent on
            fresh installs; warning in dry-run mode is noise (implementation change).
    """
    try:
        blocks = extract_today_alerts(alert_file, target_date, lookback_days=3)
        log.info("%s: %d blocks extracted", label, len(blocks))
        return blocks
    except FileNotFoundError:
        if not_found_msg:
            # implementation change: In dry-run mode, missing optional sources are expected
            # (the file may not have been generated yet).  Use debug-level so
            # the information is available for troubleshooting without polluting
            # the dry-run output with spurious warnings.
            if dry_run:
                log.debug("[%s] alert file not found (dry-run, optional source): %s", label, alert_file)
            else:
                log.warning("[%s] alert file not found: %s", label, alert_file)
            # implementation note: return SourceNotReady (not a plain str) so source_failures
            # is not inflated when an optional watcher simply hasn't run yet.
            return SourceNotReady(not_found_msg)
        msg = f"[{label}] unavailable: file not found"
        log.warning(msg)
        return msg
    except Exception as exc:  # noqa: BLE001
        # Use a generic user-facing message to avoid leaking internal paths,
        # hostnames, or API error bodies into the brief markdown. Full exception
        # detail is preserved in the log for operator diagnosis.
        msg = f"[{label}] unavailable: collection error (see logs)"
        log.warning("[%s] collection error: %s", label, exc, exc_info=True)
        return msg


def _collect_calendar_meetings(
    target_date: date,
    internal_domains: set[str],
    user_email: str,
) -> list[dict[str, Any]] | str:
    """Fetch external calendar meetings via MCP; return string on any failure."""
    calendar_session = MCPSession(_MCP_CALENDAR_BASE)
    try:
        calendar_session.initialize()
        meetings = fetch_external_meetings(calendar_session, target_date, internal_domains, user_email)
        log.info("External meetings: %d fetched", len(meetings))
        return meetings
    except Exception as exc:  # noqa: BLE001
        # historic regression: escalate to WARNING and include a sanitised error summary in the
        # returned string so the AE sees what kind of failure occurred.
        # We use the exception type name + a short fixed-vocabulary suffix rather than
        # the raw str(exc) to avoid leaking internal MCP hostnames, paths, or response
        # fragments into the user-facing brief (Constitution VIII: Security by Default).
        # The full exception is still logged at WARNING with exc_info for diagnostics.
        log.warning("[Calendar] unavailable: %s", exc, exc_info=True)
        exc_kind = type(exc).__name__
        return f"_Calendar unavailable ({exc_kind}) — check logs for details_"
    finally:
        calendar_session.close()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _write_brief_to_disk(
    brief_md: str,
    target_date: date,
    elapsed: float,
    sources: list[list[str] | SourceNotReady | str | list[dict[str, Any]] | list[dict[str, str]]],
    *,
    dry_run: bool,
    output_dir: Path | None = None,
) -> int:
    """Write the rendered brief markdown to disk and emit run_status. Returns exit code.

    output_dir defaults to get_watchers_dir() for backward compatibility; callers
    consolidating brief output (e.g. `brief generate`) pass an explicit target.
    """
    import click

    target_dir = output_dir if output_dir is not None else get_watchers_dir()
    brief_filename = f"morning-brief-{target_date.strftime('%Y-%m-%d')}.md"
    brief_path = target_dir / brief_filename

    brief_bytes = len(brief_md.encode())
    # historic regression: threshold raised from 10KB to 15KB — a healthy post-003 brief
    # with pipeline-review + champion-signals + quota sections is ~10.5KB.
    # The old 10KB threshold was calibrated before those sections were added
    # and was firing as a false positive on every run.
    if brief_bytes > 15_000:
        click.echo(
            f"[morning-brief] WARNING: brief is unusually large "
            f"({brief_bytes // 1024}KB > 15KB) — possible duplicate alerts. "
            "Run 'fieldkit watch run pursuit-stalls' to check suppression state.",
            err=True,
        )

    # implementation note: scan for pre-existing 0-byte morning-brief stubs before writing.
    # Do not auto-delete — operator must decide whether to remove or investigate.
    for _stub in target_dir.glob("morning-brief-*.md") if target_dir.exists() else []:
        if _stub.stat().st_size == 0:
            log.warning("Pre-existing 0-byte brief stub found: %s — manual cleanup required", _stub)

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief_md, encoding="utf-8")
    except OSError as exc:
        log.error("Failed to write brief to %s: %s", brief_path, exc, exc_info=True)
        write_run_status(
            watcher="morning-brief",
            outcome="fatal",
            records_checked=5,
            alerts_generated=0,
            failures=5,
            elapsed_seconds=elapsed,
            dry_run=False,
        )
        return 1

    # implementation note: post-write size guard — a 0-byte file means the write silently
    # produced no content (e.g. empty string passed in, or a partial flush).
    # Delete the stub and raise so cli_main() maps this to EXIT_DATA (3).
    assert_nonzero_write(brief_path)

    source_failures = sum(1 for src in sources if isinstance(src, str))
    write_run_status(
        watcher="morning-brief",
        outcome="ok" if source_failures == 0 else "partial",
        records_checked=5,
        alerts_generated=1,
        failures=source_failures,
        elapsed_seconds=elapsed,
        dry_run=dry_run,
    )
    log.info(
        "Morning brief written — path=%s source_failures=%d duration=%.1fs",
        brief_path,
        source_failures,
        elapsed,
    )
    return 0


__all__ = [
    "_MCP_CALENDAR_BASE",
    "MCPSession",
    "SourceNotReady",
    "_accounts_dir",
    "_backstory_alerts_file",
    "_collect_alert_source",
    "_collect_calendar_meetings",
    "_collect_degraded_sources",
    "_contract_expiry_alerts_file",
    "_draft_queue_alerts_file",
    "_email_domain",
    "_fmt_time",
    "_parse_internal_domains",
    "_pursuit_stall_alerts_file",
    "_render_cross_account_section",
    "_render_degraded_section",
    "_render_meetings_section",
    "_slack_alerts_file",
    "_write_brief_to_disk",
    "detect_cross_account_signals",
    "extract_today_alerts",
    "fetch_external_meetings",
    "get_latest_pursuit_files",
    "get_watchers_dir",
    "resolve_user_email",
    "watcher_logging",
    "write_run_status",
]
