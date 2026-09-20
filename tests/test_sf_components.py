"""Tests for fieldkit.sf.components — CPQ quote-line walk, bucketing, and the
contract-type detector that re-anchors fixed-price ACV (historic regression / #1206).

All Salesforce access is mocked at the SFDirectClient.fetch_related_list_records
seam (Principle IV — no live SF). These tests lock the *corrected* behavior, so
none are characterization tests.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from fieldkit.sf.client import SFAPIError, SFAuthError
from fieldkit.sf.components import (
    OPP_QUOTES_RELATED_LIST,
    QUOTE_LINES_RELATED_LIST,
    ComponentLine,
    bucket_for_family,
    effective_net_consulting_acv,
    fetch_opp_component_lines,
    opp_contract_type,
    sort_component_lines,
)

pytestmark = pytest.mark.unit

_OPP_ID = "006000000000000AAA"
_QUOTE_A = "a0Q000000000000AAA"
_QUOTE_B = "a0Q000000000001AAA"

_FIXED_PRICE = "CONSULTING - FIXED PRICE"
_GROSS = 200.0


# ── Fixture builders ─────────────────────────────────────────────────────────


def _line(
    *,
    family: str | None = None,
    sku: str | None = None,
    net: float | None = None,
    quantity: float | None = None,
    unit_price: float | None = None,
    rec_id: str = "a0R000000000000AAA",
    include_family_key: bool = True,
) -> dict[str, Any]:
    """Build a raw UI-API quote-line record."""
    fields: dict[str, Any] = {}
    if include_family_key:
        fields["SBQQ__ProductFamily__c"] = {"value": family, "displayValue": None}
    if sku is not None:
        fields["SBQQ__ProductCode__c"] = {"value": sku, "displayValue": None}
    if net is not None:
        fields["SBQQ__NetTotal__c"] = {"value": net, "displayValue": None}
    if quantity is not None:
        fields["SBQQ__Quantity__c"] = {"value": quantity, "displayValue": None}
    if unit_price is not None:
        fields["SBQQ__NetPrice__c"] = {"value": unit_price, "displayValue": None}
    return {"id": rec_id, "fields": fields}


def _client(
    *,
    quotes: list[dict[str, Any]] | None = None,
    lines_by_quote: dict[str, list[dict[str, Any]]] | None = None,
) -> MagicMock:
    """A MagicMock SFDirectClient whose related-list route serves the two-hop walk."""
    quotes = quotes if quotes is not None else []
    lines_by_quote = lines_by_quote or {}

    def _frl(parent_id: str, related_list_id: str, fields: str | None = None) -> list[dict[str, Any]]:
        if related_list_id == OPP_QUOTES_RELATED_LIST:
            return quotes
        if related_list_id == QUOTE_LINES_RELATED_LIST:
            return lines_by_quote.get(parent_id, [])
        return []

    m = MagicMock()
    m.fetch_related_list_records.side_effect = _frl
    return m


# ── opp_contract_type ─────────────────────────────────────────────────────────


def test_opp_contract_type_fixed_price_line() -> None:
    client = _client(quotes=[{"id": _QUOTE_A}], lines_by_quote={_QUOTE_A: [_line(family=_FIXED_PRICE, net=250000.0)]})
    result = opp_contract_type(client, _OPP_ID)
    assert result == "fixed_price"


@pytest.mark.parametrize(
    "family",
    [
        "consulting - fixed price",
        "Consulting - Fixed Price",
        "CONSULTING - FIXED PRICE ",
        " CONSULTING - FIXED PRICE",
    ],
)
def test_opp_contract_type_fixed_price_case_and_whitespace_tolerant(family: str) -> None:
    """historic regression: family casing and whitespace variance remains fixed-price."""
    client = _client(quotes=[{"id": _QUOTE_A}], lines_by_quote={_QUOTE_A: [_line(family=family, net=250000.0)]})
    result = opp_contract_type(client, _OPP_ID)
    assert result == "fixed_price"


@pytest.mark.parametrize(
    "family",
    [
        "CONSULTING - TIME & MATERIALS",
        "CONSULTING - PREPAID CREDITS",
        "OpenShift Platform Plus",
    ],
)
def test_opp_contract_type_non_fixed_price_is_standard(family: str) -> None:
    client = _client(quotes=[{"id": _QUOTE_A}], lines_by_quote={_QUOTE_A: [_line(family=family, net=100000.0)]})
    result = opp_contract_type(client, _OPP_ID)
    assert result == "standard"


def test_opp_contract_type_no_quote_lines_is_standard() -> None:
    client = _client(quotes=[{"id": _QUOTE_A}], lines_by_quote={_QUOTE_A: []})
    result = opp_contract_type(client, _OPP_ID)
    assert result == "standard"


def test_opp_contract_type_no_quotes_is_standard() -> None:
    client = _client(quotes=[])
    result = opp_contract_type(client, _OPP_ID)
    assert result == "standard"


def test_opp_contract_type_multi_quote_fixed_price_in_second_quote() -> None:
    """A fixed-price line anywhere across multiple quotes marks the opp fixed_price."""
    client = _client(
        quotes=[{"id": _QUOTE_A}, {"id": _QUOTE_B}],
        lines_by_quote={
            _QUOTE_A: [_line(family="CONSULTING - TIME & MATERIALS", net=50000.0)],
            _QUOTE_B: [_line(family=_FIXED_PRICE, net=250000.0, rec_id="a0R000000000009AAA")],
        },
    )
    result = opp_contract_type(client, _OPP_ID)
    assert result == "fixed_price"


def test_opp_contract_type_missing_family_field_is_standard() -> None:
    """A line whose related-list response omits SBQQ__ProductFamily__c doesn't crash."""
    client = _client(
        quotes=[{"id": _QUOTE_A}],
        lines_by_quote={_QUOTE_A: [_line(include_family_key=False, net=100000.0)]},
    )
    result = opp_contract_type(client, _OPP_ID)
    assert result == "standard"


