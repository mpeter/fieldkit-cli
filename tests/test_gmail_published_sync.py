"""Published-generation orchestration for Gmail provider pages."""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from fieldkit.errors import (
    GmailAuthError,
    GmailSyncPartialError,
    GmailSyncRestartRequiredError,
    SQLiteSnapshotError,
)
from fieldkit.gmail import sync_engine
from fieldkit.gmail.batch import BatchFetchResult, SyncSummary
from fieldkit.gmail.publication import (
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
    publication_root_for,
)
from fieldkit.gmail.sync_store import sync_get, sync_set
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit


def _message(message_id: str = "message-1") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "thread_id": "thread-1",
        "from_addr": "alice@example.com",
        "to_addr": "bob@acme-corp.example.com",
        "cc_addr": "",
        "subject": "Planning",
        "date_str": "2026-09-01",
        "date_epoch": 1788220800,
        "labels": '["INBOX"]',
        "body_plain": "Fictional fixture",
        "body_html": "",
        "size_bytes": 20,
        "snippet": "Planning",
        "attachments": [],
    }


def test_complete_provider_page_publishes_rows_and_checkpoint_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    monkeypatch.setattr(sync_engine, "list_messages", lambda *args, **kwargs: ([{"id": "message-1"}], None))
    monkeypatch.setattr(
        sync_engine,
        "fetch_messages_batch",
        lambda *args, **kwargs: BatchFetchResult(messages=[_message()]),
    )

    outcome, summary = sync_engine._scan_published_messages(
        object(),
        database,
        query=None,
        page_token_key=sync_engine._FULL_PAGE_TOKEN_KEY,
        count_key=sync_engine._FULL_COUNT_KEY,
        max_messages=0,
        exhausted_key=sync_engine._FULL_SCAN_EXHAUSTED_KEY,
    )

    assert outcome == "exhausted"
    assert summary.added == 1
    with open_gmail_publication(database) as connection:
        assert connection.execute("SELECT subject FROM messages").fetchone()[0] == "Planning"
        assert (
            connection.execute("SELECT value FROM sync_state WHERE key = ?", (sync_engine._FULL_COUNT_KEY,)).fetchone()[
                0
            ]
            == "1"
        )
        assert (
            connection.execute(
                "SELECT value FROM sync_state WHERE key = ?", (sync_engine._FULL_SCAN_EXHAUSTED_KEY,)
            ).fetchone()[0]
            == "true"
        )
        assert connection.execute("SELECT generation FROM _fieldkit_publication").fetchone()[0] == 2


