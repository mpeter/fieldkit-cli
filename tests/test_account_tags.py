"""Unit tests for published Gmail account-tag associations."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.account_tags import cli as account_tags_cli
from fieldkit.errors import GmailSyncPartialError
from fieldkit.gmail import account_tags
from fieldkit.gmail.publication import (
    GMAIL_QUERY_READY_KEY,
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
    publication_root_for,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit


def _make_db(
    tmp_path: Path,
    threads: list[str],
    messages: list[tuple[str, str, list[str]]],
    label_rows: list[tuple[str, str]],
) -> Path:
    """Create a temp SQLite DB with schema and seeded rows."""
    db_path = tmp_path / "test.db"
    initialize_gmail_publication(db_path)

    def seed(connection: SQLiteMutationConnection) -> None:
        for thread_id in threads:
            connection.execute(
                "INSERT INTO threads(thread_id, subject, snippet, message_count, updated_at) VALUES (?,?,?,?,?)",
                (thread_id, "", "", 0, "2024-01-01"),
            )
        for message_id, thread_id, labels in messages:
            connection.execute(
                "INSERT INTO messages(message_id, thread_id, labels, synced_at) VALUES (?,?,?,datetime('now'))",
                (message_id, thread_id, json.dumps(labels)),
            )
        for label_id, label_name in label_rows:
            connection.execute(
                "INSERT OR IGNORE INTO labels(label_id, label_name) VALUES (?,?)",
                (label_id, label_name),
            )
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(db_path, seed)
    return db_path


def _get_thread_accounts(db_path: Path) -> list[tuple[str, str]]:
    with open_gmail_publication(db_path) as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute("SELECT thread_id, account FROM thread_accounts ORDER BY thread_id, account")
        ]


def test_basic_ref_tagging(tmp_path):
    """A message with a ref/global-pay label produces (thread_id, global-pay) in thread_accounts."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_globalpay"])],
        label_rows=[("Label_ref_globalpay", "ref/global-pay")],
    )
    account_tags.update_account_tags(db)
    assert _get_thread_accounts(db) == [("t1", "global-pay")]