def test_opp_contract_type_non_auth_error_degrades_to_standard() -> None:
    """A non-auth SFAPIError during the walk degrades to standard (D4), not a crash."""
    client = MagicMock()
    client.fetch_related_list_records.side_effect = SFAPIError("boom")
    result = opp_contract_type(client, _OPP_ID)
    assert result == "standard"


def test_opp_contract_type_auth_error_propagates() -> None:
    """SFAuthError must propagate — a dead credential can't be swallowed to standard."""
    client = MagicMock()
    client.fetch_related_list_records.side_effect = SFAuthError("expired")
    with pytest.raises(SFAuthError, match=r"expired"):
        opp_contract_type(client, _OPP_ID)


# ── bucket_for_family ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        ("SUPPORT - TAM", ("tam", "M000114")),
        ("SUPPORT - TAM - PREMIUM", ("tam", "M000114")),  # prefix match
        ("TRAINING", ("learning", "M000148")),
        ("TRAINING - PREPAID CREDITS", ("learning", "M000148")),  # prefix match
        ("CONSULTING", ("consulting", "M000148")),
        ("CONSULTING - FIXED PRICE", ("consulting", "M000148")),  # FP is inside consulting
        ("consulting - time & materials", ("consulting", "M000148")),  # case-insensitive
        ("OpenShift Platform Plus", ("product", "")),  # no prefix match
        ("", ("product", "")),
        (None, ("product", "")),
    ],
)
def test_bucket_for_family(family: str | None, expected: tuple[str, str]) -> None:
    result = bucket_for_family(family)
    assert result == expected


def test_sort_component_lines_is_stable_across_reversed_input_and_nulls() -> None:
    lines: list[ComponentLine] = [
        {
            "quote_id": _QUOTE_B,
            "line_id": None,
            "sku": None,
            "product_family": None,
            "bucket": "product",
            "comp_measure": "",
            "quantity": None,
            "unit_price": None,
            "net_price": None,
        },
        {
            "quote_id": _QUOTE_A,
            "line_id": "a0R000000000001AAA",
            "sku": "TAM-1",
            "product_family": "SUPPORT - TAM",
            "bucket": "tam",
            "comp_measure": "M000114",
            "quantity": 1.0,
            "unit_price": 10.0,
            "net_price": 10.0,
        },
    ]
    forward = sort_component_lines(lines)
    reverse = sort_component_lines(list(reversed(lines)))
    assert forward == reverse
    assert [line["bucket"] for line in forward] == ["tam", "product"]


# ── fetch_opp_component_lines ─────────────────────────────────────────────────