def test_unresolved_provider_page_performs_no_cache_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    monkeypatch.setattr(sync_engine, "list_messages", lambda *args, **kwargs: ([{"id": "message-1"}], "next"))
    monkeypatch.setattr(
        sync_engine,
        "fetch_messages_batch",
        lambda *args, **kwargs: BatchFetchResult(messages=[_message()], unresolved=1),
    )

    outcome, summary = sync_engine._scan_published_messages(
        object(),
        database,
        query=None,
        page_token_key=sync_engine._FULL_PAGE_TOKEN_KEY,
        count_key=sync_engine._FULL_COUNT_KEY,
        max_messages=0,
    )

    assert outcome == "paused"
    assert summary.unresolved == 1
    with open_gmail_publication(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT generation FROM _fieldkit_publication").fetchone()[0] == 1


def test_authentication_failure_precedes_cache_creation(tmp_path: Path) -> None:
    database = tmp_path / "gmail.db"

    def fail_authentication() -> object:
        raise GmailAuthError("fixture authentication failure")

    with pytest.raises(GmailAuthError, match="fixture authentication failure"):
        sync_engine.run_published_sync(
            service_factory=fail_authentication,
            db_path=database,
            full=False,
            since=None,
            max_messages=0,
        )

    assert not database.exists()
    assert not publication_root_for(database).exists()


def test_first_sync_label_failure_precedes_cache_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "gmail.db"

    def fail_labels(_service: object) -> list[tuple[str, str]]:
        raise GmailSyncPartialError("labels unavailable")

    monkeypatch.setattr(sync_engine, "_fetch_labels", fail_labels)

    with pytest.raises(GmailSyncPartialError, match="labels unavailable"):
        sync_engine.run_published_sync(
            service_factory=object,
            db_path=database,
            full=False,
            since=None,
            max_messages=0,
        )

    assert not database.exists()
    assert not publication_root_for(database).exists()


def test_first_message_list_failure_leaves_cache_non_queryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fieldkit.gmail import query_domain

    database = tmp_path / "gmail.db"
    monkeypatch.setattr(sync_engine, "_fetch_labels", lambda _service: [])
    monkeypatch.setattr(sync_engine, "_capture_history_id", lambda _service: "100")

    def fail_list(*_args: object, **_kwargs: object) -> tuple[list[dict[str, object]], str | None]:
        raise GmailSyncPartialError("message list unavailable")

    monkeypatch.setattr(sync_engine, "list_messages", fail_list)

    with pytest.raises(GmailSyncPartialError, match="message list unavailable"):
        sync_engine.run_published_sync(
            service_factory=object,
            db_path=database,
            full=False,
            since=None,
            max_messages=0,
        )

    with pytest.raises(GmailSyncPartialError, match="not ready"):
        query_domain.connect(database)


def test_unmanaged_existing_cache_requires_explicit_import_after_authentication(tmp_path: Path) -> None:
    database = tmp_path / "gmail.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.close()
    calls = 0

    def authenticate() -> object:
        nonlocal calls
        calls += 1
        return object()

    with pytest.raises(SQLiteSnapshotError, match="requires explicit import"):
        sync_engine.run_published_sync(
            service_factory=authenticate,
            db_path=database,
            full=False,
            since=None,
            max_messages=0,
        )

    assert calls == 1
    assert not publication_root_for(database).exists()


def test_malformed_profile_response_is_a_typed_partial_failure() -> None:
    class _Request:
        def execute(self) -> list[object]:
            return []

    class _Users:
        def getProfile(self, **kwargs: object) -> _Request:
            del kwargs
            return _Request()

    class _Service:
        def users(self) -> _Users:
            return _Users()

    with pytest.raises(GmailSyncPartialError, match="profile synchronization returned invalid data"):
        sync_engine._capture_history_id(_Service())


def test_malformed_history_page_performs_no_cache_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    monkeypatch.setattr(
        sync_engine,
        "_history_page",
        lambda *args, **kwargs: {
            "history": [{"messagesDeleted": [{"message": {"id": 7}}]}],
            "historyId": "101",
        },
    )

    with pytest.raises(GmailSyncPartialError, match="history synchronization returned invalid data"):
        sync_engine._sync_history_pages(
            object(),
            database,
            start_history_id="100",
            page_token_key=sync_engine._INCREMENTAL_PAGE_TOKEN_KEY,
            latest_history_key=sync_engine._INCREMENTAL_LATEST_HISTORY_KEY,
            publish_global_checkpoint=True,
        )

    with open_gmail_publication(database) as connection:
        assert connection.execute("SELECT generation FROM _fieldkit_publication").fetchone()[0] == 1


def test_label_failure_log_excludes_provider_payload(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.WARNING, logger="gmail-sync")

    def fail(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("private provider payload")

    monkeypatch.setattr(sync_engine, "_api_call_with_retry", fail)

    with pytest.raises(GmailSyncPartialError, match="no label changes were published"):
        sync_engine._fetch_labels(object())

    assert "Gmail label synchronization failed" in caplog.text
    assert "private provider payload" not in caplog.text


def _set_state(database: Path, **values: str | None) -> None:
    def mutation(connection: SQLiteMutationConnection) -> None:
        for key, value in values.items():
            sync_set(connection, key, value)

    apply_gmail_page(database, mutation)


def test_incremental_pages_publish_final_provider_history_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    _set_state(database, initial_sync_complete="true", last_history_id="100")
    pages = iter(
        [
            {"history": [], "historyId": "101", "nextPageToken": "page-2"},
            {"history": [], "historyId": "102"},
        ]
    )
    monkeypatch.setattr(sync_engine, "_history_page", lambda *args, **kwargs: next(pages))

    summary = sync_engine._run_published_incremental_sync(object(), database, "100")

    assert summary == SyncSummary()
    with open_gmail_publication(database) as connection:
        assert sync_get(connection, "last_history_id") == "102"
        assert sync_get(connection, sync_engine._INCREMENTAL_PAGE_TOKEN_KEY) is None
        assert sync_get(connection, sync_engine._INCREMENTAL_LATEST_HISTORY_KEY) is None


def test_incremental_unresolved_page_preserves_checkpoint_and_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    _set_state(database, initial_sync_complete="true", last_history_id="100")
    monkeypatch.setattr(
        sync_engine,
        "_history_page",
        lambda *args, **kwargs: {
            "history": [{"messagesAdded": [{"message": {"id": "message-1", "labelIds": ["INBOX"]}}]}],
            "historyId": "101",
        },
    )
    monkeypatch.setattr(
        sync_engine,
        "fetch_messages_batch",
        lambda *args, **kwargs: BatchFetchResult(messages=[], unresolved=1),
    )

    summary = sync_engine._run_published_incremental_sync(object(), database, "100")

    assert summary.unresolved == 1
    with open_gmail_publication(database) as connection:
        assert sync_get(connection, "last_history_id") == "100"
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_since_sync_uses_inclusive_utc_epoch_and_clears_bounded_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    observed: list[tuple[str | None, str | None]] = []

    def list_page(
        _service: object,
        page_token: str | None,
        *,
        query: str | None,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        del max_results
        observed.append((page_token, query))
        return [], None

    monkeypatch.setattr(sync_engine, "list_messages", list_page)

    summary = sync_engine._run_published_since_sync(object(), database, datetime(2026, 6, 1), 0)

    assert summary == SyncSummary()
    assert observed == [("", "after:1780271999")]
    with open_gmail_publication(database) as connection:
        assert sync_get(connection, sync_engine._SINCE_EPOCH_KEY) is None
        assert sync_get(connection, sync_engine._SINCE_PAGE_TOKEN_KEY) is None
        assert sync_get(connection, sync_engine._SINCE_COUNT_KEY) is None


def test_expired_full_replay_publishes_restart_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    _set_state(
        database,
        full_sync_requested="true",
        full_sync_start_history_id="100",
        full_scan_exhausted="true",
        last_page_token="old-page",
        messages_synced="9",
    )
    monkeypatch.setattr(sync_engine, "_history_page", lambda *args, **kwargs: None)
    monkeypatch.setattr(sync_engine, "_capture_history_id", lambda _service: "200")

    with pytest.raises(GmailSyncRestartRequiredError, match="retry to restart"):
        sync_engine._run_published_full_sync(object(), database, max_messages=0, forced=True)

    with open_gmail_publication(database) as connection:
        assert sync_get(connection, sync_engine._FORCED_HISTORY_KEY) == "200"
        assert sync_get(connection, sync_engine._FULL_PAGE_TOKEN_KEY) == ""
        assert sync_get(connection, sync_engine._FULL_COUNT_KEY) == "0"
        assert sync_get(connection, "initial_sync_complete") is None
