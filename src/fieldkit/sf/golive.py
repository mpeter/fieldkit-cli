"""Build deterministic, provenance-carrying go-live revenue rows."""

from typing import TypedDict

from fieldkit.errors import FieldkitError
from fieldkit.sf.components import BUCKET_ORDER, Bucket, ComponentLine


class GoliveLine(TypedDict):
    """One sourced Salesforce quote line in the go-live revenue block."""

    quote_id: str
    sku: str
    product_family: str
    bucket: Bucket
    units: float | None
    unit_price: float | None
    annual_value: float | None
    source: str


def assemble_revenue_block(lines: list[ComponentLine]) -> list[GoliveLine]:
    """Normalize and stably sort Salesforce component lines for reporting."""
    result: list[GoliveLine] = []
    for line in lines:
        line_id = line["line_id"]
        if line_id is None:
            raise FieldkitError("quote line id is required for provenance")
        result.append(
            GoliveLine(
                quote_id=line["quote_id"],
                sku=line["sku"] or "",
                product_family=line["product_family"] or "",
                bucket=line["bucket"],
                units=line["quantity"],
                unit_price=line["unit_price"],
                annual_value=line["net_price"],
                source=f"sf:quote-line:{line_id}",
            )
        )
    return sorted(result, key=lambda row: (BUCKET_ORDER[row["bucket"]], row["sku"], row["quote_id"], row["source"]))
