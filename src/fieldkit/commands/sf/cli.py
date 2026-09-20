"""fieldkit sf — Salesforce pipeline Click group."""

import click

from fieldkit.commands._lazy import make_lazy_group

# Map command name → dotted module path (loaded on first use)
_COMMANDS: dict[str, str] = {
    "account": "fieldkit.commands.sf.account",
    "components": "fieldkit.commands.sf.components",
    "frontmatter": "fieldkit.commands.sf.frontmatter",
    "listview": "fieldkit.commands.sf.listview",
    "meddpicc": "fieldkit.commands.sf.meddpicc",
    "opportunity": "fieldkit.commands.sf.opportunity",
    "quote": "fieldkit.commands.sf.quote",
    "reconcile": "fieldkit.commands.sf.reconcile",
    "schema": "fieldkit.commands.sf.schema",
    "session-check": "fieldkit.commands.sf.session_check",
    "set-field": "fieldkit.commands.sf.set_field",
    "set-next-steps": "fieldkit.commands.sf.set_next_steps",
    "update-closeplan": "fieldkit.commands.sf.update_closeplan",
}

# implementation change: shared lazy-loader; removed copy-pasted _LazyGroup definition.
_LazyGroup = make_lazy_group(_COMMANDS)


@click.group(
    name="sf",
    cls=_LazyGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Salesforce pipeline — listview, opportunity, account, quote, meddpicc, frontmatter, reconcile, field writes."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
