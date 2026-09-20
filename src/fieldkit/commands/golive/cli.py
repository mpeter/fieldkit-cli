"""Read-only deterministic revenue sourcing for go-live reports."""

import json
from collections import defaultdict

import click
from rich.console import Console
from rich.table import Table

from fieldkit.cli_exit import cli_main
from fieldkit.config import get_sf_rest_base_url, get_sf_session_id
from fieldkit.errors import FieldkitError
from fieldkit.sf.client import SFAuthError, SFDirectClient, SFNotFoundError
from fieldkit.sf.components import fetch_opp_component_lines
from fieldkit.sf.golive import GoliveLine, assemble_revenue_block
from fieldkit.sf.opportunities import resolve_opportunity_reference

_BUCKET_LABELS = {"tam": "TAM", "learning": "Learning", "consulting": "Consulting", "product": "Product"}


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _render_rows(rows: list[GoliveLine]) -> None:
    console = Console()
    by_quote: dict[str, list[GoliveLine]] = defaultdict(list)
    for row in rows:
        by_quote[row["quote_id"]].append(row)

    overall_values: list[float] = []
    for quote_id in sorted(by_quote):
        quote_rows = by_quote[quote_id]
        console.print(f"[bold]Quote {quote_id}[/bold]")
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Bucket")
        table.add_column("SKU")
        table.add_column("Product family")
        table.add_column("Units", justify="right")
        table.add_column("Unit price", justify="right")
        table.add_column("Annual value", justify="right")
        table.add_column("Source")

        represented_buckets = {row["bucket"] for row in quote_rows}
        bucket_totals: dict[str, list[float]] = defaultdict(list)
        for row in quote_rows:
            annual_value = row["annual_value"]
            if annual_value is not None:
                bucket_totals[row["bucket"]].append(annual_value)
                overall_values.append(annual_value)
            table.add_row(
                _BUCKET_LABELS[row["bucket"]],
                row["sku"] or "—",
                row["product_family"] or "—",
                _number(row["units"]),
                _money(row["unit_price"]),
                _money(annual_value),
                row["source"],
            )
        for bucket in ("tam", "learning", "consulting", "product"):
            if bucket in represented_buckets:
                values = bucket_totals[bucket]
                subtotal = sum(values) if values else None
                table.add_row(f"{_BUCKET_LABELS[bucket]} total", "", "", "", "", _money(subtotal), "")
        console.print(table)
    grand_total = sum(overall_values) if overall_values else None
    console.print(f"[bold]Grand total: {_money(grand_total)}[/bold]")


@click.command(name="golive", context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.argument("opportunity")
@click.option("--json", "as_json", is_flag=True, help="Emit deterministic sourced rows as JSON.")
def cli(opportunity: str, as_json: bool) -> None:
    """Source a go-live revenue block from an opportunity's CPQ quote lines.

    OPPORTUNITY is a 15/18-character Salesforce id or a 5-12 digit
    Opportunity Number.
    """
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
            rows = assemble_revenue_block(fetch_opp_component_lines(client, opp_id))

        if as_json:
            click.echo(json.dumps(rows, indent=2, sort_keys=True))
        elif not rows:
            click.echo("no quote lines — nothing to source")
        else:
            _render_rows(rows)
