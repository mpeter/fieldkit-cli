"""fieldkit commands — machine-readable CLI registry (D4).

Usage:
    fieldkit commands            human-readable table of every leaf command
    fieldkit commands --json     full registry as JSON (the agent-facing form)

See `openspec/changes/cli-ux-redesign/design.md` D4 and
`src/fieldkit/cli_registry.py` for the introspection mechanism.
"""

import json

import click
from rich.console import Console
from rich.table import Table

from fieldkit.cli_registry import build_registry


@click.command(name="commands")
@click.option("--json", "output_json", is_flag=True, help="Emit the full registry as JSON.")
def cli(output_json: bool) -> None:
    """List every leaf command with its summary, write class, and account scope.

    The registry agents route on: full command name, argument/flag surface,
    write classification (read-only / workspace / external), and whether
    `--account` scoping is supported. Use `--json` for the machine-readable
    form; the default table is for humans.

    The Account column reads `filter` (optional, narrows a sweep across all
    accounts) or `selector` (names the single account acted on — omitting it
    does not widen the command). Blank means the command has no `--account`.

    `write_class` carries its provenance in `write_class_source`. "declared"
    means the command states what it writes via `@declare_write` and is safe
    to route on; "inferred" means it was guessed from flag presence, which
    describes the flags offered rather than what the command touches. The
    table marks inferred rows with a trailing `?`.
    """
    entries = build_registry()

    if output_json:
        click.echo(json.dumps([e.to_dict() for e in entries], indent=2))
        return

    console = Console()
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Command", min_width=30)
    table.add_column("Summary", min_width=40)
    table.add_column("Write class", min_width=10)
    table.add_column("Account", justify="center", min_width=9)
    for e in entries:
        # Trailing "?" marks a class inferred from flags rather than declared —
        # the difference between a stated fact and a guess, visible at a glance.
        write_class = e.write_class if e.write_class_source == "declared" else f"{e.write_class}?"
        # "filter" narrows a sweep; "selector" names the target. Printing "yes"
        # for both is what let a required --account read as an optional filter.
        table.add_row(f"fieldkit {e.full_name}", e.summary, write_class, e.account_scope or "")
    console.print(table)
