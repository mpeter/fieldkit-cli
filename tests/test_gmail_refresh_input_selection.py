"""Executable local Gmail refresh outcomes, without provider or installed-artifact claims."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fieldkit.__main__ import main
from fieldkit.config import clear_config_caches
from fieldkit.gmail.discover import clear_gmail_caches, get_gmail_db_path
from fieldkit.gmail.publication import open_gmail_publication, publication_root_for
from tests.documentation_workflow_support import snapshot_workflow
from tests.test_gmail_cache_import import _legacy_cache
from tests.test_gmail_discover import _make_gmail_db

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    (tmp_path / "config").mkdir()
    (tmp_path / "accounts" / "acme" / "pursuits").mkdir(parents=True)
    config = tmp_path / "config.yaml"
    config.write_text(f"fieldkit_home: {tmp_path}\ngmail_db: {tmp_path / 'selected.db'}\n", encoding="utf-8")
    (tmp_path / "config" / "accounts.yaml").write_text(
        "accounts:\n  acme:\n    domains: [acme-corp.example.com]\ninternal_domains: [example.com]\n", encoding="utf-8"
    )
    monkeypatch.setattr("fieldkit.config._loader.CONFIG_PATH", config)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    monkeypatch.delenv("FIELDKIT_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    clear_config_caches()
    clear_gmail_caches()
    yield tmp_path
    clear_config_caches()
    clear_gmail_caches()


def _selected_cache(path: Path) -> None:
    cache = _make_gmail_db(path)
    cache.execute("INSERT INTO labels(label_id, label_name) VALUES ('selected-label', 'ref/acme')")
    cache.execute("INSERT INTO threads(thread_id, subject) VALUES ('selected-thread', 'Selected automation')")
    for index in range(2):
        cache.execute(
            "INSERT INTO messages(message_id, thread_id, from_addr, to_addr, subject, "
            "date_str, date_epoch, labels, body_plain) VALUES (?, 'selected-thread', "
            "'selected@acme-corp.example.com', 'operator@example.com', 'Selected automation', ?, ?, "
            "'[\"selected-label\"]', 'Selected fictional evidence')",
            (f"selected-message-{index}", datetime.now(UTC).date().isoformat(), int(datetime.now(UTC).timestamp())),
        )
    cache.execute(
        "INSERT INTO people(email, display_name, message_count) VALUES "
        "('selected@acme-corp.example.com', 'Selected Fictional Contact', 2)"
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_import_to_different_target_preserves_selection_for_doctor_tags_and_enrichment(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    selected = workspace / "selected.db"
    source = workspace / "legacy.db"
    imported = workspace / "imported.db"
    _selected_cache(selected)
    _legacy_cache(source)
    protected = {path: path.read_bytes() for path in (source, workspace / "config.yaml")}
    selected_before = _snapshot(publication_root_for(selected))
    pursuit = workspace / "accounts" / "acme" / "pursuits" / "automation.md"
    pursuit.write_text("What We're Selling: automation\n", encoding="utf-8")
    other_account = workspace / "accounts" / "other-corp"
    (other_account / "pursuits").mkdir(parents=True)
    (other_account / "pursuits" / "automation.md").write_text("What We're Selling: automation\n", encoding="utf-8")
    other_report = other_account / "gmail-intel.md"
    other_report.write_text("Retained other account report\n", encoding="utf-8")
    (workspace / "config" / "accounts.yaml").write_text(
        "accounts:\n  acme:\n    domains: [acme-corp.example.com]\n"
        "  other-corp:\n    domains: [other-corp.com]\ninternal_domains: [example.com]\n",
        encoding="utf-8",
    )
    clear_config_caches()
    other_before = snapshot_workflow(other_account)

    result = main(["gmail", "import-cache", "--source", str(source), "--db", str(imported), "--json"])
    assert result == 0
    assert json.loads(capsys.readouterr().out)["table_counts"]["messages"] == 1
    assert {path: path.read_bytes() for path in protected} == protected
    assert _snapshot(publication_root_for(selected)) == selected_before
    clear_config_caches()
    clear_gmail_caches()
    assert get_gmail_db_path() == selected
    imported_before = _snapshot(workspace)

    result = main(["doctor", "gmail", "--json"])
    assert result == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["healthy"] is True
    assert doctor["message"].startswith("2 messages,")

    result = main(["gmail", "account-tags", "--json"])
    assert result == 0
    assert json.loads(capsys.readouterr().out)["items"] == [{"account": "acme", "threads": 1}]
    with open_gmail_publication(selected) as connection:
        assert [tuple(row) for row in connection.execute("SELECT thread_id, account FROM thread_accounts")] == [
            ("selected-thread", "acme")
        ]

    result = main(["gmail", "enrich-pursuits", "--account", "acme", "--json"])
    assert result == 0
    report_path = workspace / "accounts" / "acme" / "gmail-intel.md"
    assert json.loads(capsys.readouterr().out)["accounts"] == [
        {"account": "acme", "status": "written", "path": str(report_path)}
    ]
    report = report_path.read_text(encoding="utf-8")
    assert report.strip()
    assert "selected@acme-corp.example.com" in report
    assert "Selected automation" in report
    assert "alice@example.com" not in report
    assert snapshot_workflow(other_account) == other_before
    assert other_report.read_text(encoding="utf-8") == "Retained other account report\n"
    assert {path: path.read_bytes() for path in protected} == protected
    after = _snapshot(workspace)
    assert {name: data for name, data in after.items() if name.startswith("imported")} == {
        name: data for name, data in imported_before.items() if name.startswith("imported")
    }
    selected_publication = str(publication_root_for(selected).relative_to(workspace)) + "/"
    changed = {name for name in after.keys() | imported_before.keys() if after.get(name) != imported_before.get(name)}
    assert changed
    assert all(
        name == "selected.db" or name.startswith(selected_publication) or name == "accounts/acme/gmail-intel.md"
        for name in changed
    )


@pytest.mark.parametrize("existing_report", [False, True])
def test_valid_account_without_pursuits_skips_without_creating_or_replacing_report(
    workspace: Path, capsys: pytest.CaptureFixture[str], existing_report: bool
) -> None:
    report_path = workspace / "accounts" / "acme" / "gmail-intel.md"
    if existing_report:
        report_path.write_text("Retained fictional report\n", encoding="utf-8")
    before = _snapshot(workspace)

    result = main(["gmail", "enrich-pursuits", "--account", "acme", "--json"])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "accounts": [{"account": "acme", "status": "skipped", "path": None}],
        "error": None,
        "requested": "acme",
    }
    assert _snapshot(workspace) == before
    assert report_path.exists() is existing_report
