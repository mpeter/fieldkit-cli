"""fieldkit sf quote — Read a CPQ Quote and its quote lines.

Some Salesforce deployments block SOQL/SOSL for CPQ objects. The Quote header
is read via a normal sObject record fetch, and quote lines use the UI API
``related-list-records`` route (relatedListId ``SBQQ__LineItems__r``), which is
compatible with deployments where standard child-query routes are unavailable.

Usage:
    fieldkit sf quote <quote_id> [--json]

Reads the Quote header (number, status, net amount, discount) and each quote
line (product, quantity, list/net totals, discount). Prints a human-readable
summary by default; ``--json`` emits a structured payload.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL
from fieldkit.config import get_sf_rest_base_url, get_sf_session_id
from fieldkit.sf.components import QUOTE_LINES_RELATED_LIST, _field_value

LOG_PREFIX = "[sf-quote]"

# CPQ Quote sObject. The line-items related list constant + the UI-API
# _field_value extractor now live in fieldkit.sf.components (shared, one home —
# historic regression / R25); this module imports them rather than keeping local copies.
_QUOTE_SOBJECT = "SBQQ__Quote__c"

# Matches valid 15- or 18-character Salesforce IDs (alphanumeric only).
# Rejects placeholder strings like "TBD", "NEEDS-LOOKUP", etc.
_SF_ID_RE = re.compile(r"^[A-Za-z0-9]{15}$|^[A-Za-z0-9]{18}$")

# Quote header fields to request from the sObject REST API.
_QUOTE_FIELDS = ",".join(
    [
        "Id",
        "Name",
        "SBQQ__Status__c",
        "SBQQ__NetAmount__c",
        "SBQQ__ListAmount__c",
        "SBQQ__CustomerAmount__c",
        "SBQQ__AverageCustomerDiscount__c",
        "SBQQ__Opportunity2__c",
    ]
)

# Candidate quote-line field API names, in priority order per column. The
# related-list-records response only carries the related list's configured
# columns, so each column tolerates absence (renders "-").
_LINE_PRODUCT_KEYS = ("SBQQ__ProductName__c",)
_LINE_QUANTITY_KEYS = ("SBQQ__Quantity__c",)
_LINE_LIST_TOTAL_KEYS = ("SBQQ__ListTotal__c",)
_LINE_NET_TOTAL_KEYS = ("SBQQ__NetTotal__c", "SBQQ__NetPrice__c")
_LINE_DISCOUNT_KEYS = ("SBQQ__Discount__c",)


def _log(msg: str) -> None:
    click.echo(f"{LOG_PREFIX} {msg}", err=True)


def _fmt_currency(val: Any) -> str:
    """Format a numeric value as a dollar string, or '-' when absent."""
    if val is None:
        return "-"
    try:
        num = float(val)
    except (TypeError, ValueError):
        return str(val)
    if num == 0.0:
        return "$0"
    return f"${num:,.0f}"


def _parse_line(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw UI-API quote-line record into a flat display dict."""
    fields = record.get("fields") or {}
    return {
        "id": record.get("id"),
        "product": _field_value(fields, _LINE_PRODUCT_KEYS),
        "quantity": _field_value(fields, _LINE_QUANTITY_KEYS),
        "list_total": _field_value(fields, _LINE_LIST_TOTAL_KEYS),
        "net_total": _field_value(fields, _LINE_NET_TOTAL_KEYS),
        "discount": _field_value(fields, _LINE_DISCOUNT_KEYS),
    }


def _fetch_quote(quote_id: str) -> dict[str, Any]:
    """Fetch the CPQ Quote header via the sObject REST API.

    Returns the parsed record dict. Raises on auth/API errors; exits 2 when no
    session/base URL is configured, and exits 3 when the Quote is not found.
    """
    import fieldkit.sf.client as _sf_direct

    sid = get_sf_session_id()
    if not sid:
        _log("ERROR: No Salesforce session. Run: fieldkit auth sf")
        raise SystemExit(EXIT_AUTH)

    base_url = get_sf_rest_base_url()
    if not base_url:
        _log("ERROR: No Salesforce base URL configured.")
        raise SystemExit(EXIT_AUTH)

    _log(f"Fetching quote {quote_id} from Salesforce...")
    with _sf_direct.SFDirectClient(session_id=sid, base_url=base_url) as client:
        try:
            return client.fetch_sobject(_QUOTE_SOBJECT, quote_id, _QUOTE_FIELDS)
        except _sf_direct.SFAuthError:
            _log("ERROR: Auth failure. Run: fieldkit auth sf")
            raise
        except _sf_direct.SFNotFoundError:
            _log(f"ERROR: Quote {quote_id} not found in Salesforce.")
            raise SystemExit(EXIT_DATA) from None
        except _sf_direct.SFAPIError as exc:
            _log(f"ERROR: {exc}")
            raise SystemExit(EXIT_PARTIAL) from None


