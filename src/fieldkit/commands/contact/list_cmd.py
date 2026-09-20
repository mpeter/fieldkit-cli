"""fieldkit contact list — List cached contacts from the people index.

Pure read of the existing people-index cache (built by ``fieldkit sync``).
Never triggers a rebuild — run 'fieldkit sync' first if the cache is stale
or missing.
"""

import json
import sqlite3

import click

from fieldkit.cli_exit import EXIT_PARTIAL, cli_main
from fieldkit.contact.people_query import list_people
from fieldkit.gmail.discover import get_gmail_db_path


@click.command("list")
@click.option("--account", default=None, metavar="SLUG", help="Restrict to this account slug")
@click.option("--limit", default=None, type=int, metavar="N", help="Return at most N people")
@click.option("--json", "output_json", is_flag=True, help="Emit JSON output instead of a table")
@click.help_option("-h", "--help")
def cli(account: str | None, limit: int | None, output_json: bool) -> None:
    """List people from the cached people index (does not rebuild the cache)."""
    with cli_main():
        db_path = get_gmail_db_path()

        try:
            people = list_people(db_path, account=account, limit=limit)
        except sqlite3.OperationalError as exc:
            if "no such table: people" in str(exc):
                click.echo(
                    "Error: people index not found. Run 'fieldkit sync' to build it first.",
                    err=True,
                )
                raise SystemExit(EXIT_PARTIAL) from None
            raise

        if output_json:
            click.echo(json.dumps(people, indent=2, default=str))
            return

        if not people:
            click.echo("No people found.")
            return

        for person in people:
            name = person.get("display_name") or "(no name)"
            email = person.get("email") or ""
            acct = person.get("account") or "—"
            msgs = person.get("message_count") or 0
            last_seen = (person.get("last_seen") or "")[:10] or "—"
            click.echo(f"{email:<40}  {name:<28}  {acct:<20}  msgs={msgs:<5}  last_seen={last_seen}")
