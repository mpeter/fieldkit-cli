#!/usr/bin/env python3
"""Draft-queue watcher — CLI wiring.

All business logic lives in ``fieldkit.watch.draft_queue``. This file is
Click wiring only (parse args → call domain → exit with code).

Usage:
    fieldkit watch run draft-queue
    fieldkit watch run draft-queue --dry-run

Exit codes:
    0  Success; zero or more stale drafts written to alerts file.
    1  MCP gateway unavailable or fatal configuration error.
"""

import click

from fieldkit.cli_registry import declare_write
from fieldkit.watch.draft_queue import (
    _MCP_BASE,
    _MCP_TIMEOUT,
    _alerts_file,
    _extract_header,
    _format_age,
    _resolve_user_email,
    _run_draft_queue,
    get_watchers_dir,
    log,
    parse_drafts,
    write_alerts,
)
from fieldkit.watch.morning_brief_mcp import MCPSession

__all__ = [
    "_MCP_BASE",
    "_MCP_TIMEOUT",
    "MCPSession",
    "_alerts_file",
    "_extract_header",
    "_format_age",
    "_resolve_user_email",
    "_run_draft_queue",
    "get_watchers_dir",
    "log",
    "parse_drafts",
    "write_alerts",
]


# ---------------------------------------------------------------------------
# Click CLI
# ---------------------------------------------------------------------------


@declare_write("workspace")
@click.command("draft-queue")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Log what would be written; do not call MCP or modify any files.",
)
@click.option("--account", "-a", default=None, help="Limit draft-queue check to a single account slug.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the run outcome as JSON.")
def cli(dry_run: bool, account: str | None, as_json: bool) -> None:
    """Draft-queue watcher — lists stale Gmail drafts (>24h) and writes alerts."""
    from fieldkit.commands._account_guard import validate_account_slug

    validate_account_slug(account)
    raise SystemExit(_run_draft_queue(dry_run=dry_run, account=account, as_json=as_json))


if __name__ == "__main__":
    raise SystemExit(cli())
