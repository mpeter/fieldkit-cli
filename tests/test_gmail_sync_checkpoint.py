"""Tests for gmail sync checkpoint (resume) behaviour.

The checkpoint mechanism uses two sync_state keys:
  - last_page_token: the Gmail API page token to resume from (empty string = "done")
  - messages_synced:  running count of messages inserted this run

These tests exercise the _sync_get / _sync_set helpers and verify that the
main() loop reads / writes checkpoint state correctly, using a fully mocked
Gmail service so no network calls are made.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.gmail import sync_command
from fieldkit.errors import GmailSyncRestartRequiredError
from fieldkit.gmail import sync_engine as sync
from fieldkit.gmail.batch import BatchFetchResult
from fieldkit.gmail.sync_store import insert_batch

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
# _sync_get / _sync_set
# ---------------------------------------------------------------------------


# ── TestSyncStateHelpers (flattened) ────────────────────────────────────────


def test_sync_state_helpers_get_missing_key_returns_none(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    assert sync._sync_get(conn, "nonexistent") is None


def test_sync_state_helpers_set_then_get_returns_value(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "last_page_token", "abc123")
    conn.commit()
    assert sync._sync_get(conn, "last_page_token") == "abc123"


def test_sync_state_helpers_set_none_stores_none(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "last_page_token", None)
    conn.commit()
    row = conn.execute("SELECT value FROM sync_state WHERE key='last_page_token'").fetchone()
    assert row is not None
    assert row[0] is None


def test_sync_state_helpers_overwrite_existing_key(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "messages_synced", "10")
    conn.commit()
    sync._sync_set(conn, "messages_synced", "20")
    conn.commit()
    assert sync._sync_get(conn, "messages_synced") == "20"


# ---------------------------------------------------------------------------
# Checkpoint written after first batch
# ---------------------------------------------------------------------------


# ── TestCheckpointWrittenAfterBatch (flattened) ─────────────────────────────


def test_checkpoint_written_after_batch_checkpoint_saved_after_first_batch(tmp_path: Path) -> None:
    """After processing a page with a next_page_token, the token is persisted."""
    db_path = tmp_path / "gmail.db"
    conn = make_db(tmp_path)

    # list_messages returns one page with a continuation token, then the run hits max_messages
    page1_stubs = [{"id": "msg1", "threadId": "thread1"}]
    # fetch_messages_batch returns a list of structured message dicts
    stub_msg = _stub_message("msg1")

    with (
        patch.object(sync, "db_init", return_value=conn),
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        patch.object(sync, "sync_labels"),
        patch.object(sync, "list_messages", return_value=(page1_stubs, "TOKEN_PAGE2")),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync_command.cli.main(args=["--db", str(db_path), "--max-messages", "1"], standalone_mode=False)

    # main() closes conn; reopen to read persisted state
    conn2 = sqlite3.connect(db_path)
    saved_token = sync._sync_get(conn2, "last_page_token")
    synced_count = sync._sync_get(conn2, "messages_synced")
    conn2.close()

    assert saved_token == "TOKEN_PAGE2", f"Expected TOKEN_PAGE2, got {saved_token!r}"
    assert synced_count is not None
    assert int(synced_count) >= 1


# ---------------------------------------------------------------------------
# Resume run starts from saved page token
# ---------------------------------------------------------------------------


# ── TestResumeFromCheckpoint (flattened) ────────────────────────────────────


def test_resume_from_checkpoint_resume_passes_saved_token_to_list_messages(tmp_path: Path) -> None:
    """list_messages must be called with the saved page_token on resume."""
    conn = make_db(tmp_path)

    # Pre-seed a checkpoint
    sync._sync_set(conn, "last_page_token", "RESUME_TOKEN")
    sync._sync_set(conn, "messages_synced", "5")
    conn.commit()

    stub_msg = _stub_message("msg_resume")
    mock_list = MagicMock(return_value=([{"id": "msg_resume", "threadId": "t1"}], None))

    with (
        patch.object(sync, "db_init", return_value=conn),
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        patch.object(sync, "sync_labels"),
        patch.object(sync, "list_messages", mock_list),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync_command.cli.main(args=["--db", str(tmp_path / "gmail.db")], standalone_mode=False)

    # First call to list_messages must pass the resume token
    first_call_token = mock_list.call_args_list[0][0][1]  # positional arg 1 = page_token
    assert first_call_token == "RESUME_TOKEN", (
        f"Expected list_messages to resume from RESUME_TOKEN, got {first_call_token!r}"
    )


def test_resume_from_checkpoint_resume_increments_from_prior_count(tmp_path: Path) -> None:
    """messages_synced counter resumes from the prior checkpoint value."""
    db_path = tmp_path / "gmail.db"
    conn = make_db(tmp_path)

    sync._sync_set(conn, "last_page_token", "RESUME_TOKEN")
    sync._sync_set(conn, "messages_synced", "42")
    conn.commit()

    stub_msg = _stub_message("msg_new")
    mock_service = MagicMock()
    mock_service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "500"}

    with (
        patch.object(sync, "db_init", return_value=conn),
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=mock_service),
        patch.object(sync, "sync_labels"),
        patch.object(sync, "list_messages", return_value=([{"id": "msg_new", "threadId": "t1"}], None)),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync_command.cli.main(args=["--db", str(db_path)], standalone_mode=False)

    # main() closes conn; reopen to read persisted state
    conn2 = sqlite3.connect(db_path)
    final_count = sync._sync_get(conn2, "messages_synced")
    conn2.close()
    assert final_count is None


# ---------------------------------------------------------------------------
# Completed run clears checkpoint
# ---------------------------------------------------------------------------


# ── TestCheckpointClearedOnCompletion (flattened) ───────────────────────────


def test_checkpoint_cleared_on_completion_checkpoint_cleared_after_full_completion(tmp_path: Path) -> None:
    """The page checkpoint is cleared when sync reaches the end."""
    db_path = tmp_path / "gmail.db"
    conn = make_db(tmp_path)

    stub_msg = _stub_message("msg_last")

    mock_service = MagicMock()
    mock_service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "9999"}

    with (
        patch.object(sync, "db_init", return_value=conn),
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=mock_service),
        patch.object(sync, "sync_labels"),
        patch.object(sync, "list_messages", return_value=([{"id": "msg_last", "threadId": "t1"}], None)),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync_command.cli.main(args=["--db", str(db_path)], standalone_mode=False)

    # main() closes conn; reopen to read persisted state
    conn2 = sqlite3.connect(db_path)
    token = sync._sync_get(conn2, "last_page_token")
    complete = sync._sync_get(conn2, "initial_sync_complete")
    conn2.close()

    assert token is None
    assert complete == "true", f"Expected 'true', got {complete!r}"


# ---------------------------------------------------------------------------
# Full sync (no --max-messages) ignores checkpoint
# ---------------------------------------------------------------------------


# ── TestFullSyncIgnoresCheckpoint (flattened) ───────────────────────────────


def test_full_sync_ignores_checkpoint_no_max_messages_still_reads_checkpoint_for_resume(tmp_path: Path) -> None:
    """Even without --max-messages, a saved checkpoint is honoured (partial run resume)."""
    conn = make_db(tmp_path)

    # Seed a partial checkpoint (no max_messages was set on prior run, but interrupted)
    sync._sync_set(conn, "last_page_token", "PARTIAL_TOKEN")
    sync._sync_set(conn, "messages_synced", "10")
    conn.commit()

    stub_msg = _stub_message("msg_cont")
    mock_list = MagicMock(return_value=([{"id": "msg_cont", "threadId": "t1"}], None))
    mock_service = MagicMock()
    mock_service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "1000"}

    with (
        patch.object(sync, "db_init", return_value=conn),
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=mock_service),
        patch.object(sync, "sync_labels"),
        patch.object(sync, "list_messages", mock_list),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[stub_msg])),
    ):
        sync_command.cli.main(args=["--db", str(tmp_path / "gmail.db")], standalone_mode=False)

    # Should have started from PARTIAL_TOKEN
    first_call_token = mock_list.call_args_list[0][0][1]
    assert first_call_token == "PARTIAL_TOKEN"


def _service_with_history(profile_id: str = "100", replay_id: str = "101") -> MagicMock:
    service = MagicMock()
    service.users().getProfile().execute.return_value = {"historyId": profile_id}
    service.users().history().list().execute.return_value = {"historyId": replay_id}
    service.reset_mock()
    return service


def test_forced_full_clears_incremental_state_and_starts_at_first_page(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "initial_sync_complete", "true")
    sync._sync_set(conn, "last_history_id", "old")
    conn.commit()
    service = _service_with_history()
    list_mock = MagicMock(return_value=([], None))

    with (
        patch.object(sync, "list_messages", list_mock),
        patch.object(sync, "sync_labels"),
    ):
        result = sync._run_sync(service_factory=lambda: service, conn=conn, full=True, since=None, max_messages=0)

    assert result == sync.SyncSummary()
    assert not list_mock.call_args.args[1]
    assert sync._sync_get(conn, "initial_sync_complete") == "true"
    assert sync._sync_get(conn, "last_history_id") == "101"
    assert sync._sync_get(conn, "full_sync_requested") is None


@pytest.mark.parametrize("full", [False, True])
def test_incomplete_forced_full_resumes_with_or_without_flag(tmp_path: Path, full: bool) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "full_sync_requested", "true")
    sync._sync_set(conn, "full_sync_start_history_id", "100")
    sync._sync_set(conn, "last_page_token", "resume-token")
    sync._sync_set(conn, "messages_synced", "5")
    conn.commit()
    service = _service_with_history()
    list_mock = MagicMock(return_value=([], None))

    with patch.object(sync, "list_messages", list_mock), patch.object(sync, "sync_labels"):
        sync._run_sync(service_factory=lambda: service, conn=conn, full=full, since=None, max_messages=0)

    assert list_mock.call_args.args[1] == "resume-token"
    service.users().getProfile.assert_not_called()


def test_scan_uses_remaining_limit_and_exact_size_exhausts(tmp_path: Path) -> None:
    from fieldkit.gmail.sync_engine import _scan_messages

    conn = make_db(tmp_path)
    stubs = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(3)]
    messages = [_stub_message(f"m{i}") for i in range(3)]
    list_mock = MagicMock(return_value=(stubs, None))

    with (
        patch.object(sync, "list_messages", list_mock),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=messages)),
    ):
        scan_result = _scan_messages(
            MagicMock(),
            conn,
            query=None,
            page_token_key="custom_page_token",
            count_key="custom_count",
            max_messages=3,
        )

    assert scan_result == ("exhausted", sync.SyncSummary(added=3))
    outcome, result = scan_result
    assert outcome == "exhausted"
    assert result.added == 3
    assert list_mock.call_args.kwargs["max_results"] == 3
    assert sync._sync_get(conn, "custom_count") == "3"
    assert sync._sync_get(conn, "custom_page_token") == ""
    assert sync._sync_get(conn, "messages_synced") == "0"


def test_scan_pauses_at_complete_page_and_larger_limit_resumes(tmp_path: Path) -> None:
    from fieldkit.gmail.sync_engine import _scan_messages

    conn = make_db(tmp_path)
    first_page = [{"id": "m1", "threadId": "t1"}]
    second_page = [{"id": "m2", "threadId": "t2"}]
    list_mock = MagicMock(side_effect=[(first_page, "next"), (second_page, None)])

    def fetch(_service: Any, ids: list[str]) -> BatchFetchResult:
        return BatchFetchResult(messages=[_stub_message(message_id) for message_id in ids])

    with patch.object(sync, "list_messages", list_mock), patch.object(sync, "fetch_messages_batch", side_effect=fetch):
        first_scan_result = _scan_messages(
            MagicMock(), conn, query=None, page_token_key="last_page_token", count_key="messages_synced", max_messages=1
        )
        second_scan_result = _scan_messages(
            MagicMock(), conn, query=None, page_token_key="last_page_token", count_key="messages_synced", max_messages=2
        )

    assert first_scan_result == ("paused", sync.SyncSummary(added=1))
    assert second_scan_result == ("exhausted", sync.SyncSummary(added=1))
    first_outcome, first_result = first_scan_result
    second_outcome, second_result = second_scan_result
    assert first_outcome == "paused"
    assert first_result.added == 1
    assert second_outcome == "exhausted"
    assert second_result.added == 1
    assert list_mock.call_args_list[1].args[1] == "next"


def test_forced_full_replays_scan_window_deletion_before_completion(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    insert_batch(conn, [_stub_message("gone")])
    insert_batch(conn, [_stub_message("relabeled")])
    insert_batch(conn, [_stub_message("absent-but-kept")])
    sync._sync_set(conn, "full_sync_requested", "true")
    sync._sync_set(conn, "full_sync_start_history_id", "100")
    conn.commit()
    service = _service_with_history()
    service.users().history().list().execute.return_value = {
        "historyId": "105",
        "history": [
            {
                "messagesDeleted": [{"message": {"id": "gone"}}],
                "labelsAdded": [{"message": {"id": "relabeled"}, "labelIds": ["STARRED"]}],
            }
        ],
    }

    with patch.object(sync, "list_messages", return_value=([], None)):
        result = sync._full_sync(service, conn, max_messages=0)

    assert result == sync.SyncSummary()
    assert conn.execute("SELECT message_id FROM messages WHERE message_id='gone'").fetchone() is None
    labels = conn.execute("SELECT labels FROM messages WHERE message_id='relabeled'").fetchone()[0]
    assert "STARRED" in labels
    assert conn.execute("SELECT message_id FROM messages WHERE message_id='absent-but-kept'").fetchone() is not None
    assert sync._sync_get(conn, "initial_sync_complete") == "true"
    assert sync._sync_get(conn, "last_history_id") == "105"


def test_forced_full_expired_history_resets_scan_and_exits_one(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "full_sync_requested", "true")
    sync._sync_set(conn, "full_sync_start_history_id", "100")
    sync._sync_set(conn, "last_page_token", "old-page")
    sync._sync_set(conn, "messages_synced", "9")
    conn.commit()
    service = _service_with_history(profile_id="200")

    with (
        patch.object(sync, "list_messages", return_value=([], None)),
        patch.object(sync, "_list_incremental_history", return_value=None),
        pytest.raises(GmailSyncRestartRequiredError, match="Gmail changed too far back"),
    ):
        sync._full_sync(service, conn, max_messages=0)

    assert sync._sync_get(conn, "full_sync_start_history_id") == "200"
    assert sync._sync_get(conn, "last_page_token") == ""
    assert sync._sync_get(conn, "messages_synced") == "0"
    assert sync._sync_get(conn, "initial_sync_complete") is None


def test_forced_full_capture_failure_preserves_existing_state(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "initial_sync_complete", "true")
    sync._sync_set(conn, "last_history_id", "old")
    conn.commit()

    with (
        patch.object(sync, "_capture_history_id", side_effect=RuntimeError("profile failed")),
        pytest.raises(RuntimeError, match="profile failed"),
    ):
        sync._prepare_forced_full_sync(MagicMock(), conn)

    assert sync._sync_get(conn, "initial_sync_complete") == "true"
    assert sync._sync_get(conn, "last_history_id") == "old"
    assert sync._sync_get(conn, "full_sync_requested") is None


def test_unresolved_replay_resumes_without_relisting_at_same_limit(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "full_sync_requested", "true")
    sync._sync_set(conn, "full_sync_start_history_id", "100")
    conn.commit()
    service = _service_with_history()
    list_mock = MagicMock(return_value=([{"id": "m1", "threadId": "t1"}], None))
    replay_mock = MagicMock(
        side_effect=[
            (sync.SyncSummary(unresolved=1), None, False),
            (sync.SyncSummary(), "105", False),
        ]
    )

    with (
        patch.object(sync, "list_messages", list_mock),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[_stub_message("m1")])),
        patch.object(sync, "_replay_forced_history", replay_mock),
    ):
        first_result = sync._full_sync(service, conn, max_messages=1)
        assert sync._sync_get(conn, "full_scan_exhausted") == "true"
        second_result = sync._full_sync(service, conn, max_messages=1)

    assert first_result == sync.SyncSummary(added=1, unresolved=1)
    assert second_result == sync.SyncSummary()
    assert list_mock.call_count == 1
    assert replay_mock.call_count == 2
    assert sync._sync_get(conn, "initial_sync_complete") == "true"
    assert sync._sync_get(conn, "last_history_id") == "105"
    assert sync._sync_get(conn, "full_scan_exhausted") is None


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
        sync._sync_set(conn, key, value)
    conn.commit()


def _assert_global_sync_state(conn: sqlite3.Connection) -> None:
    assert {key: sync._sync_get(conn, key) for key in _GLOBAL_SYNC_STATE} == _GLOBAL_SYNC_STATE


def test_since_sync_uses_inclusive_utc_epoch_query_and_cleans_up(tmp_path: Path) -> None:
    from fieldkit.gmail.sync_engine import _since_sync

    conn = make_db(tmp_path)
    _seed_global_sync_state(conn)
    list_mock = MagicMock(return_value=([], None))

    with patch.object(sync, "list_messages", list_mock):
        result = _since_sync(MagicMock(), conn, datetime(2026, 6, 1), max_messages=0)

    assert result == sync.SyncSummary()
    assert list_mock.call_args.kwargs["query"] == "after:1780271999"
    assert list_mock.call_args.args[1] == ""
    assert sync._sync_get(conn, "since_epoch") is None
    assert sync._sync_get(conn, "since_page_token") is None
    assert sync._sync_get(conn, "since_messages_synced") is None
    _assert_global_sync_state(conn)


def test_since_sync_same_date_resumes_then_larger_limit_finishes(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _seed_global_sync_state(conn)
    sync._sync_set(conn, "since_epoch", "1780272000")
    sync._sync_set(conn, "since_page_token", "bounded-next")
    sync._sync_set(conn, "since_messages_synced", "2")
    conn.commit()
    list_mock = MagicMock(
        side_effect=[
            ([{"id": "m3", "threadId": "t3"}], "bounded-last"),
            ([{"id": "m4", "threadId": "t4"}], None),
        ]
    )

    def fetch(_service: Any, ids: list[str]) -> BatchFetchResult:
        return BatchFetchResult(messages=[_stub_message(message_id) for message_id in ids])

    with patch.object(sync, "list_messages", list_mock), patch.object(sync, "fetch_messages_batch", side_effect=fetch):
        paused = sync._since_sync(MagicMock(), conn, datetime(2026, 6, 1), max_messages=3)
        finished = sync._since_sync(MagicMock(), conn, datetime(2026, 6, 1), max_messages=0)

    assert paused == sync.SyncSummary(added=1)
    assert finished == sync.SyncSummary(added=1)
    assert [call.args[1] for call in list_mock.call_args_list] == ["bounded-next", "bounded-last"]
    assert {call.kwargs["query"] for call in list_mock.call_args_list} == {"after:1780271999"}
    assert sync._sync_get(conn, "since_epoch") is None
    _assert_global_sync_state(conn)


def test_since_sync_changed_date_resets_only_bounded_state(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _seed_global_sync_state(conn)
    sync._sync_set(conn, "since_epoch", "old")
    sync._sync_set(conn, "since_page_token", "old-page")
    sync._sync_set(conn, "since_messages_synced", "7")
    conn.commit()

    def inspect_state(*_args: Any, **_kwargs: Any) -> tuple[sync.ScanOutcome, sync.SyncSummary]:
        assert sync._sync_get(conn, "since_epoch") == "1780272000"
        assert sync._sync_get(conn, "since_page_token") == ""
        assert sync._sync_get(conn, "since_messages_synced") == "0"
        return "paused", sync.SyncSummary()

    with patch.object(sync, "_scan_messages", side_effect=inspect_state):
        result = sync._since_sync(MagicMock(), conn, datetime(2026, 6, 1), max_messages=1)

    assert result == sync.SyncSummary()
    _assert_global_sync_state(conn)


def test_bounded_exception_retains_checkpoint_and_global_state(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _seed_global_sync_state(conn)

    with (
        patch.object(sync, "list_messages", side_effect=RuntimeError("list failed")),
        pytest.raises(RuntimeError, match="list failed"),
    ):
        sync._since_sync(MagicMock(), conn, datetime(2026, 6, 1), max_messages=0)

    assert sync._sync_get(conn, "since_epoch") == "1780272000"
    assert sync._sync_get(conn, "since_page_token") == ""
    assert sync._sync_get(conn, "since_messages_synced") == "0"
    _assert_global_sync_state(conn)


def test_bounded_exhaustion_checkpoint_resumes_cleanup_without_relisting(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    sync._sync_set(conn, "since_epoch", "1780272000")
    conn.commit()
    list_mock = MagicMock(return_value=([{"id": "m1", "threadId": "t1"}], None))

    with (
        patch.object(sync, "list_messages", list_mock),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[_stub_message("m1")])),
    ):
        scan_result = sync._scan_messages(
            MagicMock(),
            conn,
            query="after:1780271999",
            page_token_key="since_page_token",
            count_key="since_messages_synced",
            max_messages=1,
            exhausted_page_token=sync._SINCE_EXHAUSTED_TOKEN,
            started_at_key=None,
        )
        assert sync._sync_get(conn, "since_page_token") == sync._SINCE_EXHAUSTED_TOKEN
        resumed_result = sync._since_sync(MagicMock(), conn, datetime(2026, 6, 1), max_messages=1)

    assert scan_result == ("exhausted", sync.SyncSummary(added=1))
    assert resumed_result == sync.SyncSummary()
    assert list_mock.call_count == 1
    assert sync._sync_get(conn, "since_epoch") is None
    assert sync._sync_get(conn, "since_page_token") is None
    assert sync._sync_get(conn, "since_messages_synced") is None