def test_fetch_opp_component_lines_multi_quote() -> None:
    client = _client(
        quotes=[{"id": _QUOTE_A}, {"id": _QUOTE_B}],
        lines_by_quote={
            _QUOTE_A: [
                _line(
                    family="CONSULTING",
                    sku="CONS-001",
                    net=100000.0,
                    quantity=10.0,
                    unit_price=10000.0,
                    rec_id="a0R000000000001AAA",
                )
            ],
            _QUOTE_B: [_line(family=_FIXED_PRICE, sku="FP-001", net=250000.0, rec_id="a0R000000000002AAA")],
        },
    )
    result = fetch_opp_component_lines(client, _OPP_ID)
    assert len(result) == 2
    assert result[0]["quote_id"] == _QUOTE_A
    assert result[0]["sku"] == "CONS-001"
    assert result[0]["product_family"] == "CONSULTING"
    assert result[0]["bucket"] == "consulting"
    assert result[0]["comp_measure"] == "M000148"
    assert result[0]["quantity"] == 10.0
    assert result[0]["unit_price"] == 10000.0
    assert result[0]["net_price"] == 100000.0
    assert result[1]["quote_id"] == _QUOTE_B
    assert result[1]["product_family"] == _FIXED_PRICE


def test_fetch_opp_component_lines_requests_family_field() -> None:
    """The quote-line fetch must request SBQQ__ProductFamily__c explicitly (default
    related-list columns omit it)."""
    client = _client(quotes=[{"id": _QUOTE_A}], lines_by_quote={_QUOTE_A: [_line(family="CONSULTING", net=1.0)]})
    result = fetch_opp_component_lines(client, _OPP_ID)
    assert len(result) == 1
    line_call = next(
        c for c in client.fetch_related_list_records.call_args_list if c.args[1] == QUOTE_LINES_RELATED_LIST
    )
    assert "SBQQ__ProductFamily__c" in line_call.args[2]
    assert "SBQQ__Quantity__c" in line_call.args[2]
    assert "SBQQ__NetPrice__c" in line_call.args[2]


def test_fetch_opp_component_lines_missing_family_defaults_product() -> None:
    client = _client(
        quotes=[{"id": _QUOTE_A}],
        lines_by_quote={_QUOTE_A: [_line(include_family_key=False, sku="X", net=5.0)]},
    )
    result = fetch_opp_component_lines(client, _OPP_ID)
    assert result[0]["product_family"] is None
    assert result[0]["bucket"] == "product"
    assert result[0]["comp_measure"] == ""


def test_fetch_opp_component_lines_skips_quote_without_id() -> None:
    client = _client(
        quotes=[{"id": None}, {"id": _QUOTE_A}], lines_by_quote={_QUOTE_A: [_line(family="TRAINING", net=1.0)]}
    )
    result = fetch_opp_component_lines(client, _OPP_ID)
    assert len(result) == 1
    assert result[0]["bucket"] == "learning"


@pytest.mark.parametrize(
    "contract_type,gross_acv,net_acv,arr,expected",
    [
        ("fixed_price", 200.0, 100.0, 90.0, 100.0),
        ("fixed_price", 200.0, None, 90.0, 200.0),
        ("fixed_price", None, None, 90.0, 90.0),
        ("fixed_price", None, None, None, 0.0),
        ("standard", 150.0, 100.0, 90.0, 150.0),
        ("standard", None, 100.0, 90.0, 100.0),
        ("standard", None, None, 90.0, 90.0),
        (None, 150.0, 100.0, None, 150.0),
        ("standard", 0.0, 100.0, None, 0.0),
    ],
)
def test_effective_net_consulting_acv(
    contract_type: str | None,
    gross_acv: float | None,
    net_acv: float | None,
    arr: float | None,
    expected: float,
) -> None:
    result = effective_net_consulting_acv(contract_type, gross_acv, net_acv, arr)
    assert result == pytest.approx(expected)


def test_effective_net_consulting_acv_fixed_price_acv_none_falls_back_to_gross() -> None:
    """historic regression: missing net ACV degrades to gross before ARR, never zero."""
    result = effective_net_consulting_acv("fixed_price", _GROSS, None, 750_000.0)
    assert result == pytest.approx(_GROSS)


def test_effective_net_consulting_acv_fixed_price_preserves_zero_net() -> None:
    result = effective_net_consulting_acv("fixed_price", _GROSS, 0.0, 750_000.0)
    assert result == 0.0
