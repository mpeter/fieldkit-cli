"""Rendering helpers for the Salesforce listview sync command."""

import json
from collections.abc import Callable

import click
from rich.console import Console
from rich.table import Table

console = Console(stderr=True)


def print_untracked_table(all_untracked_opps: list[dict[str, str]], log: Callable[[str], None]) -> None:
    """Print open opportunities that have no local pursuit file."""
    if not all_untracked_opps:
        return
    click.echo("", err=True)
    log("Untracked opportunities (open, no local pursuit file):")
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("NAME")
    table.add_column("STAGE")
    table.add_column("ACV", justify="right")
    table.add_column("CLOSE_DATE")
    for opportunity in all_untracked_opps:
        name = opportunity.get("name", "") or ""
        stage = opportunity.get("stage", "") or ""
        acv = opportunity.get("acv") or opportunity.get("arr") or "N/A"
        close_date = opportunity.get("close_date", "") or ""
        table.add_row(name, stage, str(acv), close_date)
    console.print(table)


def print_sync_summary(
    total_updated: int,
    total_untracked: int,
    total_errors: int,
    *,
    as_json: bool = False,
    quiet: bool = False,
) -> None:
    """Print the human summary and emit its JSON representation."""
    if not quiet:
        click.echo("", err=True)
        summary_table = Table(show_header=False, box=None, pad_edge=False)
        summary_table.add_column("Label", min_width=40)
        summary_table.add_column("Count", justify="right", min_width=4)
        summary_table.add_row("Synced (pursuit file matched + written)", str(total_updated))
        summary_table.add_row("Untracked (no local pursuit file found)", str(total_untracked))
        summary_table.add_row("Errors (write failures)", str(total_errors))
        summary_table.add_row("Total opportunities seen", str(total_updated + total_untracked + total_errors))
        console.print(summary_table)

    result_json = json.dumps(
        {
            "status": "ok" if total_errors == 0 else "partial",
            "updated": total_updated,
            "untracked": total_untracked,
            "errors": total_errors,
        }
    )
    click.echo(result_json, err=not as_json)
