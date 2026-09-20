"""fieldkit meeting list — List pursuits with a linked Pursuit Workbook."""

import json

import click

from fieldkit.cli_exit import cli_main
from fieldkit.commands._account_guard import validate_account_slug
from fieldkit.config import get_fieldkit_home
from fieldkit.meeting.docs_domain import list_meetings


@click.command("list")
@click.option(
    "--account",
    "-a",
    default=None,
    metavar="SLUG",
    help="Limit the listing to a single account slug. Default: all accounts.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the listing as JSON.")
@click.help_option("-h", "--help")
def cli(account: str | None, as_json: bool) -> None:
    """List all pursuits that have a linked Pursuit Workbook.

    Scans accounts/*/pursuits/*.md under the data root and prints one line
    per pursuit whose frontmatter contains a non-empty 'gdoc_workbook' field.

    Output format: <relative_path>  <doc_url>

    Exit codes: 0 success; 3 unknown account slug.
    """
    validate_account_slug(account)
    with cli_main():
        data_root = get_fieldkit_home()

        if as_json:
            items = [
                {"relative_path": str(entry.relative_path), "url": entry.url}
                for entry in list_meetings(data_root, account)
            ]
            click.echo(
                json.dumps(
                    {"items": items, "count": len(items), "filters": {"account": account}}, indent=2, default=str
                )
            )
            return

        header_printed = False
        for entry in list_meetings(data_root, account):
            if not header_printed:
                click.echo("PATH  URL")
                header_printed = True
            click.echo(f"{entry.relative_path}  {entry.url}")
