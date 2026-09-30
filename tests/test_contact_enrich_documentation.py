"""Fixed, isolated contact-discovery scenarios for the packaged workflow."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.contact.enrich_cmd import cli
from fieldkit.commands.contact.report_cmd import cli as report_cli
from fieldkit.contact import _enrich_helpers, enrich, report
from fieldkit.enrich import _helpers, _io

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("account,expected_count", [("acme-corp", 1), ("missing", 0)])
def test_documented_local_discovery_is_scoped_and_does_not_generate_web_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: str, expected_count: int
) -> None:
    accounts = tmp_path / "accounts"
    account_dir = accounts / "acme-corp"
    account_dir.mkdir(parents=True)
    (account_dir / "account.md").write_text(
        "# Acme Corp\n\n## Stakeholders\n- **Jane Doe** (CTO) — jane@example.com\n",
        encoding="utf-8",
    )
    output = tmp_path / "contact-enrich"
    output.mkdir()
    monkeypatch.setattr(_enrich_helpers, "get_accounts_root", lambda: accounts)
    monkeypatch.setattr(_enrich_helpers, "load_gmail_cache_contacts", lambda _account: [])
    monkeypatch.setattr(enrich, "enrich_dir", lambda: output)

    result = CliRunner().invoke(cli, ["--discover", "--account", account, "--json"])

    assert result.exit_code == 0
    summary = json.loads(result.output)
    assert summary["total"] == expected_count
    raw = json.loads((output / "contacts-raw.json").read_text(encoding="utf-8"))
    assert len(raw) == expected_count
    assert not (output / "web_search_batch.json").exists()


def test_documented_web_result_merge_adds_only_missing_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "contact-enrich"
    output.mkdir()
    raw_file = output / "contacts-raw.json"
    raw_file.write_text(
        json.dumps([{"full_name": "Jane Doe", "account": "acme-corp", "email": "jane@example.com"}]),
        encoding="utf-8",
    )
    (output / "web-search-results.json").write_text(
        json.dumps(
            [
                {
                    "full_name": "Jane Doe",
                    "account": "acme-corp",
                    "email": "untrusted@example.com",
                    "linkedin_url": "https://www.linkedin.com/in/jane-doe",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_io, "enrich_dir", lambda: output)
    monkeypatch.setattr(enrich, "enrich_dir", lambda: output)

    result = CliRunner().invoke(cli, ["--apply-web", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["updated_fields"] == 1
    merged = json.loads(raw_file.read_text(encoding="utf-8"))
    assert merged[0]["email"] == "jane@example.com"
    assert merged[0]["linkedin_url"] == "https://www.linkedin.com/in/jane-doe"


def test_documented_report_writes_to_isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "contact-enrich"
    output.mkdir()
    contact = {
        "full_name": "Jane Doe",
        "company": "Acme Corp",
        "account": "acme-corp",
        "email": "jane@example.com",
        "email_frequency": 1,
        "source": "account-file",
        "confidence": "LOW",
    }
    (output / "contacts-raw.json").write_text(json.dumps([contact]), encoding="utf-8")
    (output / "contacts-enriched.json").write_text(json.dumps([contact]), encoding="utf-8")
    monkeypatch.setattr(_io, "enrich_dir", lambda: output)
    monkeypatch.setattr(report, "enrich_dir", lambda: output)

    result = CliRunner().invoke(report_cli, ["--json"])

    assert result.exit_code == 0
    rendered = json.loads(result.output)["report"]
    assert "Jane Doe" in rendered
    assert (output / "report.md").read_text(encoding="utf-8") == rendered


@pytest.mark.parametrize(
    "account,has_contacts,expected_count", [(None, False, 0), ("missing", True, 0), ("acme-corp", True, 1)]
)
def test_enrichment_preserves_legacy_and_other_account_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: str | None, has_contacts: bool, expected_count: int
) -> None:
    output = tmp_path / "contact-enrich"
    memory = tmp_path / "memory/personal/contacts"
    output.mkdir()
    memory.mkdir(parents=True)
    legacy = output / "memory"
    legacy.mkdir()
    legacy_file = legacy / "contact_jane_doe.md"
    legacy_file.write_text("Legacy Jane\n", encoding="utf-8")
    other_account = memory / "contact_beta_person.md"
    other_account.write_text("Other account\n", encoding="utf-8")
    tracked = [legacy, legacy_file, other_account]
    before_stats = {path: path.stat() for path in tracked}
    before_bytes = {path: path.read_bytes() for path in [legacy_file, other_account]}
    (output / "contacts-raw.json").write_text(
        json.dumps(
            [
                {
                    "full_name": "Jane Doe",
                    "company": "Acme Corp",
                    "account": "acme-corp",
                    "phone": "555-0100",
                    "source": "account-file",
                }
            ]
            if has_contacts
            else []
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_io, "enrich_dir", lambda: output)
    monkeypatch.setattr(enrich, "enrich_dir", lambda: output)
    monkeypatch.setattr(_io, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(_helpers, "enrich_dir", lambda: output)
    monkeypatch.setattr(_enrich_helpers, "enrich_dir", lambda: output)
    monkeypatch.setattr(_enrich_helpers, "contacts_memory_dir", lambda: memory)
    monkeypatch.setattr(_enrich_helpers, "get_user_email", lambda: None)
    monkeypatch.setattr(_enrich_helpers, "build_domain_account_map", dict)
    monkeypatch.setattr(_enrich_helpers, "get_internal_domains", list)

    result = CliRunner().invoke(cli, ["--json"] + (["--account", account] if account is not None else []))

    assert result.exit_code == 0
    summary = json.loads(result.output)
    assert summary == {"total_enriched": expected_count, "total_failed": 0, "total_raw_contacts": expected_count}
    for path, stat in before_stats.items():
        current = path.stat()
        assert (current.st_ino, current.st_mode, current.st_size, current.st_mtime_ns, current.st_ctime_ns) == (
            stat.st_ino,
            stat.st_mode,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
    for path, content in before_bytes.items():
        assert path.read_bytes() == content
    assert sorted(path.name for path in legacy.iterdir()) == [legacy_file.name]
    assert "Migrated" not in result.output
    if expected_count:
        enriched = json.loads((output / "contacts-enriched.json").read_text(encoding="utf-8"))
        assert enriched[0]["full_name"] == "Jane Doe"
        assert (output / "checkpoint.json").is_file()
        assert len(list(memory.glob("contact_*.md"))) == 2
    else:
        assert not (output / "contacts-enriched.json").exists()
        assert not (output / "checkpoint.json").exists()
