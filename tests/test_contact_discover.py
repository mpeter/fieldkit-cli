#!/usr/bin/env python3
"""Unit tests for tools/contact_enrich/discover_contacts.py.

Covers:
- extract_from_account_md: no file, no stakeholder section, table format, list format
- extract_from_sf_frontmatter: no pursuits dir, no frontmatter, no contact roles, parsed roles
- merge_contacts: empty, single, merge by email, merge by name+company, skip unidentifiable
"""

from pathlib import Path

import pytest

from fieldkit.contact._enrich_helpers import (
    extract_from_account_md,
    extract_from_sf_frontmatter,
    merge_contacts,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_account(tmp_path: Path, name: str = "acme") -> Path:
    """Return an account_path directory (does not create account.md)."""
    acct = tmp_path / name
    acct.mkdir(parents=True, exist_ok=True)
    return acct


def _write_account_md(account_path: Path, content: str) -> None:
    (account_path / "account.md").write_text(content, encoding="utf-8")


# ── TestExtractFromAccountMd ──────────────────────────────────────────────────


# ── TestExtractFromAccountMd (flattened) ────────────────────────────────────


def test_extract_from_account_md_no_account_md_returns_empty(tmp_path):
    """When account.md does not exist, returns empty list."""
    acct = _make_account(tmp_path)
    result = extract_from_account_md(acct)
    assert result == []


def test_extract_from_account_md_no_stakeholder_section_returns_empty(tmp_path):
    """account.md without a Stakeholder section returns empty list."""
    acct = _make_account(tmp_path)
    _write_account_md(
        acct,
        "# ACME\n\n## Overview\n\nSome text.\n\n## Competitive Landscape\n\nText.\n",
    )
    result = extract_from_account_md(acct)
    assert result == []


def test_extract_from_account_md_table_format_short_row_skipped(tmp_path):
    """Table rows with fewer than 4 cells are silently skipped."""
    acct = _make_account(tmp_path)
    _write_account_md(
        acct,
        "## Stakeholder Map\n\n"
        "| Name | Title | SF Contact Role | Supplemental |\n"
        "| --- | --- | --- | --- |\n"
        "| Short |\n",  # only 1 cell
    )
    result = extract_from_account_md(acct)
    assert result == []


def test_extract_from_account_md_table_format_garbage_name_filtered(tmp_path):
    """Rows whose Name matches a garbage taxonomy entry are filtered out."""
    acct = _make_account(tmp_path)
    _write_account_md(
        acct,
        "## Stakeholder Map\n\n"
        "| Name | Title | SF Contact Role | Supplemental |\n"
        "| --- | --- | --- | --- |\n"
        "| Economic Buyer | role | Champion | |\n"
        "| Champion | role | Champion | |\n",
    )
    result = extract_from_account_md(acct)
    assert result == []


def test_extract_from_account_md_list_format_happy_path(tmp_path):
    """Standard list-format line produces contact with name, title, and email."""
    acct = _make_account(tmp_path, "global-pay")
    _write_account_md(
        acct,
        "## Stakeholder Map\n\n- **Jane Smith** (CFO) — jane@globalpay.example.com\n",
    )
    result = extract_from_account_md(acct)
    assert len(result) == 1
    c = result[0]
    assert c["full_name"] == "Jane Smith"
    assert c["title"] == "CFO"
    assert c["email"] == "jane@globalpay.example.com"
    assert c["source"] == "account-file"


def test_extract_from_account_md_list_format_no_bold_name_skipped(tmp_path):
    """List lines without **bold** name are skipped."""
    acct = _make_account(tmp_path)
    _write_account_md(
        acct,
        "## Stakeholder Map\n\n- Some text without bold formatting\n",
    )
    result = extract_from_account_md(acct)
    assert result == []


def test_extract_from_account_md_list_format_with_linkedin(tmp_path):
    """LinkedIn URL is extracted from list-format line."""
    acct = _make_account(tmp_path, "acme-bank")
    _write_account_md(
        acct,
        "## Stakeholder Map\n\n- **Bob Jones** (VP Engineering) — https://www.linkedin.com/in/bobjones\n",
    )
    result = extract_from_account_md(acct)
    assert len(result) == 1
    assert result[0]["linkedin_url"] == "https://www.linkedin.com/in/bobjones"
    assert result[0]["full_name"] == "Bob Jones"


def test_extract_from_account_md_table_format_happy_path(tmp_path):
    """Full table row with Name|Title|SF Contact Role|Supplemental is parsed."""
    acct = _make_account(tmp_path, "acme")
    _write_account_md(
        acct,
        "## Stakeholder Map\n\n"
        "| Name | Title | SF Contact Role | Supplemental |\n"
        "| --- | --- | --- | --- |\n"
        "| Alice Wong | CTO | Technical Buyer | alice@acme.example.com |\n",
    )
    result = extract_from_account_md(acct)
    assert len(result) == 1
    c = result[0]
    assert c["full_name"] == "Alice Wong"
    assert c["title"] == "CTO"
    assert c["email"] == "alice@acme.example.com"
    assert c["source"] == "account-file"


# ── TestExtractFromSfFrontmatter ──────────────────────────────────────────────


# ── TestExtractFromSfFrontmatter (flattened) ────────────────────────────────


def test_extract_from_sf_frontmatter_no_pursuits_dir_returns_empty(tmp_path):
    """Account path with no pursuits/ directory returns empty list."""
    acct = _make_account(tmp_path)
    result = extract_from_sf_frontmatter(acct)
    assert result == []


def test_extract_from_sf_frontmatter_pursuit_no_frontmatter_skipped(tmp_path):
    """Pursuit file without --- frontmatter block is skipped."""
    acct = _make_account(tmp_path, "global-pay")
    pursuits_dir = acct / "pursuits"
    pursuits_dir.mkdir()
    (pursuits_dir / "nodash.md").write_text("# Just a heading\n\nNo frontmatter.\n", encoding="utf-8")
    result = extract_from_sf_frontmatter(acct)
    assert result == []


def test_extract_from_sf_frontmatter_pursuit_no_contact_roles_skipped(tmp_path, write_pursuit_generic):
    """Pursuit file with frontmatter but no sf_contact_roles is skipped."""
    acct = _make_account(tmp_path)
    write_pursuit_generic(
        acct,
        "deal-alpha",
        "stage: discover\nowner: me\n",
    )
    result = extract_from_sf_frontmatter(acct)
    assert result == []


def test_extract_from_sf_frontmatter_contact_roles_parsed(tmp_path, write_pursuit_generic):
    """Pursuit with sf_contact_roles list extracts name, title, and sf_role."""
    acct = _make_account(tmp_path, "global-pay")
    # Extra trailing field ensures all role lines have a trailing newline
    # (the regex (?:  -.*\n)* requires \n after each line).
    write_pursuit_generic(
        acct,
        "deal-beta",
        "stage: propose\n"
        "sf_contact_roles:\n"
        "  - Jane Smith (VP Engineering) - Technical Buyer\n"
        "  - Bob Jones (CFO) - Economic Buyer\n"
        "owner: me\n",
    )
    result = extract_from_sf_frontmatter(acct)
    assert len(result) == 2

    names = {c["full_name"] for c in result}
    assert "Jane Smith" in names
    assert "Bob Jones" in names

    jane = next(c for c in result if c["full_name"] == "Jane Smith")
    assert jane["title"] == "VP Engineering"
    assert jane["sf_role"] == "Technical Buyer"
    assert jane["source"] == "sf"


def test_extract_from_sf_frontmatter_role_without_title(tmp_path, write_pursuit_generic):
    """Role line without parenthesized title sets title to None."""
    acct = _make_account(tmp_path, "acme-bank")
    write_pursuit_generic(
        acct,
        "deal-gamma",
        "stage: discover\nsf_contact_roles:\n  - Alice Brown - Champion\nowner: me\n",
    )
    result = extract_from_sf_frontmatter(acct)
    assert len(result) == 1
    assert result[0]["full_name"] == "Alice Brown"
    assert result[0]["title"] is None
    assert result[0]["sf_role"] == "Champion"


# ── TestMergeContacts ─────────────────────────────────────────────────────────


# ── TestMergeContacts (flattened) ───────────────────────────────────────────


def _merge_contacts_c(**kwargs):
    """Build a minimal contact dict."""
    base = {"source": "account-file"}
    base.update(kwargs)
    return base


def test_merge_contacts_empty_list_returns_empty():
    """Empty input list returns empty list."""
    assert merge_contacts([]) == []


def test_merge_contacts_single_contact_returned():
    """Single contact is returned with _all_sources populated."""
    c = _merge_contacts_c(full_name="Jane Smith", email="jane@acme.example.com", company="Acme")
    result = merge_contacts([c])
    assert len(result) == 1
    assert result[0]["full_name"] == "Jane Smith"
    assert "_all_sources" in result[0]


def test_merge_contacts_merge_by_email():
    """Two contacts with same email are merged into one record."""
    c1 = _merge_contacts_c(
        full_name="Jane Smith",
        email="jane@acme.example.com",
        company="Acme",
        title="VP",
        source="account-file",
    )
    c2 = _merge_contacts_c(
        full_name="Jane Smith",
        email="jane@acme.example.com",
        company="Acme",
        sf_role="Champion",
        source="sf",
    )
    result = merge_contacts([c1, c2])
    assert len(result) == 1
    merged = result[0]
    # Both sources are represented
    assert set(merged["_all_sources"]) == {"account-file", "sf"}
    # Fields from both contacts are present
    assert merged.get("title") == "VP"
    assert merged.get("sf_role") == "Champion"


def test_merge_contacts_merge_by_name_company():
    """Two contacts with same name+company (no email) are merged."""
    c1 = _merge_contacts_c(full_name="Bob Jones", company="Global Pay", source="account-file")
    c2 = _merge_contacts_c(full_name="Bob Jones", company="Global Pay", sf_role="Technical Buyer", source="sf")
    result = merge_contacts([c1, c2])
    assert len(result) == 1
    assert set(result[0]["_all_sources"]) == {"account-file", "sf"}


def test_merge_contacts_no_identifiable_info_skipped():
    """Contact without email or name+company is skipped (cannot be keyed)."""
    c = _merge_contacts_c(source="gmail")  # no email, no full_name, no company
    result = merge_contacts([c])
    assert result == []


# ---------------------------------------------------------------------------
# 4C.2 load_gmail_cache_contacts
# ---------------------------------------------------------------------------


# ── TestLoadGmailCacheContacts (flattened) ──────────────────────────────────


@pytest.mark.unit
def test_load_gmail_cache_contacts_db_not_found_returns_empty(tmp_path: Path) -> None:
    """Returns [] when the gmail.db does not exist."""
    from unittest.mock import patch

    from fieldkit.contact._enrich_helpers import load_gmail_cache_contacts

    nonexistent = tmp_path / "nonexistent.db"
    with patch("fieldkit.contact._enrich_helpers.get_gmail_db_path", return_value=nonexistent):
        result = load_gmail_cache_contacts("acme")

    assert result == []


@pytest.mark.unit
def test_load_gmail_cache_contacts_returns_rows_from_people_table(tmp_path: Path) -> None:
    """Returns list of contact dicts when the people table has rows."""
    import sqlite3
    from unittest.mock import patch

    from fieldkit.contact._enrich_helpers import load_gmail_cache_contacts

    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE people (
                email TEXT, display_name TEXT, thread_count INTEGER,
                last_seen TEXT, account TEXT, is_internal INTEGER
            )"""
    )
    conn.execute(
        "INSERT INTO people VALUES (?, ?, ?, ?, ?, ?)",
        ("alice@acme.example.com", "Alice Smith", 5, "2026-06-01", "acme", 0),
    )
    conn.commit()
    conn.close()

    with patch("fieldkit.contact._enrich_helpers.get_gmail_db_path", return_value=db_path):
        result = load_gmail_cache_contacts("acme")

    assert len(result) == 1
    assert result[0]["email"] == "alice@acme.example.com"
    assert "full_name" in result[0]


# ---------------------------------------------------------------------------
# 4C.3 discover_all_contacts
# ---------------------------------------------------------------------------


# ── TestDiscoverAllContacts (flattened) ─────────────────────────────────────


@pytest.mark.unit
def test_discover_all_contacts_no_db_returns_empty(tmp_path: Path) -> None:
    """Returns empty list when accounts_root does not exist."""
    from unittest.mock import patch

    from fieldkit.contact._enrich_helpers import discover_all_contacts

    # Point accounts_root to a non-existent directory
    missing_accounts = tmp_path / "accounts"
    with patch("fieldkit.contact._enrich_helpers.get_accounts_root", return_value=missing_accounts):
        result = discover_all_contacts()

    assert isinstance(result, list)
    assert result == []
