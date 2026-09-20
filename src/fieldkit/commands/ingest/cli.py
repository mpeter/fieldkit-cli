"""fieldkit ingest CLI group — source provenance and idempotent ingestion pipelines."""

import click

from fieldkit.commands._lazy import LazyCommand, make_lazy_group

_COMMANDS: dict[str, LazyCommand] = {
    "backfill": LazyCommand("fieldkit.commands.ingest.backfill", description="Find notes missing provenance"),
    "discover": LazyCommand("fieldkit.commands.ingest.discover", description="Discover new pipeline sources"),
    "promote": LazyCommand("fieldkit.commands.ingest.promote", description="Promote meeting action items"),
    "reprocess": LazyCommand("fieldkit.commands.ingest.reprocess", description="Re-run stored artifacts"),
    "route": LazyCommand("fieldkit.commands.ingest.route", description="Route meetings to accounts"),
    "run": LazyCommand("fieldkit.commands.ingest.run", description="Execute an ingestion pipeline"),
    "status": LazyCommand("fieldkit.commands.ingest.status", description="Show pipeline status"),
}

_LazyGroup = make_lazy_group(_COMMANDS, command_prefix="ingest")


@click.group(
    name="ingest",
    cls=_LazyGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Source provenance and idempotent ingestion pipelines."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(1)
