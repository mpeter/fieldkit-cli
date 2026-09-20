"""fieldkit contact find — Look up a contact by email or name.

Usage:
    fieldkit contact find <query> [--db PATH] [--affiliations] [--json]
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from fieldkit.cli_exit import EXIT_PARTIAL, cli_main
from fieldkit.config import get_accounts_root
from fieldkit.contact.resolver import resolve, scan_pursuit_affiliations

logger = logging.getLogger(__name__)

console = Console()


def _fmt_date(val: str | None) -> str:
    return val[:10] if val else "—"


def _parse_thread_date(raw_date: str) -> str:
    """Parse an RFC 2822 or ISO date string to YYYY-MM-DD, falling back to prefix."""
    try:
        from email.utils import parsedate

        parsed = parsedate(raw_date)
        if parsed:
            return f"{parsed[0]}-{parsed[1]:02d}-{parsed[2]:02d}"
    except (IndexError, TypeError, ValueError):
        # parsedate() returns None on bad input (no raise), but the tuple
        # indexing/formatting can still raise IndexError, TypeError, or ValueError
        # for malformed RFC 2822 date strings — fall through to the prefix fallback.
        logger.debug("contact find: failed to parse date string %r", raw_date, exc_info=True)
    return raw_date[:10]


def _print_not_found(result: dict[str, Any]) -> None:
    q = result.get("email") or result.get("query", "")
    click.echo(f"No contact found for: {q}")


def _print_ambiguous(result: dict[str, Any]) -> None:
    q = result.get("query", "")
    candidates = result.get("candidates") or []
    click.echo(f"Ambiguous: '{q}' matched {len(candidates)} contacts\n")
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Email", min_width=35)
    table.add_column("Name", min_width=25)
    table.add_column("Account", min_width=20)
    table.add_column("Msgs", justify="right", min_width=6)
    for c in candidates:
        table.add_row(
            c.get("email") or "",
            c.get("display_name") or "",
            c.get("account") or "",
            str(c.get("message_count") or 0),
        )
    console.print(table)
    click.echo("\nRe-run with the exact email to resolve.")


def _print_identity(result: dict[str, Any]) -> None:
    click.echo("=" * 60)
    click.echo("IDENTITY")
    click.echo("=" * 60)
    click.echo(f"  Name:       {result.get('display_name', '—')}")
    click.echo(f"  Email:      {result.get('email', '—')}")
    click.echo(f"  Domain:     {result.get('domain', '—')}")
    click.echo(f"  Account:    {result.get('account', '—')}")
    click.echo(f"  Internal:   {'Yes' if result.get('is_internal') else 'No'}")


def _print_communication(result: dict[str, Any]) -> None:
    click.echo("\nCOMMUNICATION")
    click.echo("-" * 60)
    click.echo(f"  Messages:   {result.get('message_count', 0)}")
    click.echo(f"  Threads:    {result.get('thread_count', 0)}")
    click.echo(f"  Initiated:  {result.get('initiated_count', 0)}")
    click.echo(f"  Meetings:   {result.get('meeting_count', 0)}")
    click.echo(f"  First seen: {_fmt_date(result.get('first_seen'))}")
    click.echo(f"  Last seen:  {_fmt_date(result.get('last_seen'))}")
    if result.get("is_internal"):
        click.echo(f"  Slack ID:   {result.get('slack_user_id', '—')}")
        click.echo(f"  Slack msgs: {result.get('slack_message_count', 0)}")


def _print_threads(threads: list[dict[str, Any]]) -> None:
    if not threads:
        return
    click.echo(f"\nRECENT THREADS ({len(threads)} shown)")
    click.echo("-" * 60)
    for t in threads:
        date_ = _parse_thread_date(t.get("date_str") or "")
        subj = t.get("subject") or "(no subject)"
        snippet = (t.get("snippet") or "").replace("\n", " ")
        click.echo(f"  {date_}  {subj}")
        if snippet:
            click.echo(f"            {snippet[:120]}")


def _print_meetings(meetings: list[dict[str, Any]]) -> None:
    if not meetings:
        return
    click.echo(f"\nRECENT MEETINGS ({len(meetings)} shown)")
    click.echo("-" * 60)
    for m in meetings:
        click.echo(f"  {_fmt_date(m.get('start_time'))}  {m.get('summary') or '(no title)'}")


def _print_affiliations(affiliations: list[dict[str, str | None]] | None) -> None:
    if affiliations is None:
        return
    click.echo(f"\nPURSUIT AFFILIATIONS ({len(affiliations)} found)")
    click.echo("-" * 60)
    if not affiliations:
        click.echo("  None found.")
        return
    for a in affiliations:
        click.echo(f"  {a.get('pursuit_file', '')}")
        click.echo(
            f"    Name: {a.get('name') or '—'}  |  Title: {a.get('title') or '—'}  |  MEDDPICC: {a.get('meddpicc_role') or '—'}"
        )


def _print_human(result: dict[str, Any], affiliations: list[dict[str, str | None]] | None) -> None:
    """Print a human-readable contact card."""
    rtype = result.get("type")

    if rtype == "not_found":
        _print_not_found(result)
        return

    if rtype == "ambiguous":
        _print_ambiguous(result)
        return

    # resolved
    _print_identity(result)
    _print_communication(result)

    click.echo("\nSIGNALS")
    click.echo("-" * 60)
    click.echo(f"  Champion:   {result.get('champion_signal') or '—'}")
    click.echo(f"  Engagement: {result.get('decay_signal') or '—'}")

    _print_threads(result.get("recent_threads", []))
    _print_meetings(result.get("recent_meetings", []))
    _print_affiliations(affiliations)
    click.echo()


@click.command("find")
@click.argument("query")
@click.option("--db", default=None, metavar="PATH", help="Path to gmail.db (defaults to configured gmail_db path)")
@click.option("--affiliations", is_flag=True, help="Also scan pursuit files for matching stakeholder tables")
@click.option("--json", "output_json", is_flag=True, help="Emit JSON output instead of human-readable card")
@click.help_option("-h", "--help")
def cli(query: str, db: str | None, affiliations: bool, output_json: bool) -> None:
    """Look up a contact by email or name and display a unified profile."""
    with cli_main():
        db_path = Path(db) if db else None

        try:
            result = resolve(query, db_path)
        except sqlite3.OperationalError as exc:
            if "no such table: people" in str(exc):
                click.echo(
                    "Error: people index not found. Run 'fieldkit sync' to build it first.",
                    err=True,
                )
                raise SystemExit(EXIT_PARTIAL) from None
            raise

        affil_data = None
        if affiliations:
            # implementation change: pass get_accounts_root() so the scanner resolves against the
            # configured data-root rather than a hardcoded path.
            affil_data = scan_pursuit_affiliations(query, accounts_root=get_accounts_root())

        if output_json:
            payload = dict(result)
            if affil_data is not None:
                payload["affiliations"] = affil_data
            click.echo(json.dumps(payload, indent=2, default=str))
        else:
            _print_human(result, affil_data)
