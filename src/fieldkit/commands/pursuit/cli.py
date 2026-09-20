"""fieldkit pursuit — Pursuit file management commands."""

import click

from fieldkit.commands._lazy import make_lazy_group

_COMMANDS: dict[str, str] = {
    "advance": "fieldkit.commands.pursuit.advance_cmd",
    "archive": "fieldkit.commands.pursuit.archive_cmd",
    "audit": "fieldkit.commands.pursuit.audit_cmd",
    "create": "fieldkit.commands.pursuit.create_cmd",
    "forecast": "fieldkit.commands.pursuit.forecast",
    "health": "fieldkit.commands.pursuit.pipeline_health",
    "projects": "fieldkit.commands.pursuit.projects_health",
    "rename": "fieldkit.commands.pursuit.rename_cmd",
    "repair-dates": "fieldkit.commands.pursuit.repair_dates_cmd",
}

# implementation change: shared lazy-loader; removed copy-pasted _LazyGroup definition.
_LazyGroup = make_lazy_group(_COMMANDS)


@click.group(
    name="pursuit",
    cls=_LazyGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Pursuit file management — create, audit, advance, archive, rename, and forecast pursuits."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
