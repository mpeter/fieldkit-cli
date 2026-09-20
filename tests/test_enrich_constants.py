"""Unit tests for fieldkit.enrich.constants — shared enrich constants."""

import pytest

from fieldkit.enrich.constants import GARBAGE_NAMES


@pytest.mark.unit
def test_garbage_names_has_16_entries() -> None:
    """GARBAGE_NAMES must contain exactly 16 entries."""
    assert len(GARBAGE_NAMES) == 16


@pytest.mark.unit
def test_garbage_names_is_frozenset() -> None:
    """GARBAGE_NAMES must be a frozenset (immutable, hashable)."""
    assert isinstance(GARBAGE_NAMES, frozenset)


@pytest.mark.unit
def test_garbage_names_contains_role() -> None:
    """'Role' must be in GARBAGE_NAMES."""
    assert "Role" in GARBAGE_NAMES


@pytest.mark.unit
def test_garbage_names_contains_economic_buyer() -> None:
    """'Economic Buyer' must be in GARBAGE_NAMES."""
    assert "Economic Buyer" in GARBAGE_NAMES


@pytest.mark.unit
def test_garbage_names_contains_champion() -> None:
    """'Champion' must be in GARBAGE_NAMES."""
    assert "Champion" in GARBAGE_NAMES


@pytest.mark.unit
def test_garbage_names_contains_all_expected_entries() -> None:
    """GARBAGE_NAMES must contain all 16 canonical role taxonomy strings."""
    expected = {
        "Role",
        "Status",
        "Action Needed",
        "Economic Buyer",
        "Champion",
        "Technical Buyer",
        "Influencer",
        "Procurement",
        "Adoption Lead",
        "End User",
        "Legal",
        "Partner Sponsor",
        "Accounts Payable",
        "Evaluator",
        "Paper Process",
        "Decision Maker",
    }
    assert expected == GARBAGE_NAMES
