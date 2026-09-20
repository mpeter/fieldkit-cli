"""fieldkit meeting link — Create a Pursuit Workbook and link it to a pursuit."""

import json
import webbrowser
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_DATA, cli_main
from fieldkit.meeting.docs_domain import link


@click.command("link")
@click.argument("pursuit_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--open", "-o", "open_browser", is_flag=True, default=False, help="Open the workbook in the browser after creation."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the workbook link result as JSON.")
@click.help_option("-h", "--help")
def cli(pursuit_file: Path, open_browser: bool, as_json: bool) -> None:
    """Create a Pursuit Workbook GDoc and write its ID to frontmatter.

    The workbook is a single pageless Google Doc with tabs:
      Tab 1 (Overview): narrative, stakeholders, MEDDPICC, risks, scope
      Additional tabs are added per meeting via 'fieldkit meeting note'.
    """
    with cli_main():
        # historic regression: wrap frontmatter read in ValueError guard — no traceback on bad files
        try:
            result = link(pursuit_file)
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_DATA) from None

        if as_json:
            click.echo(
                json.dumps(
                    {
                        "pursuit_file": str(pursuit_file),
                        "url": result.url,
                        "already_linked": result.already_linked,
                        "created": not result.already_linked,
                    },
                    indent=2,
                    default=str,
                )
            )
        elif result.already_linked:
            click.echo(f"Already linked: {result.url}")
        else:
            click.echo(f"Created Pursuit Workbook: {result.url}")
            click.echo(f"Frontmatter updated: {pursuit_file}")

        # implementation change: open in browser when --open / -o flag is set.
        # An already-linked workbook is not re-opened (pre-existing behavior).
        if open_browser and not result.already_linked:
            webbrowser.open(result.url)