def test_ref_keep_excluded(tmp_path):
    """A ref/keep label must not produce any thread_accounts row."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_keep"])],
        label_rows=[("Label_ref_keep", "ref/keep")],
    )
    account_tags.update_account_tags(db)
    assert _get_thread_accounts(db) == []


def test_multi_account_tagging(tmp_path):
    """A message tagged with two ref/* labels produces two rows."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_globalpay", "Label_ref_acme-bank"])],
        label_rows=[
            ("Label_ref_globalpay", "ref/global-pay"),
            ("Label_ref_acme-bank", "ref/acme-bank"),
        ],
    )
    account_tags.update_account_tags(db)
    assert set(_get_thread_accounts(db)) == {("t1", "global-pay"), ("t1", "acme-bank")}


def test_no_ref_labels(tmp_path):
    """A message with only a system label (INBOX) produces no thread_accounts rows."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["INBOX"])],
        label_rows=[("INBOX", "INBOX")],
    )
    account_tags.update_account_tags(db)
    assert _get_thread_accounts(db) == []


def test_idempotent_upsert(tmp_path):
    """Calling update_account_tags twice must not duplicate rows (composite PK)."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_globalpay"])],
        label_rows=[("Label_ref_globalpay", "ref/global-pay")],
    )
    account_tags.update_account_tags(db)
    account_tags.update_account_tags(db)
    assert len(_get_thread_accounts(db)) == 1


def test_filtered_update_removes_stale_target_and_preserves_other_accounts(tmp_path: Path) -> None:
    """A filtered reconciliation replaces only the selected account's rows."""
    db = _make_db(
        tmp_path,
        threads=["t-acme", "t-global"],
        messages=[
            ("m-acme", "t-acme", ["label-acme"]),
            ("m-global", "t-global", ["label-global"]),
        ],
        label_rows=[
            ("label-acme", "ref/acme-corp"),
            ("label-global", "ref/global-pay"),
        ],
    )
    account_tags.update_account_tags(db)

    def remove_acme_label(connection: SQLiteMutationConnection) -> None:
        connection.execute("UPDATE messages SET labels = '[]' WHERE message_id = 'm-acme'")

    apply_gmail_page(db, remove_acme_label)
    result = account_tags.update_account_tags(db, account_filter="acme-corp")

    assert result.associations == 0
    assert result.removed == 1
    assert _get_thread_accounts(db) == [("t-global", "global-pay")]


def test_unfiltered_update_reconciles_reassigned_labels(tmp_path: Path) -> None:
    """A full reconciliation removes the old account when a thread is reassigned."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["label-acme"])],
        label_rows=[
            ("label-acme", "ref/acme-corp"),
            ("label-global", "ref/global-pay"),
        ],
    )
    account_tags.update_account_tags(db)

    def reassign_label(connection: SQLiteMutationConnection) -> None:
        connection.execute("UPDATE messages SET labels = '[\"label-global\"]' WHERE message_id = 'm1'")

    apply_gmail_page(db, reassign_label)
    result = account_tags.update_account_tags(db)

    assert result.account_threads == (("global-pay", 1),)
    assert result.associations == 1
    assert result.removed == 1
    assert _get_thread_accounts(db) == [("t1", "global-pay")]


def test_empty_valid_selection_reports_no_changes(tmp_path: Path) -> None:
    """A ready cache with no selected labels is a valid empty reconciliation."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["INBOX"])],
        label_rows=[("INBOX", "INBOX")],
    )

    result = account_tags.update_account_tags(db, account_filter="acme-corp")

    assert result.account_threads == ()
    assert result.associations == 0
    assert result.removed == 0
    assert _get_thread_accounts(db) == []


def test_cli_reports_removed_stale_associations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter must not report a stale-table no-op after reconciliation."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["label-acme"])],
        label_rows=[("label-acme", "ref/acme-corp")],
    )
    account_tags.update_account_tags(db)

    def remove_label(connection: SQLiteMutationConnection) -> None:
        connection.execute("UPDATE messages SET labels = '[]' WHERE message_id = 'm1'")

    apply_gmail_page(db, remove_label)
    monkeypatch.setattr("fieldkit.config.get_account_names", lambda: ["acme-corp"])
    result = CliRunner().invoke(
        account_tags_cli,
        ["--db", str(db), "--account", "acme-corp", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed"] == 1
    assert _get_thread_accounts(db) == []


def test_mixed_keep_and_ref(tmp_path):
    """When a message has both ref/keep and ref/global-pay, only ref/global-pay should be tagged."""
    db = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[("m1", "t1", ["Label_ref_keep", "Label_ref_globalpay"])],
        label_rows=[
            ("Label_ref_keep", "ref/keep"),
            ("Label_ref_globalpay", "ref/global-pay"),
        ],
    )
    account_tags.update_account_tags(db)
    assert _get_thread_accounts(db) == [("t1", "global-pay")]


def test_account_tags_publishes_the_associations_as_a_new_generation(tmp_path: Path) -> None:
    """Tag writes and the generation receipt become visible atomically."""
    from fieldkit.gmail.account_tags import update_account_tags
    from fieldkit.gmail.publication import (
        GMAIL_QUERY_READY_KEY,
        apply_gmail_page,
        initialize_gmail_publication,
        open_gmail_publication,
    )
    from fieldkit.sqlite_publication import SQLiteMutationConnection

    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)

    def seed(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT INTO threads(thread_id, subject, message_count) VALUES (?, ?, ?)",
            ("thread-1", "Fictional planning", 1),
        )
        connection.execute(
            "INSERT INTO labels(label_id, label_name) VALUES (?, ?)",
            ("label-acme", "ref/acme-corp"),
        )
        connection.execute(
            """
            INSERT INTO messages(message_id, thread_id, labels, synced_at)
            VALUES (?, ?, ?, ?)
            """,
            ("message-1", "thread-1", '["label-acme"]', "2026-09-28T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    initial = apply_gmail_page(database, seed)

    result = update_account_tags(database, account_filter="acme-corp")

    assert result.associations == 1
    assert result.account_threads == (("acme-corp", 1),)
    with open_gmail_publication(database) as connection:
        assert connection.execute("SELECT generation FROM _fieldkit_publication").fetchone()[0] == (
            initial.generation + 1
        )
        assert [tuple(row) for row in connection.execute("SELECT thread_id, account FROM thread_accounts")] == [
            ("thread-1", "acme-corp")
        ]


def test_account_tags_refuses_a_cache_without_a_complete_first_page(tmp_path: Path) -> None:
    """A schema-only generation cannot be mistaken for an empty tagged cache."""
    database = tmp_path / "gmail.db"
    initial = initialize_gmail_publication(database)

    with pytest.raises(GmailSyncPartialError, match="not ready"):
        account_tags.update_account_tags(database)

    root = publication_root_for(database)
    assert json.loads((root / "state.json").read_text(encoding="utf-8"))["status"] == "updating"
    assert json.loads((root / "receipt.json").read_text(encoding="utf-8"))["generation"] == initial.generation


def test_sync_account_tags_and_enrich_share_one_published_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each workflow step observes the generation published by the previous step."""
    from fieldkit.commands.gmail import enrich_pursuits
    from fieldkit.gmail import sync_engine
    from fieldkit.gmail.batch import BatchFetchResult

    database = tmp_path / "gmail.db"
    accounts_root = tmp_path / "accounts"
    pursuit = accounts_root / "acme-corp" / "pursuits" / "planning.md"
    pursuit.parent.mkdir(parents=True)
    pursuit.write_text("---\nstage: discover\n---\n\n# Planning\n", encoding="utf-8")
    message = {
        "message_id": "message-1",
        "thread_id": "thread-1",
        "from_addr": "alice@example.com",
        "to_addr": "bob@acme-corp.example.com",
        "cc_addr": "",
        "subject": "Planning",
        "date_str": "2026-09-01",
        "date_epoch": 1788220800,
        "labels": '["label-acme"]',
        "body_plain": "Fictional planning fixture",
        "body_html": "",
        "size_bytes": 27,
        "snippet": "Planning",
        "attachments": [],
    }
    monkeypatch.setattr(sync_engine, "_fetch_labels", lambda _service: [("label-acme", "ref/acme-corp")])
    monkeypatch.setattr(sync_engine, "list_messages", lambda *_args, **_kwargs: ([{"id": "message-1"}], None))
    monkeypatch.setattr(
        sync_engine,
        "fetch_messages_batch",
        lambda *_args, **_kwargs: BatchFetchResult(messages=[message]),
    )

    summary = sync_engine.run_published_sync(
        service_factory=object,
        db_path=database,
        full=False,
        since=datetime(2026, 9, 1),
        max_messages=1,
    )
    tag_result = account_tags.update_account_tags(database, account_filter="acme-corp")

    monkeypatch.setattr(enrich_pursuits, "get_accounts_root", lambda: accounts_root)
    monkeypatch.setattr(enrich_pursuits, "get_gmail_db_path", lambda: database)
    monkeypatch.setattr(
        enrich_pursuits,
        "get_accounts_config",
        lambda: {"accounts": {"acme-corp": {"domains": ["acme-corp.example.com"]}}},
    )
    monkeypatch.setattr(enrich_pursuits, "get_accounts_config", lambda **kwargs: {"accounts": {"acme-corp": {}}})
    exit_code = enrich_pursuits._run_enrich(account_slug="acme-corp")

    assert summary.summary.added == 1
    assert tag_result.associations == 1
    assert exit_code == 0
    report = (accounts_root / "acme-corp" / "gmail-intel.md").read_text(encoding="utf-8")
    assert "Gmail Intelligence Report: acme-corp" in report
    assert "Planning" in report
    assert "alice@example.com" in report

    def remove_account_label(connection: SQLiteMutationConnection) -> None:
        connection.execute("UPDATE messages SET labels = '[]' WHERE message_id = 'message-1'")

    apply_gmail_page(database, remove_account_label)
    reconciliation = account_tags.update_account_tags(database, account_filter="acme-corp")
    second_exit_code = enrich_pursuits._run_enrich(account_slug="acme-corp")

    assert reconciliation.associations == 0
    assert reconciliation.removed == 1
    assert second_exit_code == 0
    refreshed_report = (accounts_root / "acme-corp" / "gmail-intel.md").read_text(encoding="utf-8")
    assert "alice@example.com" not in refreshed_report
