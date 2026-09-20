"""fieldkit gmail — Gmail cache pipeline Click group."""

import click

from fieldkit.commands._lazy import LazyCommand, make_lazy_group
from fieldkit.config.optional_dependencies import GOOGLE_IMPORT_ROOTS

# Map command name → dotted module path (loaded on first use)
_COMMANDS: dict[str, LazyCommand] = {
    "account-tags": LazyCommand("fieldkit.commands.gmail.account_tags", description="List Gmail account tags"),
    "backstory-gap": LazyCommand("fieldkit.commands.gmail.backstory_gap", description="Find account context gaps"),
    "decay": LazyCommand("fieldkit.commands.gmail.decay", description="Report stale Gmail relationships"),
    "enrich-pursuits": LazyCommand(
        "fieldkit.commands.gmail.enrich_pursuits", description="Enrich pursuits from the local Gmail cache"
    ),
    "query": LazyCommand("fieldkit.commands.gmail.query", description="Query the local Gmail cache"),
    "sync": LazyCommand(
        "fieldkit.commands.gmail.sync_command",
        profile="google",
        import_roots=GOOGLE_IMPORT_ROOTS,
        description="Synchronize Gmail through Google OAuth",
    ),
}

# implementation change: shared lazy-loader; removed copy-pasted _LazyGroup definition.
# logging.basicConfig() removed from group callback — implementation change: it was running
# even on --help. Moved to main() in fieldkit/__main__.py.
_LazyGroup = make_lazy_group(_COMMANDS, command_prefix="gmail")


@click.group(
    name="gmail",
    cls=_LazyGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Gmail cache pipeline — sync, query, and analyse Gmail data."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
