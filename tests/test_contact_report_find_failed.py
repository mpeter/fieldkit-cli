"""Tests for find_failed_contacts — covering uncovered branches.

cc=8, cov=48%, target: cover name-match+email-mismatch, no-email raw,
slug-remap, empty inputs.
"""

from typing import Any

import pytest

from fieldkit.contact.report import find_failed_contacts

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Name-matched but email differs → failed
# ---------------------------------------------------------------------------


# ── TestFindFailedContactsEmailMismatch (flattened) ─────────────────────────


def test_find_failed_contacts_email_mismatch_name_match_email_mismatch_is_failed() -> None:
    """Contact with matching name but different email is classified as failed."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Alice Smith", "email": "alice@acme-corp.com"},
    ]
    # Enriched has Alice with a different email
    enriched: list[dict[str, Any]] = [
        {"full_name": "Alice Smith", "email": "a.smith@acme-corp.com"},
    ]

    failed = find_failed_contacts(raw, enriched)

    assert len(failed) == 1
    assert failed[0]["full_name"] == "Alice Smith"


def test_find_failed_contacts_email_mismatch_name_match_same_email_not_failed() -> None:
    """Contact with matching name AND matching email is not classified as failed."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Bob Jones", "email": "bob@example.com"},  # pii-guard: ignore
    ]
    enriched: list[dict[str, Any]] = [
        {"full_name": "Bob Jones", "email": "bob@example.com"},  # pii-guard: ignore
    ]

    failed = find_failed_contacts(raw, enriched)

    assert failed == []


def test_find_failed_contacts_email_mismatch_name_match_email_case_insensitive() -> None:
    """Email comparison is case-insensitive."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Carol White", "email": "Carol@Example.COM"},
    ]
    enriched: list[dict[str, Any]] = [
        {"full_name": "carol white", "email": "carol@example.com"},  # pii-guard: ignore
    ]

    failed = find_failed_contacts(raw, enriched)

    assert failed == []


# ---------------------------------------------------------------------------
# Raw contact with no email → matched by name only
# ---------------------------------------------------------------------------


# ── TestFindFailedContactsNoEmail (flattened) ───────────────────────────────


def test_find_failed_contacts_no_email_raw_no_email_name_matched_not_failed() -> None:
    """Raw contact with no email is matched by name alone — not failed."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Dave Brown"},  # no email
    ]
    enriched: list[dict[str, Any]] = [
        {"full_name": "Dave Brown", "email": "dave@acme-corp.com"},
    ]

    failed = find_failed_contacts(raw, enriched)

    assert failed == []


def test_find_failed_contacts_no_email_raw_no_email_name_not_in_enriched_is_failed() -> None:
    """Raw contact with no email and name absent from enriched is failed."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Eve Green"},  # no email, not enriched
    ]

    failed = find_failed_contacts(raw, [])

    assert len(failed) == 1
    assert failed[0]["full_name"] == "Eve Green"


def test_find_failed_contacts_no_email_raw_with_none_email_treated_as_no_email() -> None:
    """email=None is treated the same as absent email field."""
    raw: list[dict[str, Any]] = [
        {"full_name": "Frank Black", "email": None},
    ]
    enriched: list[dict[str, Any]] = [
        {"full_name": "Frank Black", "email": "frank@example.com"},  # pii-guard: ignore
    ]

    # No email on raw side → any enriched match by name is sufficient
    failed = find_failed_contacts(raw, enriched)

    assert failed == []


# ---------------------------------------------------------------------------
# Slug remapping: account differs but name matches
# ---------------------------------------------------------------------------


# ── TestFindFailedContactsSlugRemap (flattened) ─────────────────────────────


def test_find_failed_contacts_slug_remap_account_slug_remap_does_not_cause_false_failure() -> None:
    """historic regression: account remapping must not cause false failures.

    When enrich_contacts remaps 'Acme Corp' → 'acme-corp', the name
    key is stable and should still find a match.
    """
    raw: list[dict[str, Any]] = [
        {"full_name": "Grace Hall", "account": "Acme Corp", "email": "grace@acme-corp.com"},
    ]
    enriched: list[dict[str, Any]] = [
        # account was remapped to slug, but name and email are the same
        {"full_name": "Grace Hall", "account": "acme-corp", "email": "grace@acme-corp.com"},
    ]

    failed = find_failed_contacts(raw, enriched)

    # Should not be failed — name+email match even though account differs
    assert failed == []


# ---------------------------------------------------------------------------
# Multiple contacts with same name (disambiguation by email)
# ---------------------------------------------------------------------------


# ── TestFindFailedContactsDuplicateName (flattened) ─────────────────────────


def test_find_failed_contacts_duplicate_name_duplicate_name_different_email_only_one_failed() -> None:
    """Two raw contacts with the same name but different emails.

    The one whose email matches enriched is not failed; the other is.
    """
    raw: list[dict[str, Any]] = [
        {"full_name": "Sam Lee", "email": "sam.lee@acme-corp.com"},
        {"full_name": "Sam Lee", "email": "samuel@example.com"},  # pii-guard: ignore
    ]
    enriched: list[dict[str, Any]] = [
        {"full_name": "Sam Lee", "email": "sam.lee@acme-corp.com"},
    ]

    failed = find_failed_contacts(raw, enriched)

    failed_emails = {c.get("email") for c in failed}
    assert "samuel@example.com" in failed_emails  # pii-guard: ignore
    assert "sam.lee@acme-corp.com" not in failed_emails


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


# ── TestFindFailedContactsEdgeCases (flattened) ─────────────────────────────


def test_find_failed_contacts_edge_cases_empty_raw_returns_empty() -> None:
    failed = find_failed_contacts([], [{"full_name": "Alice", "email": "a@example.com"}])  # pii-guard: ignore
    assert failed == []


def test_find_failed_contacts_edge_cases_empty_both_returns_empty() -> None:
    failed = find_failed_contacts([], [])
    assert failed == []


def test_find_failed_contacts_edge_cases_contact_missing_full_name_field() -> None:
    """Contact with no full_name uses empty string key — matched by empty string."""
    raw: list[dict[str, Any]] = [{"email": "unknown@example.com"}]  # pii-guard: ignore
    enriched: list[dict[str, Any]] = [{"email": "unknown@example.com"}]  # pii-guard: ignore

    # Both have empty full_name → name key matches → not failed
    failed = find_failed_contacts(raw, enriched)
    # The empty-name match logic: raw has email, enriched has same email
    # enriched_by_name[""] contains email, raw_email is in that set → not failed
    assert failed == []


def test_find_failed_contacts_edge_cases_preserves_original_contact_dict() -> None:
    """Failed contacts in returned list are the same dict objects as in raw."""
    raw_contact: dict[str, Any] = {"full_name": "Henry Ford", "account": "acme-corp"}
    raw = [raw_contact]

    failed = find_failed_contacts(raw, [])

    assert len(failed) == 1
    assert failed[0] is raw_contact
