"""Import-safe Click adapter for the optional Google-backed Gmail sync."""

import json
import logging
from datetime import datetime
from pathlib import Path

import click

from fieldkit.commands.gmail.sync_lock import gmail_sync_lock
from fieldkit.gmail.discover import get_gmail_db_path

log = logging.getLogger("gmail-sync")


@click.command(name="sync")
@click.option("--db", default=lambda: str(get_gmail_db_path()), help="Path to SQLite DB", show_default=True)
@click.option("--max-messages", type=click.IntRange(min=0), default=0, help="Stop after N messages (0=all)")
@click.option("--full", is_flag=True, help="Force a resumable full-mailbox refresh.")
@click.option(
    "--since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    metavar="YYYY-MM-DD",
    help="Upsert messages on or after a UTC date.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the sync outcome as JSON.")
def cli(db: str, max_messages: int, full: bool, since: datetime | None, as_json: bool) -> None:
    """Sync Gmail → SQLite. Incremental by default; resumes from the last sync. Use --full to re-sync all messages.
    Exits 2 when interactive Google OAuth consent is required but stdin is not a TTY.
    Run interactively and open the printed URL manually; no browser is launched (open_browser=False)."""
    if full and since is not None:
        raise click.UsageError("--full and --since cannot be used together")

    db_path = Path(db).expanduser().resolve()
    from fieldkit.gmail.auth import get_gmail_service
    from fieldkit.gmail.sync_engine import run_published_sync

    service = get_gmail_service()
    with gmail_sync_lock(db_path):
        result = run_published_sync(
            service_factory=lambda: service,
            db_path=db_path,
            full=full,
            since=since,
            max_messages=max_messages,
        )
    summary = result.summary

    log.info(
        "Sync summary: %d added, %d failed (%d not found, %d unresolved)",
        summary.added,
        summary.failed,
        summary.not_found,
        summary.unresolved,
    )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "mode": result.mode,
                    "added": summary.added,
                    "failed": summary.failed,
                    "not_found": summary.not_found,
                    "unresolved": summary.unresolved,
                    "partial": summary.failed > 0,
                    "retry": {
                        "required": summary.unresolved > 0,
                        "checkpoint_key": result.checkpoint_key,
                        "checkpoint": result.checkpoint,
                    },
                },
                sort_keys=True,
            )
        )
    from fieldkit.gmail.sync_store import raise_partial_sync

    raise_partial_sync(summary)
    log.info("Gmail cache synchronized.")
