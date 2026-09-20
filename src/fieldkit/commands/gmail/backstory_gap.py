#!/usr/bin/env python3
"""Backstory gap report — contacts visible in first-party signals but invisible to Backstory/CRM.

For each account in config/accounts.yaml, queries the people table for external contacts
with engagement above the blindspots_min_messages threshold. These are contacts the AE
interacts with regularly that Backstory/CRM may not track.

Usage:
    python tools/gmail-cache/backstory-gap.py
    python tools/gmail-cache/backstory-gap.py --account global-pay
    python tools/gmail-cache/backstory-gap.py --min-messages 10
    python tools/gmail-cache/backstory-gap.py --account acme-bank --min-messages 5
"""

import json
import sqlite3
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, cast

import click
import yaml

from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.config import get_fieldkit_home
from fieldkit.gmail.discover import NOISE_REGEX, get_gmail_db_path


@cache
def _repo_root() -> Path:
    return get_fieldkit_home()


@cache
def _config_path() -> Path:
    return _repo_root() / "config" / "accounts.yaml"


def is_noise(email: str) -> bool:
    """Return True if the email address matches a known noise pattern (noreply, automated, etc.)."""
    return bool(NOISE_REGEX.search(email))


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection to the Gmail cache database, or exit if it doesn't exist."""
    if not db_path.exists():
        click.echo(f"ERROR: database not found: {db_path}", err=True)
        raise SystemExit(EXIT_PARTIAL)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def load_config(config_path: Path) -> dict[str, Any]:
    """Load and return the accounts YAML config, or exit if the file is missing."""
    if not config_path.exists():
        click.echo(f"ERROR: config not found: {config_path}", err=True)
        raise SystemExit(EXIT_PARTIAL)
    with config_path.open(encoding="utf-8") as f:
        return cast(dict[str, Any], yaml.safe_load(f))


def gap_report_for_account(
    conn: sqlite3.Connection,
    account_key: str,
    account_cfg: dict[str, Any],
    min_messages: int,
) -> list[dict[str, Any]]:
    """Return rows for external contacts meeting the engagement threshold."""
    domains = account_cfg.get("domains", [])
    if not domains:
        return []

    # Build domain filter: contact's email domain must be in account's domain list
    domain_placeholders = ",".join("?" * len(domains))
    query = f"""
        SELECT
            email,
            display_name,
            message_count,
            thread_count,
            COALESCE(meeting_count, 0)       AS meeting_count,
            COALESCE(slack_message_count, 0)  AS slack_message_count,
            last_seen
        FROM people
        WHERE is_internal = 0
          AND account = ?
          AND message_count >= ?
          AND LOWER(SUBSTR(email, INSTR(email, '@') + 1)) IN ({domain_placeholders})
        ORDER BY message_count DESC
    """
    params = [account_key, min_messages] + [d.lower() for d in domains]
    rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        if is_noise(row["email"]):
            continue
        results.append(
            {
                "email": row["email"],
                "name": row["display_name"] or "",
                "message_count": row["message_count"],
                "thread_count": row["thread_count"],
                "meeting_count": row["meeting_count"],
                "slack_message_count": row["slack_message_count"],
                "last_seen": row["last_seen"] or "",
            }
        )
    return results


def render_markdown(
    account_key: str,
    contacts: list[dict[str, Any]],
    min_messages: int,
    as_of: str,
) -> str:
    """Render a gap-contact list as a markdown section with a summary table."""
    lines = []
    lines.append(f"## {account_key}")
    lines.append("")
    lines.append(f"*As of {as_of} — contacts with ≥{min_messages} messages, not tracked in Backstory/CRM*")
    lines.append("")

    if not contacts:
        lines.append("_No gap contacts found at this threshold._")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"**{len(contacts)} contact(s) visible in first-party signals**")
    lines.append("")
    lines.append("| Email | Name | Msgs | Threads | Meetings | Slack | Last Seen |")
    lines.append("|-------|------|------|---------|----------|-------|-----------|")
    for c in contacts:
        last = c["last_seen"][:10] if c["last_seen"] else "—"
        lines.append(
            f"| {c['email']} | {c['name']} | {c['message_count']} "
            f"| {c['thread_count']} | {c['meeting_count']} "
            f"| {c['slack_message_count']} | {last} |"
        )
    lines.append("")
    return "\n".join(lines)


def _emit_json(
    conn: sqlite3.Connection,
    accounts: dict[str, Any],
    min_messages: int | None,
    as_of: str,
    account_filter: str | None,
) -> None:
    """Emit the gap report as a machine-readable document on stdout.

    Each contact carries the ``min_messages`` threshold that was actually
    applied to it — the per-account default differs from account to account,
    so a single top-level threshold would misdescribe most rows.
    """
    items: list[dict[str, Any]] = []
    for account_key, account_cfg in accounts.items():
        threshold = min_messages if min_messages is not None else account_cfg.get("blindspots_min_messages", 20)
        for contact in gap_report_for_account(conn, account_key, account_cfg, threshold):
            items.append({"account": account_key, "min_messages": threshold, **contact})

    click.echo(
        json.dumps(
            {
                "items": items,
                "count": len(items),
                "as_of": as_of,
                "filters": {"account": account_filter, "min_messages": min_messages},
            },
            indent=2,
            default=str,
        )
    )


@click.command(name="backstory-gap")
@click.option(
    "--account",
    default=None,
    help="Restrict to a single account key (e.g. global-pay, acme-bank, shield-ins). Default: all accounts.",
)
@click.option(
    "--min-messages",
    type=int,
    default=None,
    help="Minimum message count threshold. Defaults to blindspots_min_messages from config/accounts.yaml per account.",
)
@click.option("--db", default=lambda: str(get_gmail_db_path()), help="Path to gmail.db", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def cli(account: str | None, min_messages: int | None, db: str, as_json: bool) -> None:
    """Backstory gap report — contacts visible in Gmail/Calendar/Slack but not in Backstory/CRM."""
    config = load_config(_config_path())
    accounts = config.get("accounts", {})

    if account:
        if account not in accounts:
            raise click.UsageError(f"Unknown account '{account}'. Valid: {', '.join(accounts)}")
        accounts = {account: accounts[account]}

    conn = connect(Path(db))
    try:
        as_of = datetime.now(UTC).strftime("%Y-%m-%d")

        if as_json:
            _emit_json(conn, accounts, min_messages, as_of, account)
            return

        click.echo("# Backstory Gap Report")
        click.echo("")
        click.echo("Contacts visible in first-party signals (Gmail · Calendar · Slack) but not in Backstory/CRM.")
        click.echo(f"Generated: {as_of}")
        click.echo("")

        total_gap = 0
        for account_key, account_cfg in accounts.items():
            threshold = min_messages if min_messages is not None else account_cfg.get("blindspots_min_messages", 20)
            contacts = gap_report_for_account(conn, account_key, account_cfg, threshold)
            total_gap += len(contacts)
            click.echo(render_markdown(account_key, contacts, threshold, as_of))

        click.echo("---")
        click.echo(f"**Total gap contacts across all accounts: {total_gap}**")
    finally:
        conn.close()


if __name__ == "__main__":
    cli()
