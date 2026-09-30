"""Collect optional brief inputs and publish the artifact with run-status evidence.

Publication and overall success are separate: a nonempty artifact can accompany
degraded sources or failed status persistence. Pipeline-review orchestration lives
in the brief command layer, preserving the watch domain's import boundary.
"""

import logging
from datetime import date
from functools import cache
from pathlib import Path
from stat import S_IMODE
from typing import Any

from fieldkit.config import ConfigError, get_mcp_endpoint, get_watchers_dir
from fieldkit.errors import AuthError
from fieldkit.util.atomic import assert_nonzero_write, atomic_text_write, require_nonempty_output
from fieldkit.watch._morning_brief_types import BriefWriteResult, SourceNotReady
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.mcp import MCPSession
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
from fieldkit.watch.morning_brief_render import (
    _collect_degraded_sources,
    _fmt_time,
    _render_cross_account_section,
    _render_degraded_section,
    _render_meetings_section,
)
from fieldkit.watch.status import WatcherOutcome, WatcherRunResult, write_run_status

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
            fresh installs; warning in dry-run mode would obscure useful diagnostics.
    """
    try:
        blocks = extract_today_alerts(alert_file, target_date, lookback_days=3)
        log.info("%s: %d blocks extracted", label, len(blocks))
        return blocks
    except FileNotFoundError:
        if not_found_msg:
            # Optional sources may not have run yet, especially during a preview.
            if dry_run:
                log.debug("[%s] optional alert input was not present for the preview", label)
            else:
                log.warning("[%s] alert input was not found", label)
            # An optional source that has not run is distinct from a failed source.
            return SourceNotReady(not_found_msg)
        msg = f"[{label}] unavailable: file not found"
        log.warning(msg)
        return msg
    except Exception:  # noqa: BLE001 -- one local source degrades the assembled brief
        msg = f"[{label}] unavailable: collection error (see logs)"
        log.warning("[%s] collection failed", label)
        return msg


def _collect_calendar_meetings(
    target_date: date,
    internal_domains: set[str],
    user_email: str,
    *,
    enabled: bool = True,
) -> list[dict[str, Any]] | SourceNotReady | str:
    """Fetch configured external calendar meetings without assuming a provider route."""
    if not enabled:
        return SourceNotReady("_Calendar input was not run for this local preview._")
    endpoint = get_mcp_endpoint("calendar")
    if endpoint is None:
        return SourceNotReady("_Calendar integration not configured; calendar input was not run._")
    calendar_session = MCPSession(endpoint)
    try:
        calendar_session.initialize()
        meetings = fetch_external_meetings(calendar_session, target_date, internal_domains, user_email)
        log.info("External meetings: %d fetched", len(meetings))
        return meetings
    except (AuthError, ConfigError):
        raise
    except Exception:  # noqa: BLE001 -- optional provider failures degrade the assembled brief
        log.warning("[Calendar] provider request failed")
        return "_Calendar unavailable (provider failure) — retry later._"
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
) -> BriefWriteResult:
    """Publish nonempty Markdown and report source and status-persistence failures.

    An explicit output directory selects report storage; otherwise the workspace's
    watcher directory is used. Artifact publication failures return fatal status
    without claiming a write. Empty output retains the canonical data-error guard.
    """
    import click

    require_nonempty_output(brief_md)
    records_checked = len(sources)
    source_failures = sum(1 for src in sources if isinstance(src, str))
    target_dir = output_dir if output_dir is not None else get_watchers_dir()
    brief_filename = f"morning-brief-{target_date.strftime('%Y-%m-%d')}.md"
    brief_path = target_dir / brief_filename

    brief_bytes = len(brief_md.encode())
    # Large reports can indicate repeated alert blocks across sections.
    if brief_bytes > 15_000:
        click.echo(
            f"[morning-brief] WARNING: brief is unusually large "
            f"({brief_bytes // 1024}KB > 15KB) — possible duplicate alerts. "
            "Run 'fieldkit watch run pursuit-stalls' to check suppression state.",
            err=True,
        )

    # Report existing empty artifacts for operator inspection without deleting them.
    for _stub in target_dir.glob("morning-brief-*.md") if target_dir.exists() else []:
        if _stub.stat().st_size == 0:
            log.warning("Pre-existing 0-byte brief stub found: %s — manual cleanup required", _stub.name)

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        mode = S_IMODE(brief_path.stat().st_mode) if brief_path.exists() else None
        atomic_text_write(brief_path, brief_md, mode=mode)
    except OSError:
        log.error("Morning brief persistence failed")
        status_result = write_run_status(
            watcher="morning-brief",
            outcome="fatal",
            records_checked=records_checked,
            alerts_generated=0,
            failures=source_failures + 1,
            elapsed_seconds=elapsed,
            dry_run=False,
        )
        return BriefWriteResult(
            written=False,
            run=WatcherRunResult("fatal", False, status_result),
            records_checked=records_checked,
            alerts_generated=0,
            failures=source_failures + 1 + int(status_result == "failed"),
        )

    # A completed replacement must contain content before it can count as written.
    assert_nonzero_write(brief_path)

    outcome: WatcherOutcome = "ok" if source_failures == 0 else "partial"
    status_result = write_run_status(
        watcher="morning-brief",
        outcome=outcome,
        records_checked=records_checked,
        alerts_generated=1,
        failures=source_failures,
        elapsed_seconds=elapsed,
        dry_run=dry_run,
    )
    log.info(
        "Morning brief written — file=%s source_failures=%d duration=%.1fs",
        brief_path.name,
        source_failures,
        elapsed,
    )
    return BriefWriteResult(
        written=True,
        run=WatcherRunResult("fatal" if status_result == "failed" else outcome, True, status_result),
        records_checked=records_checked,
        alerts_generated=1,
        failures=source_failures + int(status_result == "failed"),
    )


__all__ = [
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
