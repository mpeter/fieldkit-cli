#!/usr/bin/env python3
"""Tests for account.md structural compliance.

Validates that account files match the canonical structure documented in
config/account-md.schema.md and that discovery scripts can parse them.
"""

import re
from pathlib import Path

import pytest

from tests.conftest import DATA_ROOT, needs_data

pytestmark = [pytest.mark.integration, needs_data]

_ACCOUNTS_DIR = (DATA_ROOT / "accounts") if DATA_ROOT else Path("accounts")


def _is_internal_only(account_path: Path) -> bool:
    """Return True if the account.md has internal_only: true in its YAML frontmatter."""
    account_file = account_path / "account.md"
    if not account_file.exists():
        return False
    content = account_file.read_text()
    # Match frontmatter block
    fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        return False
    return bool(re.search(r"^internal_only\s*:\s*true", fm_match.group(1), re.MULTILINE))


def test_all_accounts_have_stakeholder_section():
    """Verify all account.md files have a Stakeholder Map section."""
    accounts_dir = _ACCOUNTS_DIR

    for account_path in accounts_dir.iterdir():
        if not account_path.is_dir():
            continue
        if account_path.name.startswith(".") or account_path.name == ".template":
            continue
        if _is_internal_only(account_path):
            continue  # Internal routing accounts don't have stakeholder maps

        account_file = account_path / "account.md"
        if not account_file.exists():
            pytest.fail(f"{account_path.name}: account.md missing")

        content = account_file.read_text()

        # Check for Stakeholder Map section
        match = re.search(r"##\s+Stakeholder(?:\s+Map)?", content, re.IGNORECASE)

        assert match, f"{account_path.name}: No '## Stakeholder Map' section found"


def test_stakeholder_tables_have_required_columns():
    """Verify stakeholder tables include Name, Title, SF Contact Role columns."""
    accounts_dir = _ACCOUNTS_DIR
    _required_cols = {"name", "title", "sf contact role", "contact role"}

    for account_path in accounts_dir.iterdir():
        if not account_path.is_dir():
            continue
        if account_path.name.startswith(".") or account_path.name == ".template":
            continue

        account_file = account_path / "account.md"
        content = account_file.read_text()

        # Find stakeholder section
        match = re.search(r"##\s+Stakeholder(?:\s+Map)?.*?\n+(.*?)(?=\n## [^#]|\Z)", content, re.DOTALL | re.IGNORECASE)

        if not match:
            continue

        section = match.group(1)

        # Find table headers (look for | Name | ... followed by separator row)
        table_headers = re.findall(r"(\|[^\n]+\|)\n\s*\|[\s-]+\|", section)

        # Check at least one table has required columns
        found_valid_table = False
        for header in table_headers:
            cols = [c.strip().lower() for c in header.split("|")]

            # Skip Coverage Gaps table (has Role | Status | Action Needed)
            if "status" in cols and "action needed" in cols:
                continue

            # Check for required columns
            has_name = any("name" in col for col in cols)
            has_title = any("title" in col for col in cols)
            has_role = any("role" in col or "contact role" in col for col in cols)

            if has_name and has_title and has_role:
                found_valid_table = True
                break

        assert found_valid_table, (
            f"{account_path.name}: No stakeholder table with Name, Title, and SF Contact Role columns found"
        )


def test_discovery_script_can_parse_all_accounts():
    """Verify discover_contacts.py can extract contacts from all accounts."""
    from fieldkit.contact._enrich_helpers import extract_from_account_md

    accounts_dir = _ACCOUNTS_DIR

    for account_path in accounts_dir.iterdir():
        if not account_path.is_dir():
            continue
        if account_path.name.startswith(".") or account_path.name == ".template":
            continue
        if _is_internal_only(account_path):
            continue  # Internal routing accounts have no contacts to discover

        contacts = extract_from_account_md(account_path)

        # Each account should have at least a few contacts
        assert len(contacts) > 0, (
            f"{account_path.name}: Discovery returned 0 contacts — check if stakeholder table is parseable"
        )

        # Verify contacts have required fields
        for contact in contacts:
            assert contact.get("full_name"), f"{account_path.name}: Contact missing full_name: {contact}"
            assert contact.get("account") == account_path.name, (
                f"{account_path.name}: Contact has wrong account: {contact}"
            )


def test_coverage_gaps_table_not_parsed_as_contacts():
    """Verify Coverage Gaps table entries are not treated as contacts."""
    from fieldkit.contact._enrich_helpers import extract_from_account_md

    accounts_dir = _ACCOUNTS_DIR

    # Role names that should NOT appear as contacts
    garbage_names = {
        "Role",
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
    }

    for account_path in accounts_dir.iterdir():
        if not account_path.is_dir():
            continue
        if account_path.name.startswith(".") or account_path.name == ".template":
            continue

        contacts = extract_from_account_md(account_path)

        # Check no garbage names made it through
        contact_names = {c.get("full_name") for c in contacts}
        garbage_found = contact_names & garbage_names

        assert not garbage_found, (
            f"{account_path.name}: Found garbage entries from Coverage Gaps table: {garbage_found}"
        )


def test_mandatory_sections_present():
    """Verify all accounts have mandatory sections."""
    accounts_dir = _ACCOUNTS_DIR

    mandatory_sections = [
        "Account Overview",
        "Why We're Here",
        "Stakeholder Map",
        "Competitive Landscape",
        "Expansion Opportunities",
    ]

    for account_path in accounts_dir.iterdir():
        if not account_path.is_dir():
            continue
        if account_path.name.startswith(".") or account_path.name == ".template":
            continue
        if _is_internal_only(account_path):
            continue  # Internal routing accounts use a minimal structure

        account_file = account_path / "account.md"
        content = account_file.read_text()

        for section in mandatory_sections:
            assert re.search(rf"##\s+{re.escape(section)}", content, re.IGNORECASE), (
                f"{account_path.name}: Missing mandatory section '## {section}'"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
