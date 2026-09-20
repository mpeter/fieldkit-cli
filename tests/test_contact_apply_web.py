"""Tests for fieldkit.contact.enrich.apply_web() — web-search-results.json loading and merge."""

import json
from pathlib import Path

import pytest

from fieldkit.contact.enrich import apply_web

pytestmark = pytest.mark.unit


def _write_raw(enrich_dir: Path, contacts: list[dict]) -> None:
    (enrich_dir / "contacts-raw.json").write_text(json.dumps(contacts), encoding="utf-8")


def _write_web_results(enrich_dir: Path, results: list[dict]) -> None:
    (enrich_dir / "web-search-results.json").write_text(json.dumps(results), encoding="utf-8")


def test_apply_web_returns_zero_when_results_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_web() returns an all-zero result when web-search-results.json does not exist."""
    import fieldkit.contact.enrich as _mod

    monkeypatch.setattr(_mod, "enrich_dir", lambda: tmp_path)
    monkeypatch.setattr(_mod, "load_raw_contacts", lambda: [{"full_name": "Alice", "account": "acme-corp"}])

    result = apply_web()

    assert result.web_results_applied == 0
    assert result.updated_fields == 0
    assert result.total_raw_contacts == 1


def test_apply_web_merges_results_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_web() loads web-search-results.json and merges it into raw contacts."""
    import fieldkit.contact.enrich as _mod

    raw = [{"full_name": "Alice", "account": "acme-corp"}]
    web = [{"full_name": "Alice", "account": "acme-corp", "linkedin_url": "https://linkedin.com/in/alice"}]
    _write_web_results(tmp_path, web)

    monkeypatch.setattr(_mod, "enrich_dir", lambda: tmp_path)
    monkeypatch.setattr(_mod, "load_raw_contacts", lambda: raw)

    result = apply_web()

    assert result.web_results_applied == 1
    assert result.updated_fields == 1
    written = json.loads((tmp_path / "contacts-raw.json").read_text())
    assert written[0]["linkedin_url"] == "https://linkedin.com/in/alice"


def test_apply_web_account_filter_restricts_to_matching_web_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--account restricts which web results are applied, without dropping other accounts' raw contacts."""
    import fieldkit.contact.enrich as _mod

    raw = [
        {"full_name": "Alice", "account": "acme-corp"},
        {"full_name": "Bob", "account": "globalpay"},
    ]
    web = [
        {"full_name": "Alice", "account": "acme-corp", "email": "alice@acme-corp.com"},
        {"full_name": "Bob", "account": "globalpay", "email": "bob@globalpay.example.com"},
    ]
    _write_web_results(tmp_path, web)

    monkeypatch.setattr(_mod, "enrich_dir", lambda: tmp_path)
    monkeypatch.setattr(_mod, "load_raw_contacts", lambda: raw)

    result = apply_web(account="acme-corp")

    assert result.web_results_applied == 1
    written = json.loads((tmp_path / "contacts-raw.json").read_text())
    alice = next(c for c in written if c["full_name"] == "Alice")
    bob = next(c for c in written if c["full_name"] == "Bob")
    assert alice["email"] == "alice@acme-corp.com"
    # Bob's raw record is preserved in the write-back even though his web result was filtered out.
    assert "email" not in bob


def test_cli_empty_web_results_message_references_skill_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation change: empty web results message points operators at a doc that exists."""
    from pathlib import Path

    from click.testing import CliRunner

    from fieldkit.commands.contact.enrich_cmd import cli
    from fieldkit.contact.enrich import ApplyWebResult

    doc_path = "src/fieldkit/skills/contact/ops/contact-enrich.md"

    monkeypatch.setattr(
        "fieldkit.commands.contact.enrich_cmd.apply_web",
        lambda account=None: ApplyWebResult(web_results_applied=0, updated_fields=0, total_raw_contacts=0),
    )
    result = CliRunner().invoke(cli, ["--apply-web"])
    assert doc_path in result.output
    # BUG guard: the referenced doc must actually exist, or the message sends
    # operators to a dead path (the implementation change regression this test locks).
    repo_root = Path(__file__).resolve().parent.parent
    assert (repo_root / doc_path).is_file(), f"referenced doc does not exist: {doc_path}"
