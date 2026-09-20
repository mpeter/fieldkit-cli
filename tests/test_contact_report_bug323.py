"""Unit tests for fieldkit/enrich/generate_report.py.

Covers:
  historic regression: analyze_by_account slug-remap fix (union key sets, clamp rate/failed)
  historic regression: find_failed_contacts slug-mismatch fix (match on full_name only)
"""

from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contact(full_name: str, account: str, **extra: Any) -> dict[str, Any]:
    """Build a minimal contact dict for test fixtures."""
    return {"full_name": full_name, "account": account, **extra}


# ---------------------------------------------------------------------------
# analyze_by_account — historic regression regression tests
# ---------------------------------------------------------------------------


# ── TestAnalyzeByAccount (flattened) ────────────────────────────────────────


def test_analyze_by_account_analyze_by_account_normal_case() -> None:
    """Same account keys in raw and enriched → correct fraction, failed >= 0."""
    from fieldkit.contact.report import analyze_by_account

    raw = [
        _contact("Alice", "acme-corp"),
        _contact("Bob", "acme-corp"),
        _contact("Carol", "acme-corp"),
        _contact("Dave", "globalpay"),
    ]
    enriched = [
        _contact("Alice", "acme-corp"),
        _contact("Bob", "acme-corp"),
    ]

    result = analyze_by_account(enriched, raw)

    assert "acme-corp" in result
    assert "globalpay" in result

    acme = result["acme-corp"]
    assert acme["raw"] == 3
    assert acme["enriched"] == 2
    assert acme["failed"] == 1
    assert acme["failed"] >= 0
    assert 0.0 <= acme["rate"] <= 100.0

    gp = result["globalpay"]
    assert gp["raw"] == 1
    assert gp["enriched"] == 0
    assert gp["failed"] == 1
    assert gp["rate"] == 0.0


def test_analyze_by_account_analyze_by_account_slug_remapped() -> None:
    """Slug-remapped accounts never produce negative failed or >100% rate.

    historic regression scenario: raw has account="Acme Corp" (display name),
    enriched has account="acme-corp" (canonical slug).  Old code would
    show 0% for "Acme Corp" and miss "acme-corp".  New code must ensure
    that for every account row: failed >= 0 and rate <= 100.0.
    """
    from fieldkit.contact.report import analyze_by_account

    raw = [
        _contact("Alice", "Acme Corp"),
        _contact("Bob", "Acme Corp"),
        _contact("Carol", "Acme Corp"),
    ]
    enriched = [
        _contact("Alice", "acme-corp"),
        _contact("Bob", "acme-corp"),
        _contact("Carol", "acme-corp"),
    ]

    result = analyze_by_account(enriched, raw)

    # Assert the fix works: enriched contacts under "acme-corp" must be counted.
    # With the old code (no union), enriched contacts under the remapped slug
    # would be invisible, so total_enriched would be 0.  With the fix (union),
    # they appear and sum to 3.
    total_enriched = sum(s["enriched"] for s in result.values())
    assert total_enriched == 3, "historic regression regression: enriched contacts under remapped slug must be counted"

    # Every account row must satisfy the invariants regardless of key mismatch.
    for account, stats in result.items():
        assert stats["failed"] >= 0, f"account={account!r}: failed={stats['failed']} must be >= 0"
        assert stats["rate"] <= 100.0, f"account={account!r}: rate={stats['rate']} must be <= 100.0"


def test_analyze_by_account_analyze_by_account_rate_clamped_at_100() -> None:
    """When enriched count > raw count for same key, rate is clamped at 100.0.

    This guards against residual key-mismatch edge cases where a slug
    appears in enriched more times than in raw.
    """
    from fieldkit.contact.report import analyze_by_account

    # 1 raw contact, 3 enriched contacts with the same account key
    raw = [_contact("Alice", "acme-corp")]
    enriched = [
        _contact("Alice", "acme-corp"),
        _contact("Bob", "acme-corp"),
        _contact("Carol", "acme-corp"),
    ]

    result = analyze_by_account(enriched, raw)

    assert "acme-corp" in result
    acme = result["acme-corp"]
    # Rate must be clamped at 100.0, not 300.0
    assert acme["rate"] == 100.0, f"Expected rate=100.0, got {acme['rate']}"
    # failed must be clamped at 0, not -2
    assert acme["failed"] == 0, f"Expected failed=0, got {acme['failed']}"


def test_analyze_by_account_analyze_by_account_empty_inputs() -> None:
    """Both empty inputs → returns empty dict, no exception."""
    from fieldkit.contact.report import analyze_by_account

    result = analyze_by_account([], [])

    assert isinstance(result, dict)
    assert result == {}


# ---------------------------------------------------------------------------
# find_failed_contacts — historic regression slug-mismatch regression test (M6 fix)
# ---------------------------------------------------------------------------


# ── TestFindFailedContacts (flattened) ──────────────────────────────────────


def test_find_failed_contacts_find_failed_contacts_slug_remapped() -> None:
    """Contacts enriched under a remapped slug must not appear as failed.

    raw: account="Acme Corp" (display name)
    enriched: account="acme-corp" (canonical slug)
    Expected: zero failed contacts — all were successfully enriched.
    """
    from fieldkit.contact.report import find_failed_contacts

    raw = [
        _contact("Alice Smith", "Acme Corp"),
        _contact("Bob Jones", "Acme Corp"),
        _contact("Carol White", "Acme Corp"),
    ]
    enriched = [
        _contact("Alice Smith", "acme-corp", email="alice@acme-corp.com"),
        _contact("Bob Jones", "acme-corp", email="bob@acme-corp.com"),
        _contact("Carol White", "acme-corp", email="carol@acme-corp.com"),
    ]

    failed = find_failed_contacts(raw, enriched)

    assert failed == [], f"Expected 0 failed contacts after slug-remap fix, got {len(failed)}: " + ", ".join(
        c["full_name"] for c in failed
    )


def test_find_failed_contacts_find_failed_contacts_genuinely_missing() -> None:
    """Contacts absent from enriched are correctly reported as failed."""
    from fieldkit.contact.report import find_failed_contacts

    raw = [
        _contact("Alice Smith", "acme-corp"),
        _contact("Bob Jones", "acme-corp"),
    ]
    enriched = [
        _contact("Alice Smith", "acme-corp", email="alice@acme-corp.com"),
        # Bob is not enriched
    ]

    failed = find_failed_contacts(raw, enriched)

    assert len(failed) == 1
    assert failed[0]["full_name"] == "Bob Jones"


def test_find_failed_contacts_find_failed_contacts_empty_inputs() -> None:
    """Both empty → returns empty list, no exception."""
    from fieldkit.contact.report import find_failed_contacts

    result = find_failed_contacts([], [])
    assert result == []
