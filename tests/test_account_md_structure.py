"""Account creation and stakeholder parsing use isolated fictional records."""

import re
from pathlib import Path

import pytest
import yaml

from fieldkit.commands.init.wizard import _write_account_stub
from fieldkit.contact._enrich_helpers import extract_from_account_md

pytestmark = pytest.mark.unit


def test_account_stub_has_supported_sections(tmp_path: Path) -> None:
    result = _write_account_stub(tmp_path, "Acme Corp")

    assert result is None
    content = (tmp_path / "accounts/acme-corp/account.md").read_text(encoding="utf-8")
    assert yaml.safe_load(content.split("---", 2)[1]) == {"account": "Acme Corp"}
    assert re.findall(r"^## (.+)$", content, re.MULTILINE) == ["Overview", "Key Contacts", "Qualification"]
    assert "Salesforce ClosePlan is the authoritative source." in content


def test_account_stub_contact_table_has_supported_columns(tmp_path: Path) -> None:
    result = _write_account_stub(tmp_path, "Acme Corp")

    assert result is None
    content = (tmp_path / "accounts/acme-corp/account.md").read_text(encoding="utf-8")
    assert "| Name | Title | Role | Email |" in content
    assert extract_from_account_md(tmp_path / "accounts/acme-corp") == []


def test_discovery_parses_populated_stakeholder_table(fictional_account: Path) -> None:
    contacts = extract_from_account_md(fictional_account)

    assert len(contacts) == 2
    assert [(contact["full_name"], contact["title"], contact["sf_role"], contact["email"]) for contact in contacts] == [
        ("Jane Example", "CFO", "Economic Buyer", "jane@example.com"),
        ("Alex Example", "Architect", "Technical Buyer", "alex@example.com"),
    ]
    assert all(contact["account"] == "acme-corp" and contact["source"] == "account-file" for contact in contacts)


def test_coverage_gaps_table_not_parsed_as_contacts(fictional_account: Path) -> None:
    contacts = extract_from_account_md(fictional_account)

    assert len(contacts) == 2
    assert {contact["full_name"] for contact in contacts} == {"Jane Example", "Alex Example"}
    assert "Champion" in (fictional_account / "account.md").read_text(encoding="utf-8")


def test_account_creator_preserves_operator_authored_record(fictional_account: Path) -> None:
    path = fictional_account / "account.md"
    original = path.read_bytes()

    result = _write_account_stub(fictional_account.parents[1], "Acme Corp")

    assert result is None
    assert path.read_bytes() == original
    assert (fictional_account / "pursuits").is_dir()


@pytest.mark.parametrize(
    "content",
    [None, "# Account\n\n## Notes\nNo stakeholder section.\n", "## Stakeholder Map\n\n| Unknown |\n|---------|\n"],
    ids=["missing-record", "missing-section", "malformed-table"],
)
def test_discovery_returns_empty_for_unusable_account_record(tmp_path: Path, content: str | None) -> None:
    account = tmp_path / "acme-corp"
    account.mkdir()
    if content is not None:
        (account / "account.md").write_text(content, encoding="utf-8")

    contacts = extract_from_account_md(account)

    assert contacts == []


def test_discovery_parses_supported_stakeholder_list(tmp_path: Path) -> None:
    account = tmp_path / "acme-corp"
    account.mkdir()
    (account / "account.md").write_text(
        "## Stakeholder Map\n\n- **Jane Example** (CFO) — jane@example.com\n", encoding="utf-8"
    )

    contacts = extract_from_account_md(account)

    assert len(contacts) == 1
    assert contacts[0]["full_name"] == "Jane Example"
    assert contacts[0]["title"] == "CFO"
    assert contacts[0]["email"] == "jane@example.com"
