"""Shared CPQ quote-line domain layer (opp → quote → line walk + bucketing).

Some Salesforce deployments block SOQL/SOSL for CPQ objects. fieldkit therefore
uses the UI-API ``related-list-records`` two-hop for opportunity quote lines:
``opp → SBQQ__Quotes2__r → SBQQ__LineItems__r`` via
:meth:`fieldkit.sf.client.SFDirectClient.fetch_related_list_records`.

This module is the *one home* (historic regression) for that walk, the ``_field_value``
UI-API extractor, the ``SBQQ__LineItems__r`` related-list constant, the
``FAMILY_BUCKETS`` bucketing table, and :func:`opp_contract_type` — the
contract-type detector that re-anchors ACV for fixed-price consulting
(historic regression / #1206). ``commands/sf/quote.py`` consumes the shared constant and
extractor from here; ``sf-component-services`` (#1392) later extends this
module with its ``sf components`` command and ``--services-only`` sweep.
"""

import logging
from typing import Any, Literal, TypedDict

from fieldkit.sf.client import SFAPIError, SFAuthError, SFDirectClient

logger = logging.getLogger(__name__)

# Related-list API names for the CPQ two-hop walk (compatible with deployments
# where standard child-query routes are unavailable).
OPP_QUOTES_RELATED_LIST = "SBQQ__Quotes2__r"
QUOTE_LINES_RELATED_LIST = "SBQQ__LineItems__r"

# Quote-line sObject API name, used to build qualified ``fields`` selectors for
# the related-list-records route (``ObjectApiName.FieldName``).
_LINE_OBJECT = "SBQQ__QuoteLine__c"

# Quote-line fields requested explicitly: the default related-list columns omit
# SBQQ__ProductFamily__c (which the bucketing + contract-type detection need).
_LINE_FIELDS = ",".join(
    f"{_LINE_OBJECT}.{f}"
    for f in (
        "SBQQ__ProductCode__c",
        "SBQQ__ProductFamily__c",
        "SBQQ__Quantity__c",
        "SBQQ__NetPrice__c",
        "SBQQ__NetTotal__c",
    )
)

# Candidate net-price keys (a line may carry NetTotal or, older lines, NetPrice).
_LINE_NET_KEYS = ("SBQQ__NetTotal__c", "SBQQ__NetPrice__c")

Bucket = Literal["tam", "learning", "consulting", "product"]
ContractType = Literal["fixed_price", "standard"]

# FAMILY_BUCKETS: ordered (prefix, bucket, comp_measure). Prefix match (not
# equality) so "TRAINING - PREPAID CREDITS" and "TRAINING - SUBSCRIPTIONS" both
# land in Learning without enumerating every variant. Verbatim from #1392
# design D1 so that change imports this table rather than forking it (historic regression).
FAMILY_BUCKETS: tuple[tuple[str, Bucket, str], ...] = (
    ("SUPPORT - TAM", "tam", "M000114"),
    ("TRAINING", "learning", "M000148"),
    ("CONSULTING", "consulting", "M000148"),
)
BUCKET_ORDER: dict[Bucket, int] = {"tam": 0, "learning": 1, "consulting": 2, "product": 3}

# The specific consulting family that marks an opp fixed-price. This is a finer
# split *inside* the consulting bucket (distinct from CONSULTING - TIME &
# MATERIALS, CONSULTING - PREPAID CREDITS, ...). opp_contract_type() tests it by
# case-insensitive, trimmed match (historic regression), not prefix — the single point to
# update on an org rename.
FIXED_PRICE_FAMILY = "CONSULTING - FIXED PRICE"


class ComponentLine(TypedDict):
    """A single CPQ quote line, normalized for services bucketing.

    Mirrors the ``sf/types.py`` TypedDict style. ``line_id``, ``sku``,
    ``product_family``, and ``net_price`` may be ``None`` when the UI-API
    related-list response omits the underlying field.
    """

    quote_id: str
    line_id: str | None
    sku: str | None
    product_family: str | None
    bucket: Bucket
    comp_measure: str
    quantity: float | None
    unit_price: float | None
    net_price: float | None


def sort_component_lines(lines: list[ComponentLine]) -> list[ComponentLine]:
    """Return component lines in deterministic bucket and source-field order."""

    def key(line: ComponentLine) -> tuple[object, ...]:
        return (
            BUCKET_ORDER[line["bucket"]],
            line["quote_id"],
            line["line_id"] is None,
            line["line_id"] or "",
            line["sku"] is None,
            line["sku"] or "",
            line["product_family"] is None,
            line["product_family"] or "",
            line["net_price"] is None,
            line["net_price"] or 0.0,
            line["quantity"] is None,
            line["quantity"] or 0.0,
            line["unit_price"] is None,
            line["unit_price"] or 0.0,
        )

    return sorted(lines, key=key)


