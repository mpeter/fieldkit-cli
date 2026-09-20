"""fieldkit contact report — Generate the contact enrichment coverage report."""

import json

import click

from fieldkit.cli_exit import cli_main
from fieldkit.contact.report import build_report


@click.command("report")
@click.option("--account", default=None, metavar="SLUG", help="Restrict the report to this account slug")
@click.option("--json", "output_json", is_flag=True, help='Emit the report as JSON ({"report": <markdown>})')
@click.help_option("-h", "--help")
def cli(account: str | None, output_json: bool) -> None:
    """Generate a markdown coverage report from enriched contacts."""
    with cli_main():
        report = build_report(account=account)

        if report is None:
            click.echo("No enriched contacts found. Run 'fieldkit contact enrich' to build the enrichment index first.")
            return

        if output_json:
            click.echo(json.dumps({"report": report}, indent=2))
            return

        click.echo(report)
