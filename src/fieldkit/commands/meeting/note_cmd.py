"""fieldkit meeting note — Add a meeting note as a workbook tab."""

import json
import logging
import sys
import webbrowser
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_DATA, cli_main
from fieldkit.meeting.docs_domain import add_note

logger = logging.getLogger(__name__)


@click.command("note")
@click.argument("pursuit_file", type=click.Path(exists=True, path_type=Path))
@click.option("--title", "meeting_title", default="", help="Meeting title (used as tab name)")
@click.option("--content", default="", help="Meeting note content (reads from stdin if not provided)")
@click.option(
    "--open",
    "-o",
    "open_browser",
    is_flag=True,
    default=False,
    help="Open the workbook in the browser after adding the tab.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the added-tab result as JSON.")
@click.help_option("-h", "--help")
def cli(pursuit_file: Path, meeting_title: str, content: str, open_browser: bool, as_json: bool) -> None:
    """Add a meeting note as a new tab in the Pursuit Workbook."""
    with cli_main():
        # historic regression: only show interactive prompt when stdin is a real terminal
        if not content and sys.stdin.isatty():
            tab_preview = f"{meeting_title}" if meeting_title else "Meeting Note"
            click.echo(f"Meeting note content for '{tab_preview}' (Ctrl+D to finish):")
            lines: list[str] = []
            try:
                while True:
                    lines.append(input())
            except EOFError:
                logger.debug("meeting note: EOFError — interactive input terminated (Ctrl+D)", exc_info=True)
            content = "\n".join(lines)

        try:
            result = add_note(pursuit_file, meeting_title, content)
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_DATA) from None
        except RuntimeError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_DATA) from None

        if as_json:
            click.echo(
                json.dumps(
                    {
                        "pursuit_file": str(pursuit_file),
                        "tab_name": result.tab_name,
                        "url": result.url,
                        "added": True,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            click.echo(f"Added tab '{result.tab_name}' to Pursuit Workbook: {result.url}")

        # implementation change: open in browser when --open / -o flag is set
        if open_browser:
            webbrowser.open(result.url)
