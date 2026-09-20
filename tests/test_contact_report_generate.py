"""Tests for fieldkit/enrich/generate_report.py — find_failed_contacts."""

from typing import Any

import pytest

from fieldkit.contact.report import find_failed_contacts

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Task 11.11 — find_failed_contacts returns only failed contacts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_failed_contacts_returns_only_failed() -> None:
    """find_failed_contacts returns contacts from raw that are absent in enriched."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Alice Smith", "account": "acme-corp", "email": "alice@acme-corp.com"},
        {"full_name": "Bob Jones", "account": "acme-corp"},
        {"full_name": "Carol White", "account": "globalpay"},
    ]
    # Alice is enriched; Bob and Carol are not
    enriched: list[dict[str, Any]] = [
        {"full_name": "Alice Smith", "account": "acme-corp", "email": "alice@acme-corp.com"},
    ]

    failed = find_failed_contacts(raw, enriched)

    failed_names = {c["full_name"] for c in failed}
    assert "Alice Smith" not in failed_names
    assert "Bob Jones" in failed_names
    assert "Carol White" in failed_names
    assert len(failed) == 2


@pytest.mark.unit
def test_find_failed_contacts_empty_enriched_returns_all_raw() -> None:
    """When enriched is empty, all raw contacts are failed."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Dave Brown", "account": "acme-corp"},
        {"full_name": "Eve Green", "account": "globalpay"},
    ]

    failed = find_failed_contacts(raw, [])

    assert len(failed) == 2


@pytest.mark.unit
def test_find_failed_contacts_all_enriched_returns_empty() -> None:
    """When all raw contacts are enriched, returns empty list."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Frank Black", "account": "acme-corp"},
    ]
    enriched: list[dict[str, Any]] = [
        {"full_name": "Frank Black", "account": "acme-corp"},
    ]

    failed = find_failed_contacts(raw, enriched)

    assert failed == []


# ---------------------------------------------------------------------------
# calculate_enrichment_rate
# ---------------------------------------------------------------------------


# ── TestCalculateEnrichmentRate (flattened) ─────────────────────────────────


def test_calculate_enrichment_rate_basic_rate() -> None:
    from fieldkit.contact.report import calculate_enrichment_rate

    raw = [{"full_name": "A"}, {"full_name": "B"}, {"full_name": "C"}, {"full_name": "D"}]
    enriched = [{"full_name": "A"}, {"full_name": "B"}]
    result = calculate_enrichment_rate(raw, enriched)
    assert result["total_raw"] == 4
    assert result["total_enriched"] == 2
    assert result["rate_percent"] == 50.0
    assert result["failed"] == 2


def test_calculate_enrichment_rate_empty_raw_returns_zero_rate() -> None:
    from fieldkit.contact.report import calculate_enrichment_rate

    result = calculate_enrichment_rate([], [])
    assert result["total_raw"] == 0
    assert result["rate_percent"] == 0
    assert result["failed"] == 0


def test_calculate_enrichment_rate_all_enriched_returns_100() -> None:
    from fieldkit.contact.report import calculate_enrichment_rate

    raw = [{"full_name": "A"}]
    enriched = [{"full_name": "A"}]
    result = calculate_enrichment_rate(raw, enriched)
    assert result["rate_percent"] == 100.0
    assert result["failed"] == 0


# ---------------------------------------------------------------------------
# analyze_by_account
# ---------------------------------------------------------------------------


# ── TestAnalyzeByAccount (flattened) ────────────────────────────────────────


def test_analyze_by_account_groups_by_account() -> None:
    from fieldkit.contact.report import analyze_by_account

    raw = [
        {"full_name": "A", "account": "acme-corp"},
        {"full_name": "B", "account": "acme-corp"},
        {"full_name": "C", "account": "globalpay"},
    ]
    enriched = [
        {"full_name": "A", "account": "acme-corp"},
    ]
    result = analyze_by_account(enriched, raw)
    assert "acme-corp" in result
    assert result["acme-corp"]["raw"] == 2
    assert result["acme-corp"]["enriched"] == 1
    assert result["acme-corp"]["failed"] == 1
    assert "globalpay" in result
    assert result["globalpay"]["raw"] == 1
    assert result["globalpay"]["enriched"] == 0


def test_analyze_by_account_empty_inputs() -> None:
    from fieldkit.contact.report import analyze_by_account

    result = analyze_by_account([], [])
    assert result == {}


# ---------------------------------------------------------------------------
# analyze_sources and analyze_confidence
# ---------------------------------------------------------------------------


# ── TestAnalyzeSourcesAndConfidence (flattened) ─────────────────────────────


def test_analyze_sources_and_confidence_analyze_sources_counts_by_source() -> None:
    from fieldkit.contact.report import analyze_sources

    enriched = [
        {"source": "linkedin"},
        {"source": "linkedin"},
        {"source": "web"},
    ]
    result = analyze_sources(enriched)
    assert result["linkedin"] == 2
    assert result["web"] == 1


def test_analyze_sources_and_confidence_analyze_sources_unknown_fallback() -> None:
    from fieldkit.contact.report import analyze_sources

    enriched = [{"full_name": "A"}]  # no source key
    result = analyze_sources(enriched)
    assert result.get("unknown") == 1


def test_analyze_sources_and_confidence_analyze_confidence_counts_tiers() -> None:
    from fieldkit.contact.report import analyze_confidence

    enriched = [
        {"confidence": "HIGH"},
        {"confidence": "HIGH"},
        {"confidence": "LOW"},
    ]
    result = analyze_confidence(enriched)
    assert result["HIGH"] == 2
    assert result["LOW"] == 1


def test_analyze_sources_and_confidence_analyze_confidence_unknown_fallback() -> None:
    from fieldkit.contact.report import analyze_confidence

    enriched = [{"full_name": "A"}]  # no confidence key
    result = analyze_confidence(enriched)
    assert result.get("UNKNOWN") == 1


# ---------------------------------------------------------------------------
# top_engaged_contacts
# ---------------------------------------------------------------------------


# ── TestTopEngagedContacts (flattened) ──────────────────────────────────────


def test_top_engaged_contacts_returns_top_n_by_frequency() -> None:
    from fieldkit.contact.report import top_engaged_contacts

    enriched = [
        {"full_name": "A", "email_frequency": 10},
        {"full_name": "B", "email_frequency": 5},
        {"full_name": "C", "email_frequency": 20},
        {"full_name": "D"},  # no frequency
    ]
    result = top_engaged_contacts(enriched, limit=2)
    assert len(result) == 2
    assert result[0]["full_name"] == "C"
    assert result[1]["full_name"] == "A"


def test_top_engaged_contacts_excludes_zero_frequency() -> None:
    from fieldkit.contact.report import top_engaged_contacts

    enriched = [
        {"full_name": "A", "email_frequency": 0},
        {"full_name": "B", "email_frequency": 3},
    ]
    result = top_engaged_contacts(enriched, limit=10)
    assert len(result) == 1
    assert result[0]["full_name"] == "B"


def test_top_engaged_contacts_empty_returns_empty() -> None:
    from fieldkit.contact.report import top_engaged_contacts

    assert top_engaged_contacts([], limit=5) == []
