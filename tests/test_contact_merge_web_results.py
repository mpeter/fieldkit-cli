"""Tests for merge_web_results() in fieldkit.contact._enrich_helpers.

Covers linkedin/email/phone merge, no-overwrite of existing data, no-match
lookups, and empty-input edge cases.
"""

from typing import Any

import pytest

from fieldkit.contact._enrich_helpers import merge_web_results

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Merge LinkedIn URL
# ---------------------------------------------------------------------------


def test_merge_web_results_merges_linkedin_when_contact_has_none() -> None:
    """LinkedIn URL is merged when the contact does not already have one."""
    raw: list[dict[str, Any]] = [{"full_name": "Alice Smith", "account": "acme-corp"}]
    web: list[dict[str, Any]] = [
        {"full_name": "Alice Smith", "account": "acme-corp", "linkedin_url": "https://linkedin.com/in/alice-smith"}
    ]

    updated, count = merge_web_results(raw, web)

    assert updated[0]["linkedin_url"] == "https://linkedin.com/in/alice-smith"
    assert count == 1


def test_merge_web_results_does_not_overwrite_existing_linkedin() -> None:
    """LinkedIn URL is not overwritten when contact already has one."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Bob Jones", "account": "acme-corp", "linkedin_url": "https://linkedin.com/in/existing"}
    ]
    web: list[dict[str, Any]] = [
        {"full_name": "Bob Jones", "account": "acme-corp", "linkedin_url": "https://linkedin.com/in/new-url"}
    ]

    updated, count = merge_web_results(raw, web)

    assert updated[0]["linkedin_url"] == "https://linkedin.com/in/existing"
    assert count == 0


# ---------------------------------------------------------------------------
# Merge email
# ---------------------------------------------------------------------------


def test_merge_web_results_merges_email_when_contact_has_none() -> None:
    """Email is merged when the contact does not already have one."""
    raw: list[dict[str, Any]] = [{"full_name": "Carol White", "account": "globalpay"}]
    web: list[dict[str, Any]] = [
        {"full_name": "Carol White", "account": "globalpay", "email": "carol@globalpay.example.com"}
    ]

    updated, count = merge_web_results(raw, web)

    assert updated[0]["email"] == "carol@globalpay.example.com"
    assert count == 1


def test_merge_web_results_does_not_overwrite_existing_email() -> None:
    """Email is not overwritten when contact already has one."""
    raw: list[dict[str, Any]] = [{"full_name": "Dave Brown", "account": "acme-corp", "email": "dave@acme-corp.com"}]
    web: list[dict[str, Any]] = [{"full_name": "Dave Brown", "account": "acme-corp", "email": "d.brown@acme-corp.com"}]

    updated, count = merge_web_results(raw, web)

    assert updated[0]["email"] == "dave@acme-corp.com"
    assert count == 0


# ---------------------------------------------------------------------------
# Merge phone
# ---------------------------------------------------------------------------


def test_merge_web_results_merges_phone_when_contact_has_none() -> None:
    """Phone is merged when the contact does not already have one."""
    raw: list[dict[str, Any]] = [{"full_name": "Eve Green", "account": "acme-corp"}]
    web: list[dict[str, Any]] = [{"full_name": "Eve Green", "account": "acme-corp", "phone": "+1-555-0100"}]

    updated, count = merge_web_results(raw, web)

    assert updated[0]["phone"] == "+1-555-0100"
    assert count == 1


def test_merge_web_results_does_not_overwrite_existing_phone() -> None:
    """Phone is not overwritten when contact already has one."""
    raw: list[dict[str, Any]] = [{"full_name": "Frank Black", "account": "acme-corp", "phone": "+1-555-0200"}]
    web: list[dict[str, Any]] = [{"full_name": "Frank Black", "account": "acme-corp", "phone": "+1-555-0999"}]

    updated, count = merge_web_results(raw, web)

    assert updated[0]["phone"] == "+1-555-0200"
    assert count == 0


# ---------------------------------------------------------------------------
# Multiple fields merged in one pass
# ---------------------------------------------------------------------------


def test_merge_web_results_merges_all_missing_fields() -> None:
    """LinkedIn, email, and phone are all merged when all are missing."""
    raw: list[dict[str, Any]] = [{"full_name": "Grace Hall", "account": "acme-corp"}]
    web: list[dict[str, Any]] = [
        {
            "full_name": "Grace Hall",
            "account": "acme-corp",
            "linkedin_url": "https://linkedin.com/in/grace",
            "email": "grace@acme-corp.com",
            "phone": "+1-555-0300",
        }
    ]

    updated, count = merge_web_results(raw, web)

    assert updated[0]["linkedin_url"] == "https://linkedin.com/in/grace"
    assert updated[0]["email"] == "grace@acme-corp.com"
    assert updated[0]["phone"] == "+1-555-0300"
    assert count == 3


# ---------------------------------------------------------------------------
# No match — unrelated or mismatched web result
# ---------------------------------------------------------------------------


def test_merge_web_results_no_match_leaves_contact_unchanged() -> None:
    """A contact with no matching web result is unchanged."""
    raw: list[dict[str, Any]] = [{"full_name": "Henry Ford", "account": "acme-corp"}]
    web: list[dict[str, Any]] = [
        {"full_name": "Someone Else", "account": "globalpay", "email": "else@globalpay.example.com"}
    ]

    updated, count = merge_web_results(raw, web)

    assert "email" not in updated[0]
    assert count == 0


def test_merge_web_results_lookup_key_is_name_and_account() -> None:
    """Matching uses (full_name.lower(), account) as the lookup key."""
    raw: list[dict[str, Any]] = [{"full_name": "Sam Lee", "account": "acme-corp"}]
    web: list[dict[str, Any]] = [
        # Same name, different account → should NOT match
        {"full_name": "Sam Lee", "account": "globalpay", "email": "sam@globalpay.example.com"}
    ]

    updated, count = merge_web_results(raw, web)

    assert "email" not in updated[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_merge_web_results_empty_raw_returns_empty() -> None:
    web: list[dict[str, Any]] = [{"full_name": "Alice", "account": "acme-corp", "email": "a@example.com"}]
    updated, count = merge_web_results([], web)
    assert updated == []
    assert count == 0


def test_merge_web_results_empty_web_returns_unchanged() -> None:
    raw: list[dict[str, Any]] = [{"full_name": "Bob", "account": "acme-corp"}]
    updated, count = merge_web_results(raw, [])
    assert updated == raw
    assert count == 0


def test_merge_web_results_returns_same_list_object() -> None:
    """merge_web_results mutates and returns the original raw list."""
    raw: list[dict[str, Any]] = [{"full_name": "Carol", "account": "acme-corp"}]
    web: list[dict[str, Any]] = [{"full_name": "Carol", "account": "acme-corp", "email": "c@acme-corp.com"}]

    updated, _ = merge_web_results(raw, web)

    assert updated is raw


def test_merge_web_results_web_result_without_linkedin_does_not_clear_existing() -> None:
    """Web result without a linkedin_url key does not remove existing URL."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Dave", "account": "acme-corp", "linkedin_url": "https://linkedin.com/in/dave"}
    ]
    web: list[dict[str, Any]] = [
        # No linkedin_url key in web result
        {"full_name": "Dave", "account": "acme-corp", "email": "dave@acme-corp.com"}
    ]

    updated, count = merge_web_results(raw, web)

    assert updated[0]["linkedin_url"] == "https://linkedin.com/in/dave"
    assert updated[0]["email"] == "dave@acme-corp.com"
    # email was added (count 1), linkedin was NOT touched
    assert count == 1