def _fetch_quote_lines(quote_id: str) -> list[dict[str, Any]]:
    """Fetch quote lines via the UI API related-list-records route.

    Returns a list of normalized line dicts. Returns ``[]`` on any client
    error (auth/API), matching the graceful-degradation pattern used by
    ``_fetch_deal_splits`` — the header is still useful without lines.
    """
    import logging

    import fieldkit.sf.client as _sf_direct

    sid = get_sf_session_id()
    if not sid:
        return []

    base_url = get_sf_rest_base_url()
    if not base_url:
        return []

    with _sf_direct.SFDirectClient(session_id=sid, base_url=base_url) as client:
        try:
            raw = client.fetch_related_list_records(quote_id, QUOTE_LINES_RELATED_LIST)
        except _sf_direct.SFAuthError:
            return []
        except _sf_direct.SFAPIError as exc:
            logging.getLogger(__name__).warning("%s Quote lines unavailable: %s", LOG_PREFIX, exc)
            return []
    return [_parse_line(rec) for rec in raw]


def _build_payload(rec: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the machine-readable payload for a quote and its lines."""
    return {
        "status": "ok",
        "quote_id": rec.get("Id"),
        "quote_number": rec.get("Name"),
        "quote_status": rec.get("SBQQ__Status__c"),
        "net_amount": rec.get("SBQQ__NetAmount__c"),
        "list_amount": rec.get("SBQQ__ListAmount__c"),
        "customer_amount": rec.get("SBQQ__CustomerAmount__c"),
        "average_discount": rec.get("SBQQ__AverageCustomerDiscount__c"),
        "opportunity_id": rec.get("SBQQ__Opportunity2__c"),
        "line_count": len(lines),
        "lines": lines,
        "pulled_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _print_summary(rec: dict[str, Any], lines: list[dict[str, Any]]) -> None:
    """Print a human-readable summary of the quote and its lines."""
    click.echo(f"\n{'=' * 60}")
    click.echo(f"  Quote {rec.get('Name', 'Unknown')}")
    click.echo(f"{'=' * 60}")
    click.echo(f"  ID:             {rec.get('Id')}")
    click.echo(f"  Status:         {rec.get('SBQQ__Status__c') or '(not set)'}")
    click.echo(f"  Net Amount:     {_fmt_currency(rec.get('SBQQ__NetAmount__c'))}")
    click.echo(f"  List Amount:    {_fmt_currency(rec.get('SBQQ__ListAmount__c'))}")
    click.echo(f"  Customer Amt:   {_fmt_currency(rec.get('SBQQ__CustomerAmount__c'))}")
    avg_disc = rec.get("SBQQ__AverageCustomerDiscount__c")
    click.echo(f"  Avg Discount:   {avg_disc if avg_disc is not None else '(not set)'}")
    click.echo()

    click.echo(f"  ── Quote Lines ({len(lines)}) ──")
    if not lines:
        click.echo("  (no quote lines found)")
        click.echo()
        return

    for line in lines:
        product = line.get("product") or "(unnamed product)"
        qty = line.get("quantity")
        qty_str = f"{qty:g}" if isinstance(qty, (int, float)) else (str(qty) if qty is not None else "-")
        click.echo(
            f"  {product[:40]:<40} "
            f"qty {qty_str:>6} | "
            f"list {_fmt_currency(line.get('list_total')):>12} | "
            f"net {_fmt_currency(line.get('net_total')):>12} | "
            f"disc {line.get('discount') if line.get('discount') is not None else '-'}"
        )
    click.echo()


def run_quote(quote_id: str) -> int:
    """Core logic: fetch a quote + lines and print the summary. Returns exit code."""
    import logging

    if not _SF_ID_RE.fullmatch(quote_id):
        logging.warning(
            "quote id %r is not a valid Salesforce ID (expected 15 or 18 alphanumeric chars) — skipping",
            quote_id,
        )
        return 3

    rec = _fetch_quote(quote_id)
    lines = _fetch_quote_lines(quote_id)
    _print_summary(rec, lines)
    return 0


@click.command(
    name="quote",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.argument("quote_id")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output (suppresses human summary).",
)
def cli(quote_id: str, as_json: bool) -> None:
    """Read a CPQ Quote and its quote lines.

    QUOTE_ID is the 15- or 18-character Salesforce SBQQ__Quote__c record ID.

    Reads the Quote header (number, status, net/list/customer amounts, average
    discount) via a record fetch, and the quote lines via the UI API
    related-list-records route, which remains compatible with deployments where
    CPQ objects cannot be queried through SOQL or SOSL.
    """
    if not _SF_ID_RE.fullmatch(quote_id):
        _log(f"ERROR: Invalid quote ID: {quote_id!r} (expected 15 or 18 alphanumeric chars).")
        raise SystemExit(EXIT_DATA)

    if as_json:
        rec = _fetch_quote(quote_id)
        lines = _fetch_quote_lines(quote_id)
        click.echo(json.dumps(_build_payload(rec, lines), indent=2, default=str))
        return

    raise SystemExit(run_quote(quote_id))
