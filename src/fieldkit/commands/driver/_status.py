"""fieldkit.commands.driver._status — Print recent driver run results."""

import json
import logging
from typing import Any

import click

from fieldkit.config import get_fieldkit_data

log = logging.getLogger(__name__)


def _emit_json(runs: list[dict[str, Any]], limit: int, error: str | None = None) -> None:
    """Emit the run list as a machine-readable document on stdout.

    An unreadable status file is reported in the ``error`` field rather than by
    an empty stdout: this path exits 0 today, and a caller that trusts exit 0
    would otherwise parse nothing and read it as "no runs".
    """
    click.echo(
        json.dumps(
            {"items": runs, "count": len(runs), "error": error, "filters": {"limit": limit}},
            indent=2,
            default=str,
        )
    )


def print_status(*, limit: int = 10, as_json: bool = False) -> None:
    """Print the last *limit* driver run entries from driver-run-status.json."""
    status_file = get_fieldkit_data() / "logs" / "driver" / "driver-run-status.json"

    if not status_file.exists():
        if as_json:
            _emit_json([], limit)
        else:
            click.echo("No driver runs recorded yet.")
        return

    try:
        with status_file.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read driver-run-status.json: %s", exc)
        click.echo("Could not read driver run status.", err=True)
        if as_json:
            _emit_json([], limit, error="could not read driver run status")
        return

    runs: list[dict[str, Any]] = data.get("runs", [])
    if not runs:
        if as_json:
            _emit_json([], limit)
        else:
            click.echo("No driver runs recorded yet.")
        return

    recent = runs[-limit:]
    if as_json:
        # Newest first, matching the order the prose renderer uses.
        _emit_json(list(reversed(recent)), limit)
        return

    for entry in reversed(recent):
        outcome = entry.get("outcome", "?")
        sym = {
            "ok": click.style("✓", fg="green"),
            "dry-run": click.style("~", fg="cyan"),
            "failed": click.style("✗", fg="red"),
            "skipped": click.style("-", fg="yellow"),
        }.get(outcome, "?")

        issue_num = entry.get("issue_number")
        title = entry.get("issue_title", "")
        ts = entry.get("ts", "")
        elapsed = entry.get("elapsed_seconds", 0.0)

        issue_str = f"#{issue_num}" if issue_num else "(none)"
        click.echo(f"  {sym} {ts[:19]}  {issue_str:>6}  {title[:50]:<50}  {elapsed:.0f}s")

        if entry.get("spend_note"):
            click.echo(f"           {entry['spend_note']}")
        if entry.get("error"):
            click.echo(click.style(f"           {entry['error']}", fg="red"))
