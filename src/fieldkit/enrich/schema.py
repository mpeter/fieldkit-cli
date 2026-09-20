"""Contact enrichment validation schema.

Defines the required structure for enriched contact records.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ContactSource = Literal["backstory", "gmail", "sf", "account-file", "web", "inferred-context"]
ConfidenceTier = Literal["HIGH", "MEDIUM", "LOW"]

# Completeness scoring weights (sum of present fields determines tier).
_SCORE_WEIGHT_EMAIL: int = 2
_SCORE_WEIGHT_LINKEDIN: int = 2
_SCORE_WEIGHT_PHONE: int = 1
_SCORE_WEIGHT_LAST_CONTACT: int = 1
_SCORE_WEIGHT_EMAIL_FREQ: int = 1
_SCORE_WEIGHT_TITLE: int = 1
_SCORE_WEIGHT_SF_ROLE: int = 1

# Tier thresholds (score >= HIGH_THRESHOLD -> HIGH, >= MEDIUM_THRESHOLD -> MEDIUM, else LOW).
_COMPLETENESS_HIGH_THRESHOLD: int = 6
_COMPLETENESS_MEDIUM_THRESHOLD: int = 3


class ContactRecord(BaseModel):
    """Enriched contact record with validation rules."""

    # Required fields
    full_name: str = Field(..., min_length=2, description="Contact's full name")
    company: str = Field(..., min_length=2, description="Company name")
    account: str = Field(..., min_length=2, description="Which account this contact belongs to")

    # At least one contact method required (validated below)
    email: str | None = Field(None, description="Email address")
    linkedin_url: str | None = Field(None, description="LinkedIn profile URL")
    phone: str | None = Field(None, description="Phone number")

    # Optional but recommended
    title: str | None = Field(None, description="Job title")

    # Metadata
    source: ContactSource = Field(..., description="Where this contact was discovered")
    last_contact_date: str | None = Field(None, description="ISO date of last interaction (YYYY-MM-DD)")
    email_frequency: int | None = Field(None, ge=0, description="Number of email threads (from gmail cache)")
    sf_role: str | None = Field(None, description="Salesforce contact role if applicable")
    confidence: ConfidenceTier = Field(..., description="Data confidence level")
    enriched_at: str = Field(default_factory=lambda: datetime.now().isoformat(), description="When enrichment occurred")
    retry_count: int = Field(default=0, ge=0, le=3, description="Number of enrichment retry attempts")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        """Basic email validation."""
        if v and "@" not in v:
            raise ValueError("Invalid email format")
        return v

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin(cls, v: str | None) -> str | None:
        """Basic LinkedIn URL validation."""
        if v and "linkedin.com" not in v.lower():
            raise ValueError("Invalid LinkedIn URL")
        return v

    def model_post_init(self, __context: Any) -> None:
        """Ensure at least one contact method exists."""
        if not any([self.email, self.linkedin_url, self.phone]):
            raise ValueError("Contact must have at least one: email, linkedin_url, or phone")

    def _calculate_confidence(self) -> ConfidenceTier:
        """Calculate confidence tier based on available data."""
        score = 0

        # Primary contact methods
        if self.email:
            score += _SCORE_WEIGHT_EMAIL
        if self.linkedin_url:
            score += _SCORE_WEIGHT_LINKEDIN
        if self.phone:
            score += _SCORE_WEIGHT_PHONE

        # Activity signals
        if self.last_contact_date:
            score += _SCORE_WEIGHT_LAST_CONTACT
        if self.email_frequency and self.email_frequency > 0:
            score += _SCORE_WEIGHT_EMAIL_FREQ

        # Role/title clarity
        if self.title:
            score += _SCORE_WEIGHT_TITLE
        if self.sf_role:
            score += _SCORE_WEIGHT_SF_ROLE

        # Tier assignment
        if score >= _COMPLETENESS_HIGH_THRESHOLD:
            return "HIGH"
        elif score >= _COMPLETENESS_MEDIUM_THRESHOLD:
            return "MEDIUM"
        else:
            return "LOW"


class EnrichmentCheckpoint(BaseModel):
    """Progress checkpoint for resumable enrichment."""

    last_completed_account: str
    last_completed_contact_index: int
    total_processed: int
    total_enriched: int
    total_failed: int
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