def _field_value(fields: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first present ``fields[key]["value"]`` among *keys*, else None.

    UI API records nest each field as ``{"value": ..., "displayValue": ...}``.
    Falls back to ``displayValue`` when ``value`` is None but a display string
    exists (common for formula/derived fields).
    """
    for key in keys:
        entry = fields.get(key)
        if isinstance(entry, dict):
            if entry.get("value") is not None:
                return entry.get("value")
            if entry.get("displayValue") is not None:
                return entry.get("displayValue")
    return None


def _to_float(value: Any) -> float | None:
    """Coerce a raw UI-API numeric value to float, or None when absent/unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bucket_for_family(family: str | None) -> tuple[Bucket, str]:
    """Map a product family to its ``(bucket, comp_measure)`` via prefix match.

    Case-insensitive; a ``None`` family (missing column) and any family with no
    prefix match both resolve to ``("product", "")`` — product revenue, not a
    services measure.
    """
    if not family:
        return ("product", "")
    upper = family.upper()
    for prefix, bucket, comp_measure in FAMILY_BUCKETS:
        if upper.startswith(prefix.upper()):
            return (bucket, comp_measure)
    return ("product", "")


def fetch_opp_component_lines(client: SFDirectClient, opp_id: str) -> list[ComponentLine]:
    """Fetch every CPQ quote line for an opportunity via the two-hop walk.

    ``opp → SBQQ__Quotes2__r → SBQQ__LineItems__r``, requesting the line fields
    explicitly (default related-list columns omit ``SBQQ__ProductFamily__c``).
    *client* is an already-open :class:`SFDirectClient` (dependency-injected for
    testability, Principle IV).

    Raises:
        SFAuthError: propagated (never swallowed — a dead credential must reach
            ``cli_main()``).
        SFAPIError:  propagated (callers decide whether to degrade).
    """
    quotes = client.fetch_related_list_records(opp_id, OPP_QUOTES_RELATED_LIST)
    lines: list[ComponentLine] = []
    for quote in quotes:
        quote_id = quote.get("id")
        if not quote_id:
            continue
        raw_lines = client.fetch_related_list_records(str(quote_id), QUOTE_LINES_RELATED_LIST, _LINE_FIELDS)
        for rec in raw_lines:
            fields = rec.get("fields") or {}
            family_raw = _field_value(fields, ("SBQQ__ProductFamily__c",))
            family = str(family_raw) if family_raw is not None else None
            bucket, comp_measure = bucket_for_family(family)
            sku_raw = _field_value(fields, ("SBQQ__ProductCode__c",))
            line_id = rec.get("id")
            lines.append(
                ComponentLine(
                    quote_id=str(quote_id),
                    line_id=str(line_id) if line_id else None,
                    sku=str(sku_raw) if sku_raw is not None else None,
                    product_family=family,
                    bucket=bucket,
                    comp_measure=comp_measure,
                    quantity=_to_float(_field_value(fields, ("SBQQ__Quantity__c",))),
                    unit_price=_to_float(_field_value(fields, ("SBQQ__NetPrice__c",))),
                    net_price=_to_float(_field_value(fields, _LINE_NET_KEYS)),
                )
            )
    return lines


def _is_fixed_price_family(family: str | None) -> bool:
    return family is not None and family.strip().upper() == FIXED_PRICE_FAMILY.upper()


def opp_contract_type(client: SFDirectClient, opp_id: str) -> ContractType:
    """Derive contract type from an opportunity's quote-line families (historic regression).

    Returns ``"fixed_price"`` iff **any** quote line's ``SBQQ__ProductFamily__c``
    case-insensitively (and trimmed) equals :data:`FIXED_PRICE_FAMILY`; otherwise
    ``"standard"`` (T&M, prepaid credits, product-only, or no quote lines).

    A non-auth :class:`SFAPIError` during the walk degrades to ``"standard"``
    (D4 — fails safe toward today's behavior, not a worse number).
    :class:`SFAuthError` propagates — a dead credential must reach ``cli_main()``.
    """
    try:
        lines = fetch_opp_component_lines(client, opp_id)
    except SFAuthError:
        raise
    except SFAPIError as exc:
        logger.warning("opp_contract_type: quote-line walk failed for %s: %s — treating as standard", opp_id, exc)
        return "standard"
    for line in lines:
        if _is_fixed_price_family(line["product_family"]):
            return "fixed_price"
    return "standard"


def effective_net_consulting_acv(
    contract_type: str | None,
    gross_acv: float | None,
    net_acv: float | None,
    arr: float | None = None,
) -> float:
    """Select consulting ACV using the historic regression contract-type policy.

    Fixed-price opportunities prefer net ACV, then degrade to gross ACV before
    ARR. Standard and unknown contract types retain the gross-first order.
    Explicit zero values are preserved throughout.
    """
    values = (net_acv, gross_acv, arr) if contract_type == "fixed_price" else (gross_acv, net_acv, arr)
    selected = next((value for value in values if value is not None), 0.0)
    return float(selected)
