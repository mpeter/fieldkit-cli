#!/usr/bin/env python3
"""Backstory account health watcher — CLI adapter.

Business logic lives in ``fieldkit.watch.backstory_health``.
This module contains only the Click command and legacy entry point.

Usage:
    fieldkit watch run backstory-health
    fieldkit watch run backstory-health --threshold 50
    fieldkit watch run backstory-health --dry-run
    fieldkit watch run backstory-health --account <account-slug>

Exit codes:
    0  All accounts checked; zero or more alerts written.
    1  Fatal configuration or MCP gateway error (process-level failure).
"""

import click

from fieldkit.cli_registry import declare_write
from fieldkit.watch.backstory_health import (
    _DEFAULT_THRESHOLD,
    _MCP_BASE,
    _MCP_TIMEOUT,
    MCPSession,
    _accounts_config,
    _alerts_file,
    _check_all_accounts,
    _load_and_filter_accounts,
    _open_mcp_session,
    _run_backstory_health,
    _state_file,
    account_threshold,
    append_alert,
    append_api_error,
    check_account,
    compute_health_score,
    find_account_id,
    get_engagement_score,
    get_risk_count,
    get_watchers_dir,
    load_accounts_config,
    load_state,
    log_run_summary,
    save_state,
)

__all__ = [
    "_DEFAULT_THRESHOLD",
    "_MCP_BASE",
    "_MCP_TIMEOUT",
    "MCPSession",
    "_accounts_config",
    "_alerts_file",
    "_check_all_accounts",
    "_load_and_filter_accounts",
    "_open_mcp_session",
    "_run_backstory_health",
    "_state_file",
    "account_threshold",
    "append_alert",
    "append_api_error",
    "check_account",
    "compute_health_score",
    "find_account_id",
    "get_engagement_score",
    "get_risk_count",
    "get_watchers_dir",
    "load_accounts_config",
    "load_state",
    "log_run_summary",
    "save_state",
    "write_run_status",
]

# Re-export write_run_status so existing patches on this module still work.
from fieldkit.watch.status import write_run_status


@declare_write("workspace")
@click.command("backstory-health")
@click.option(
    "--threshold",
    type=int,
    default=_DEFAULT_THRESHOLD,
    show_default=True,
    help="Alert threshold for mean engagement_level. Override per account in accounts.yaml via health_score_threshold.",
)
@click.option(
    "--account",
    metavar="KEY",
    default=None,
    help="Only check this account key (as defined in accounts.yaml). Default: all.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Check scores and log what would be written; do not modify any files.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the run outcome as JSON.")
def cli(threshold: int, account: str | None, dry_run: bool, as_json: bool) -> None:
    """Backstory account health watcher — detects engagement drops and writes alerts."""
    raise SystemExit(_run_backstory_health(threshold=threshold, account=account, dry_run=dry_run, as_json=as_json))


def main(argv: list[str] | None = None) -> int:
    """Legacy entry point shim; returns POSIX exit code."""
    try:
        cli.main(args=argv, standalone_mode=False)
        return 0
    except click.exceptions.Exit as exc:
        return exc.exit_code if isinstance(exc.exit_code, int) else 0
    except click.exceptions.UsageError as exc:
        click.echo(f"Error: {exc.format_message()}", err=True)
        return 2
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
