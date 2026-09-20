"""Tests for fieldkit.enrich.schema — ContactRecord Pydantic validators.

Covers validate_email and validate_linkedin field validators.
"""

import pytest
from pydantic import ValidationError

from fieldkit.enrich.schema import ContactRecord

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Minimal valid contact factory
# ---------------------------------------------------------------------------

_BASE = {
    "full_name": "Alice Smith",
    "company": "Acme Corp",
    "account": "acme",
    "source": "gmail",
    "confidence": "MEDIUM",
    "email": "alice@example.com",  # pii-guard: ignore
}


def _contact(**overrides):
    return {**_BASE, **overrides}


# ---------------------------------------------------------------------------
# 4A.4 validate_email
# ---------------------------------------------------------------------------


# ── TestValidateEmail (flattened) ───────────────────────────────────────────


def test_validate_email_valid_email_passes() -> None:
    """A properly formatted email is accepted unchanged."""
    rec = ContactRecord.model_validate(_contact(email="user@example.com"))  # pii-guard: ignore
    assert rec.email == "user@example.com"  # pii-guard: ignore


def test_validate_email_none_passes() -> None:
    """None email is accepted (email is optional)."""
    rec = ContactRecord.model_validate(_contact(email=None, linkedin_url="https://linkedin.com/in/foo"))
    assert rec.email is None


def test_validate_email_missing_at_raises() -> None:
    """Email without '@' raises ValidationError."""
    with pytest.raises(ValidationError, match="Invalid email format"):
        ContactRecord.model_validate(_contact(email="notanemail"))


# ---------------------------------------------------------------------------
# 4A.4 validate_linkedin
# ---------------------------------------------------------------------------


# ── TestValidateLinkedin (flattened) ────────────────────────────────────────


def test_validate_linkedin_valid_url_passes() -> None:
    """A LinkedIn URL is accepted unchanged."""
    rec = ContactRecord.model_validate(_contact(linkedin_url="https://linkedin.com/in/foo"))
    assert rec.linkedin_url == "https://linkedin.com/in/foo"


def test_validate_linkedin_none_passes() -> None:
    """None linkedin_url is accepted (field is optional)."""
    rec = ContactRecord.model_validate(_contact(linkedin_url=None))
    assert rec.linkedin_url is None


def test_validate_linkedin_bad_domain_raises() -> None:
    """A URL without 'linkedin.com' raises ValidationError."""
    with pytest.raises(ValidationError, match="Invalid LinkedIn URL"):
        ContactRecord.model_validate(_contact(linkedin_url="https://example.com/foo"))
