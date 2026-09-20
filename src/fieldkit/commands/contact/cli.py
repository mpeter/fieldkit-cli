"""fieldkit contact — Contact lookup, cache, enrichment, and reporting commands."""

import click

from fieldkit.commands._lazy import make_lazy_group

_COMMANDS: dict[str, str] = {
    "find": "fieldkit.commands.contact.find_cmd",
    "list": "fieldkit.commands.contact.list_cmd",
    "enrich": "fieldkit.commands.contact.enrich_cmd",
    "report": "fieldkit.commands.contact.report_cmd",
}

_LazyGroup = make_lazy_group(_COMMANDS)


@click.group(
    name="contact",
    cls=_LazyGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Contact lookup, cache, enrichment, and reporting."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
