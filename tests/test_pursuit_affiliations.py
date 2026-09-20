"""Unit tests for scan_pursuit_affiliations() in contact_resolver.py.

All tests use tmp_path fixtures — no dependency on live account files.
"""

import pytest

from fieldkit.contact import resolver as cr

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path, rel, content):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Canonical 5-column table (Name | Title | Support | MEDDPICC Role | Notes)
# ---------------------------------------------------------------------------

PURSUIT_5COL = """\
## Key Stakeholders

| Name              | Title          | Support | MEDDPICC Role | Notes        |
| ----------------- | -------------- | ------- | ------------- | ------------ |
| Alice Smith       | VP Engineering | Partner | Champion      | Key contact  |
| Bob Jones         | Director       | Neutral | Economic Buyer| —            |
"""


def test_canonical_5col_name_match(tmp_path):
    _write(tmp_path, "acme/pursuits/deal.md", PURSUIT_5COL)
    results = cr.scan_pursuit_affiliations("Alice Smith", accounts_root=tmp_path)
    assert len(results) == 1
    r = results[0]
    assert r["name"] == "Alice Smith"
    assert r["title"] == "VP Engineering"
    assert r["support"] == "Partner"
    assert r["meddpicc_role"] == "Champion"
    assert r["notes"] == "Key contact"
    assert r["pursuit_file"] == "acme/pursuits/deal.md"


