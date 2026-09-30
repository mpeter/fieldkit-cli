"""Tests for gmail sync checkpoint (resume) behaviour.

The checkpoint mechanism uses two sync_state keys:
  - last_page_token: the Gmail API page token to resume from (empty string = "done")
  - messages_synced:  running count of messages inserted this run

These tests exercise the sync state helpers and verify that published page
generations read and write checkpoint state without network access.
"""

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.gmail import sync_engine as sync
from fieldkit.gmail.batch import BatchFetchResult
from fieldkit.gmail.publication import apply_gmail_page, initialize_gmail_publication, open_gmail_publication
from fieldkit.gmail.sync_store import sync_get, sync_set
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEMA_SQL = (Path(__file__).parent.parent / "src" / "fieldkit" / "gmail" / "schema.sql").read_text()


def make_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a fresh in-memory-style DB in tmp_path with the gmail schema."""
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _managed_db(tmp_path: Path) -> Path:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    return database


def _set_managed_state(database: Path, values: dict[str, str | None]) -> None:
    def mutation(connection: SQLiteMutationConnection) -> None:
        for key, value in values.items():
            sync_set(connection, key, value)

    apply_gmail_page(database, mutation)


def _stub_message(msg_id: str = "msg1") -> dict[str, Any]:
    """Return a minimal structured message dict (post-fetch_message shape)."""
    return {
        "message_id": msg_id,
        "thread_id": f"thread_{msg_id}",
        "from_addr": "sender@example.com",  # pii-guard: ignore
        "to_addr": "me@example.com",  # pii-guard: ignore
        "cc_addr": "",
        "subject": f"Subject {msg_id}",
        "date_str": "Mon, 1 Jan 2024 00:00:00 +0000",
        "date_epoch": 1704067200,
        "labels": '["INBOX"]',
        "body_plain": "Hello",
        "body_html": "",
        "size_bytes": 100,
        "snippet": "Hello",
        "attachments": [],
    }


# ---------------------------------------------------------------------------
# sync_get / sync_set
# ---------------------------------------------------------------------------


# ── TestSyncStateHelpers (flattened) ────────────────────────────────────────


def test_sync_state_helpers_get_missing_key_returns_none(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    assert sync_get(conn, "nonexistent") is None


def test_sync_state_helpers_set_then_get_returns_value(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync_set(conn, "last_page_token", "abc123")
    conn.commit()
    assert sync_get(conn, "last_page_token") == "abc123"


def test_sync_state_helpers_set_none_stores_none(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync_set(conn, "last_page_token", None)
    conn.commit()
    row = conn.execute("SELECT value FROM sync_state WHERE key='last_page_token'").fetchone()
    assert row is not None
    assert row[0] is None


def test_sync_state_helpers_overwrite_existing_key(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync_set(conn, "messages_synced", "10")
    conn.commit()
    sync_set(conn, "messages_synced", "20")
    conn.commit()
    assert sync_get(conn, "messages_synced") == "20"


# ---------------------------------------------------------------------------
# Checkpoint written after first batch
# ---------------------------------------------------------------------------


# ── TestCheckpointWrittenAfterBatch (flattened) ─────────────────────────────


def test_checkpoint_written_after_batch_checkpoint_saved_after_first_batch(tmp_path: Path) -> None:
    """After processing a page with a next_page_token, the token is persisted."""
    db_path = tmp_path / "gmail.db"
    _managed_db(tmp_path)

    # list_messages returns one page with a continuation token, then the run hits max_messages
    page1_stubs = [{"id": "msg1", "threadId": "thread1"}]
    # fetch_messages_batch returns a list of structured message dicts
    stub_msg = _stub_message("msg1")

    with (
        patch.object(sync, "list_messages", return_value=(page1_stubs, "TOKEN_PAGE2")),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync._scan_published_messages(
            MagicMock(),
            db_path,
            query=None,
            page_token_key=sync._FULL_PAGE_TOKEN_KEY,
            count_key=sync._FULL_COUNT_KEY,
            max_messages=1,
        )

    # main() closes conn; reopen to read persisted state
    with open_gmail_publication(db_path) as conn2:
        saved_token = sync_get(conn2, "last_page_token")
        synced_count = sync_get(conn2, "messages_synced")

    assert saved_token == "TOKEN_PAGE2", f"Expected TOKEN_PAGE2, got {saved_token!r}"
    assert synced_count is not None
    assert int(synced_count) >= 1


# ---------------------------------------------------------------------------
# Resume run starts from saved page token
# ---------------------------------------------------------------------------


# ── TestResumeFromCheckpoint (flattened) ────────────────────────────────────


def test_resume_from_checkpoint_resume_passes_saved_token_to_list_messages(tmp_path: Path) -> None:
    """list_messages must be called with the saved page_token on resume."""
    database = _managed_db(tmp_path)

    # Pre-seed a checkpoint
    _set_managed_state(database, {"last_page_token": "RESUME_TOKEN", "messages_synced": "5"})

    stub_msg = _stub_message("msg_resume")
    mock_list = MagicMock(return_value=([{"id": "msg_resume", "threadId": "t1"}], None))

    with (
        patch.object(sync, "list_messages", mock_list),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync._scan_published_messages(
            MagicMock(),
            database,
            query=None,
            page_token_key=sync._FULL_PAGE_TOKEN_KEY,
            count_key=sync._FULL_COUNT_KEY,
            max_messages=0,
        )

    # First call to list_messages must pass the resume token
    first_call_token = mock_list.call_args_list[0][0][1]  # positional arg 1 = page_token
    assert first_call_token == "RESUME_TOKEN", (
        f"Expected list_messages to resume from RESUME_TOKEN, got {first_call_token!r}"
    )


def test_resume_from_checkpoint_resume_increments_from_prior_count(tmp_path: Path) -> None:
    """messages_synced counter resumes from the prior checkpoint value."""
    db_path = tmp_path / "gmail.db"
    _managed_db(tmp_path)
    _set_managed_state(db_path, {"last_page_token": "RESUME_TOKEN", "messages_synced": "42"})

    stub_msg = _stub_message("msg_new")
    mock_service = MagicMock()
    mock_service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "500"}

    with (
        patch.object(sync, "list_messages", return_value=([{"id": "msg_new", "threadId": "t1"}], None)),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync._scan_published_messages(
            mock_service,
            db_path,
            query=None,
            page_token_key=sync._FULL_PAGE_TOKEN_KEY,
            count_key=sync._FULL_COUNT_KEY,
            max_messages=0,
        )

    # main() closes conn; reopen to read persisted state
    with open_gmail_publication(db_path) as conn2:
        final_count = sync_get(conn2, "messages_synced")
    assert final_count == "43"


# ---------------------------------------------------------------------------
# Completed run clears checkpoint
# ---------------------------------------------------------------------------


# ── TestCheckpointClearedOnCompletion (flattened) ───────────────────────────


def test_checkpoint_cleared_on_completion_checkpoint_cleared_after_full_completion(tmp_path: Path) -> None:
    """The page checkpoint is cleared when sync reaches the end."""
    db_path = tmp_path / "gmail.db"
    _managed_db(tmp_path)

    stub_msg = _stub_message("msg_last")

    mock_service = MagicMock()
    mock_service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "9999"}

    with (
        patch.object(sync, "list_messages", return_value=([{"id": "msg_last", "threadId": "t1"}], None)),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
        patch.object(sync, "_history_page", return_value={"historyId": "9999"}),
    ):
        sync._run_published_full_sync(mock_service, db_path, max_messages=0, forced=False)

    # main() closes conn; reopen to read persisted state
    with open_gmail_publication(db_path) as conn2:
        token = sync_get(conn2, "last_page_token")
        complete = sync_get(conn2, "initial_sync_complete")

    assert token is None
    assert complete == "true", f"Expected 'true', got {complete!r}"


# ---------------------------------------------------------------------------
# Full sync (no --max-messages) ignores checkpoint
# ---------------------------------------------------------------------------


# ── TestFullSyncIgnoresCheckpoint (flattened) ───────────────────────────────


def test_full_sync_ignores_checkpoint_no_max_messages_still_reads_checkpoint_for_resume(tmp_path: Path) -> None:
    """Even without --max-messages, a saved checkpoint is honoured (partial run resume)."""
    database = _managed_db(tmp_path)

    # Seed a partial checkpoint (no max_messages was set on prior run, but interrupted)
    _set_managed_state(database, {"last_page_token": "PARTIAL_TOKEN", "messages_synced": "10"})

    stub_msg = _stub_message("msg_cont")
    mock_list = MagicMock(return_value=([{"id": "msg_cont", "threadId": "t1"}], None))
    mock_service = MagicMock()
    mock_service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "1000"}

    with (
        patch.object(sync, "list_messages", mock_list),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync._scan_published_messages(
            mock_service,
            database,
            query=None,
            page_token_key=sync._FULL_PAGE_TOKEN_KEY,
            count_key=sync._FULL_COUNT_KEY,
            max_messages=0,
        )

    # Should have started from PARTIAL_TOKEN
    first_call_token = mock_list.call_args_list[0][0][1]
    assert first_call_token == "PARTIAL_TOKEN"


def _service_with_history(profile_id: str = "100", replay_id: str = "101") -> MagicMock:
    service = MagicMock()
    service.users().getProfile().execute.return_value = {"historyId": profile_id}
    service.users().history().list().execute.return_value = {"historyId": replay_id}
    service.reset_mock()
    return service


_GLOBAL_SYNC_STATE = {
    "initial_sync_complete": "true",
    "last_history_id": "900",
    "last_page_token": "full-page",
    "messages_synced": "44",
    "full_sync_requested": "true",
    "full_sync_start_history_id": "800",
    "full_scan_exhausted": "true",
}


def _seed_global_sync_state(conn: sqlite3.Connection) -> None:
    for key, value in _GLOBAL_SYNC_STATE.items():
        sync_set(conn, key, value)
    conn.commit()


def _assert_global_sync_state(conn: sqlite3.Connection) -> None:
    assert {key: sync_get(conn, key) for key in _GLOBAL_SYNC_STATE} == _GLOBAL_SYNC_STATE
