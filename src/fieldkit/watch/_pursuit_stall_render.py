"""Alert-file rendering for the pursuit-stall watcher."""

import datetime
import logging
from functools import cache
from pathlib import Path
from typing import Any

import click

from fieldkit.config import get_watchers_dir
from fieldkit.watch.dedup import alert_block_exists

log = logging.getLogger(__name__)


@cache
def _alerts_file() -> Path:
    """Return the pursuit-stall alert file path."""
    return get_watchers_dir() / "pursuit-stall-alerts.md"


def _ensure_alerts_header() -> None:
    """Create the alerts file with its header when absent."""
    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Pursuit Stall Alerts\n\nAutomated alerts written by watch_pursuit_stalls.py.\n",
            encoding="utf-8",
        )


def append_stall_alert(result: dict[str, Any], *, dry_run: bool) -> None:
    """Append a de-duplicated stall alert entry."""
    now_utc = datetime.datetime.now(datetime.UTC)
    timestamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now_utc.strftime("%Y-%m-%d")
    account = result["account"]
    pursuit = result["pursuit"]
    stage = result["stage"]
    days = result["days_since_transition"]
    alert_text = "\n".join(
        [
            "",
            f"## {date_label} — {account} / {pursuit} — stalled in {stage}",
            "",
            f"- **Account:** `{account}`",
            f"- **Pursuit:** [{pursuit}]({result['path']})",
            f"- **Stage:** `{stage}`",
            f"- **Days stuck:** {days} (threshold: {result['threshold_days']})",
            f"- **Last transition:** {result['last_transition_date']}",
            f"- **Detected at:** {timestamp}",
            f"- **Action:** Champion: {result.get('champion', '(unknown)')} | Next: {result.get('sf_next_steps', '(none)')}",
            f"- **Native qualification:** {result['native_qualification']}",
            "",
        ]
    )
    if dry_run:
        log.info("[DRY RUN] Would append alert:\n%s", alert_text)
        return

    _ensure_alerts_header()
    if alert_block_exists(_alerts_file(), f"{date_label} — {account} / {pursuit}"):
        return
    with _alerts_file().open("a", encoding="utf-8") as file_handle:
        file_handle.write(alert_text)
    log.info("Stall alert written: %s/%s (%s, %d days)", account, pursuit, stage, days)


def append_run_summary_header(
    *,
    run_ts: str,
    checked: int,
    stalled: int,
    snoozed: int = 0,
    dry_run: bool,
) -> None:
    """Append an informational watcher-run summary."""
    snooze_note = f" | snoozed={snoozed} active_stalls={stalled - snoozed}" if snoozed else ""
    summary = f"\n<!-- run: {run_ts} | checked={checked} | stalled={stalled}{snooze_note} | dry_run={dry_run} -->\n"
    if dry_run:
        log.info("[DRY RUN] Run summary: %s", summary.strip())
        return
    _ensure_alerts_header()
    with _alerts_file().open("a", encoding="utf-8") as file_handle:
        file_handle.write(summary)
    if snoozed:
        click.echo(f"Stalls: {snoozed} snoozed, {stalled - snoozed} active stalls", err=True)