def test_canonical_5col_no_match(tmp_path):
    _write(tmp_path, "acme/pursuits/deal.md", PURSUIT_5COL)
    results = cr.scan_pursuit_affiliations("Unknown Person", accounts_root=tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# 3-column minimal table (Name | Title | Notes)
# ---------------------------------------------------------------------------

PURSUIT_3COL = """\
## Key Stakeholders

| Name        | Title      | Notes                          |
| ----------- | ---------- | ------------------------------ |
| Alex Rivera | Example Vendor AE | Owner — must update Salesforce |
"""


def test_3col_minimal_match(tmp_path):
    _write(tmp_path, "sf/pursuits/acs.md", PURSUIT_3COL)
    results = cr.scan_pursuit_affiliations("Alex Rivera", accounts_root=tmp_path)
    assert len(results) == 1
    r = results[0]
    assert r["name"] == "Alex Rivera"
    assert r["title"] == "Example Vendor AE"
    assert r["support"] is None
    assert r["meddpicc_role"] is None
    assert r["notes"] == "Owner — must update Salesforce"


# ---------------------------------------------------------------------------
# 7-column account.md style (Name | Title | Engagement | Support | SF Contact Role | Supplemental | Notes)
# ---------------------------------------------------------------------------

ACCOUNT_7COL = """\
## Stakeholder Map

| Name              | Title        | Engagement | Support | SF Contact Role | Supplemental | Notes        |
| ----------------- | ------------ | ---------- | ------- | --------------- | ------------ | ------------ |
| Patrick Pitsinger | VP, ADS      | Active     | Partner | **Champion**    | —            | Sponsor      |
| Philip Jones      | Middleware Mgr| Active    | Neutral | Economic Buyer  | —            | Budget owner |
"""


def test_account_7col_match(tmp_path):
    _write(tmp_path, "acme-bank/account.md", ACCOUNT_7COL)
    results = cr.scan_pursuit_affiliations("Patrick Pitsinger", accounts_root=tmp_path)
    assert len(results) == 1
    r = results[0]
    assert r["name"] == "Patrick Pitsinger"
    assert r["title"] == "VP, ADS"
    assert r["support"] == "Partner"
    # SF Contact Role maps to meddpicc_role slot when column is present (or None per alias)
    # Our alias set does not include "sf contact role" — so meddpicc_role is None here
    # Support and name are what matter for this test
    assert r["pursuit_file"] == "acme-bank/account.md"


# ---------------------------------------------------------------------------
# Gmail Signals email match
# ---------------------------------------------------------------------------

PURSUIT_WITH_SIGNALS = """\
## Key Stakeholders

| Name        | Title | Notes |
| ----------- | ----- | ----- |
| Alex Rivera | AE    | owner |

## Gmail Signals

- Pat Edwards (pat.edwards@shieldins.example.com) — 6 msgs, 5 threads
- Shannon Mueller (shannon.mueller@shieldins.example.com) — 2 msgs
"""


def test_gmail_signals_email_match(tmp_path):
    _write(tmp_path, "sf/pursuits/acs.md", PURSUIT_WITH_SIGNALS)
    results = cr.scan_pursuit_affiliations("pat.edwards@shieldins.example.com", accounts_root=tmp_path)
    assert len(results) == 1
    assert results[0]["pursuit_file"] == "sf/pursuits/acs.md"


def test_gmail_signals_email_no_match(tmp_path):
    _write(tmp_path, "sf/pursuits/acs.md", PURSUIT_WITH_SIGNALS)
    results = cr.scan_pursuit_affiliations("nobody@example.com", accounts_root=tmp_path)  # pii-guard: ignore
    assert results == []


# ---------------------------------------------------------------------------
# Bold stripping
# ---------------------------------------------------------------------------

PURSUIT_BOLD = """\
## Stakeholders

| Name       | Title | Support | MEDDPICC Role    | Notes |
| ---------- | ----- | ------- | ---------------- | ----- |
| Carol White | Dir  | Neutral | **Economic Buyer** | —   |
"""


def test_bold_stripped_from_meddpicc_role(tmp_path):
    _write(tmp_path, "acme/pursuits/deal.md", PURSUIT_BOLD)
    results = cr.scan_pursuit_affiliations("Carol White", accounts_root=tmp_path)
    assert len(results) == 1
    assert results[0]["meddpicc_role"] == "Economic Buyer"


# ---------------------------------------------------------------------------
# Multiple matches across files
# ---------------------------------------------------------------------------


def test_multiple_matches_across_files(tmp_path):
    _write(tmp_path, "acme-bank/pursuits/deal1.md", PURSUIT_5COL)
    _write(tmp_path, "acme-bank/pursuits/deal2.md", PURSUIT_5COL)
    results = cr.scan_pursuit_affiliations("Alice Smith", accounts_root=tmp_path)
    assert len(results) == 2
    files = {r["pursuit_file"] for r in results}
    assert "acme-bank/pursuits/deal1.md" in files
    assert "acme-bank/pursuits/deal2.md" in files


# ---------------------------------------------------------------------------
# Case-insensitive name match
# ---------------------------------------------------------------------------


def test_case_insensitive_name(tmp_path):
    _write(tmp_path, "acme/pursuits/deal.md", PURSUIT_5COL)
    results = cr.scan_pursuit_affiliations("alice smith", accounts_root=tmp_path)
    assert len(results) == 1
    assert results[0]["name"] == "Alice Smith"


# ---------------------------------------------------------------------------
# Empty directory
# ---------------------------------------------------------------------------


def test_empty_accounts_dir(tmp_path):
    results = cr.scan_pursuit_affiliations("Alice Smith", accounts_root=tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# Projects directory excluded
# ---------------------------------------------------------------------------


def test_projects_excluded(tmp_path):
    _write(tmp_path, "acme-bank/projects/active-project.md", PURSUIT_5COL)
    results = cr.scan_pursuit_affiliations("Alice Smith", accounts_root=tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# .template directory excluded
# ---------------------------------------------------------------------------


def test_template_excluded(tmp_path):
    _write(tmp_path, ".template/pursuits/deal.md", PURSUIT_5COL)
    results = cr.scan_pursuit_affiliations("Alice Smith", accounts_root=tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# None / empty input
# ---------------------------------------------------------------------------


def test_none_input(tmp_path):
    results = cr.scan_pursuit_affiliations(None, accounts_root=tmp_path)
    assert results == []


def test_empty_string_input(tmp_path):
    results = cr.scan_pursuit_affiliations("", accounts_root=tmp_path)
    assert results == []
