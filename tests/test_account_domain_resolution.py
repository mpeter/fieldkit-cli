"""Integration contract for account-domain resolution and contact enrichment."""

from unittest.mock import patch

import pytest

from fieldkit.config import build_domain_account_map
from fieldkit.contact._enrich_helpers import enrich_batch
from fieldkit.enrich.schema import ContactRecord

pytestmark = pytest.mark.unit


def test_ambiguous_domain_does_not_override_inferred_account() -> None:
    accounts = {
        "acme": {"domains": ["example.com"]},
        "global-pay": {"domains": ["example.com"]},
    }
    with patch("fieldkit.config._accounts._load_accounts_yaml", return_value={"accounts": accounts}):
        domain_map = build_domain_account_map()

    record = ContactRecord(
        full_name="Test User",
        company="Test Co",
        account="inferred-account",
        email="director@example.com",
        linkedin_url=None,
        phone=None,
        title=None,
        source="gmail",
        last_contact_date=None,
        email_frequency=None,
        sf_role=None,
        confidence="HIGH",
    )
    contacts = [
        {
            "email": "director@example.com",
            "full_name": "Test User",
            "company": "Test Co",
            "account": "inferred-account",
        }
    ]
    with (
        patch("fieldkit.contact._enrich_helpers.get_user_email", return_value="user@example.com"),
        patch("fieldkit.contact._enrich_helpers.build_domain_account_map", return_value=domain_map),
        patch("fieldkit.contact._enrich_helpers.get_internal_domains", return_value=[]),
        patch("fieldkit.contact._enrich_helpers.enrich_contact", return_value=record),
        patch("fieldkit.contact._enrich_helpers.write_to_memory"),
    ):
        enriched, failed = enrich_batch(contacts, 0)

    assert domain_map == {}
    assert failed == []
    assert enriched[0]["account"] == "inferred-account"
    assert enriched[0]["confidence"] == "LOW"
    assert enriched[0]["source"] == "inferred-context"
