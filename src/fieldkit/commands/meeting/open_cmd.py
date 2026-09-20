"""fieldkit meeting open — Open the linked Pursuit Workbook in the browser."""

import json
import webbrowser
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_DATA, cli_main
from fieldkit.meeting.docs_domain import open_doc


@click.command("open")
@click.argument("pursuit_file", type=click.Path(exists=True, path_type=Path))
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the resolved workbook URL as JSON.")
@click.help_option("-h", "--help")
def cli(pursuit_file: Path, as_json: bool) -> None:
    """Open the linked Pursuit Workbook in the default browser."""
    with cli_main():
        try:
            url = open_doc(pursuit_file)
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_DATA) from None
        except RuntimeError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_DATA) from None

        if as_json:
            click.echo(
                json.dumps({"pursuit_file": str(pursuit_file), "url": url, "opened": True}, indent=2, default=str)
            )
        else:
            click.echo(f"Opening: {url}")
        webbrowser.open(url)
