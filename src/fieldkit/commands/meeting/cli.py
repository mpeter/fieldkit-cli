"""fieldkit meeting — Pursuit Workbook (Google Docs) commands."""

import click

from fieldkit.commands._lazy import LazyCommand, make_lazy_group
from fieldkit.config.optional_dependencies import GOOGLE_IMPORT_ROOTS

_COMMANDS: dict[str, LazyCommand] = {
    "link": LazyCommand(
        "fieldkit.commands.meeting.link_cmd",
        profile="google",
        import_roots=GOOGLE_IMPORT_ROOTS,
        description="Create and link a Google pursuit workbook",
    ),
    "list": LazyCommand("fieldkit.commands.meeting.list_cmd", description="List locally recorded workbook links"),
    "note": LazyCommand(
        "fieldkit.commands.meeting.note_cmd",
        profile="google",
        import_roots=GOOGLE_IMPORT_ROOTS,
        description="Add a note to a Google pursuit workbook",
    ),
    "open": LazyCommand(
        "fieldkit.commands.meeting.open_cmd",
        profile="google",
        import_roots=GOOGLE_IMPORT_ROOTS,
        description="Open a linked Google pursuit workbook",
    ),
}

_LazyGroup = make_lazy_group(_COMMANDS, command_prefix="meeting")


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
