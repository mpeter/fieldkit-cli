"""Tests for conservative Gmail address-quality classification."""

import pytest

from fieldkit.gmail.address_quality import (
    SUSPECTED_MASKED_REASON,
    account_domains,
    is_suspected_masked_address,
    partition_suspected_masked,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("email", "domains", "expected"),
    [
        ("alex.taylor.q7zm@acme-corp.com", ["acme-corp.com"], True),
        ("alex.taylor.abcd@acme-corp.com", ["ACME-CORP.COM"], True),
        ("Alex.Taylor.q7zm@acme-corp.com", ["acme-corp.com"], True),
        ("alex.taylor.Q7ZM@acme-corp.com", ["acme-corp.com"], False),
        ("alex.taylor.2026@acme-corp.com", ["acme-corp.com"], False),
        ("alex.taylor.q7zm@example.net", ["acme-corp.com"], False),
        ("alex.q7zm@acme-corp.com", ["acme-corp.com"], False),
        ("alex.taylor.q7zm@acme-corp.com", [], False),
        ("team..q7zm@acme-corp.com", ["acme-corp.com"], False),
    ],
)
def test_is_suspected_masked_address(email: str, domains: list[str], expected: bool) -> None:
    result = is_suspected_masked_address(email, domains)
    assert result is expected


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"accounts": {"acme-corp": {"domains": ["Acme-Corp.com", ""]}}}, ("acme-corp.com",)),
        ({"accounts": {"acme-corp": {"domains": "acme-corp.com"}}}, ()),
        ({"accounts": []}, ()),
        ({}, ()),
    ],
)
def test_account_domains_ignores_malformed_configuration(config: dict[str, object], expected: tuple[str, ...]) -> None:
    result = account_domains(config, "acme-corp")
    assert result == expected


def test_partition_suspected_masked_preserves_order() -> None:
    ordinary_record = ("alex.taylor@acme-corp.com", "Alex Taylor", 4, 200)
    suspected_record = ("casey.morgan.q7zm@acme-corp.com", "Casey Morgan", 3, 300)

    ordinary, suspected = partition_suspected_masked(
        [suspected_record, ordinary_record],
        ["acme-corp.com"],
    )

    assert ordinary == [ordinary_record]
    assert suspected == [suspected_record]
    assert SUSPECTED_MASKED_REASON == "account-domain-four-character-suffix"
