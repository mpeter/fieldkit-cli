"""fieldkit meeting — Pursuit Workbook (Google Docs) commands."""

import click

from fieldkit.commands._lazy import make_lazy_group

_COMMANDS: dict[str, str] = {
    "link": "fieldkit.commands.meeting.link_cmd",
    "list": "fieldkit.commands.meeting.list_cmd",
    "note": "fieldkit.commands.meeting.note_cmd",
    "open": "fieldkit.commands.meeting.open_cmd",
}

_LazyGroup = make_lazy_group(_COMMANDS)


@click.group(
    name="meeting",
    cls=_LazyGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Pursuit Workbook — Google Docs integration for pursuit documentation."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
