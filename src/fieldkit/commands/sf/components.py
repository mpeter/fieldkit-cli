"""Inspect an opportunity's CPQ component lines."""

import json
from collections import defaultdict

import click
from rich.console import Console
from rich.table import Table

from fieldkit.cli_exit import cli_main
from fieldkit.config import get_sf_rest_base_url, get_sf_session_id
from fieldkit.errors import FieldkitError
from fieldkit.sf.client import SFAuthError, SFDirectClient, SFNotFoundError
from fieldkit.sf.components import BUCKET_ORDER, Bucket, ComponentLine, fetch_opp_component_lines, sort_component_lines
from fieldkit.sf.opportunities import resolve_opportunity_reference

_BUCKET_LABELS: dict[Bucket, str] = {
    "tam": "TAM",
    "learning": "Learning",
    "consulting": "Consulting",
    "product": "Product",
}


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _render(lines: list[ComponentLine]) -> None:
    console = Console()
    grouped: dict[Bucket, list[ComponentLine]] = defaultdict(list)
    for line in lines:
        grouped[line["bucket"]].append(line)
    for bucket in sorted(grouped, key=BUCKET_ORDER.__getitem__):
        console.print(f"[bold]{_BUCKET_LABELS[bucket]}[/bold]")
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        for label in ("Quote", "Line", "SKU", "Product family", "Net price", "Comp measure"):
            table.add_column(label, justify="right" if label == "Net price" else "left")
        for line in grouped[bucket]:
            table.add_row(
                line["quote_id"],
                line["line_id"] or "—",
                line["sku"] or "—",
                line["product_family"] or "—",
                _money(line["net_price"]),
                line["comp_measure"] or "—",
            )
        console.print(table)


@click.command(name="components")
@click.argument("opportunity")
@click.option("--json", "as_json", is_flag=True, help="Emit deterministic component records as JSON.")
def cli(opportunity: str, as_json: bool) -> None:
    """Show CPQ component lines for an Opportunity id or number."""
    with cli_main():
        session_id = get_sf_session_id()
        base_url = get_sf_rest_base_url()
        if not session_id or not base_url:
            raise SFAuthError("Salesforce authentication is not configured; run 'fieldkit auth sf'")
        with SFDirectClient(session_id=session_id, base_url=base_url) as client:
            try:
                opp_id = resolve_opportunity_reference(client, opportunity)
            except SFNotFoundError:
                raise FieldkitError(f"Opportunity {opportunity!r} was not found") from None
            if opp_id is None:
                raise FieldkitError(f"Opportunity {opportunity!r} was not found")
            lines = sort_component_lines(fetch_opp_component_lines(client, opp_id))
        if as_json:
            click.echo(json.dumps(lines, indent=2, sort_keys=True))
        elif not lines:
            click.echo("no component lines")
        else:
            _render(lines)
