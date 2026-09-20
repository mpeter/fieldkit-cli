"""Regression coverage for historic regression Gmail batch-fetch accounting."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from googleapiclient.http import BatchHttpRequest, HttpRequest
from httplib2 import Response

from fieldkit import cli_exit
from fieldkit.commands.gmail import sync_command
from fieldkit.errors import GmailAuthError, GmailSyncPartialError
from fieldkit.gmail import sync_engine as sync
from fieldkit.gmail import sync_store
from fieldkit.gmail.batch import BatchFetchResult, SyncSummary
from fieldkit.gmail.retry import _api_call_with_retry
from fieldkit.gmail.sync_engine import incremental_sync

pytestmark = pytest.mark.unit


def _http_error(status: int, *, reason: str | None = None) -> HttpError:
    response = MagicMock()
    response.status = status
    payload: dict[str, Any] = {"error": {}}
    if reason is not None:
        payload["error"]["errors"] = [{"reason": reason}]
    return HttpError(response, json.dumps(payload).encode())


class _Batch:
    def __init__(self, callbacks: list[tuple[Any, Any]]) -> None:
        self._callbacks = callbacks
        self._registered: list[Any] = []

    def add(self, request: Any, callback: Any) -> None:
        self._registered.append(callback)

    def execute(self) -> None:
        for callback, outcome in zip(self._registered, self._callbacks, strict=True):
            response, exception = outcome
            callback("request", response, exception)


def _service_with_callbacks(callbacks: list[tuple[Any, Any]]) -> MagicMock:
    service = MagicMock()
    service.new_batch_http_request.return_value = _Batch(callbacks)
    return service


def _raw_message(msg_id: str) -> dict[str, Any]:
    return {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "payload": {"headers": []},
        "labelIds": ["INBOX"],
        "snippet": "example",
    }


def _stored_message(msg_id: str) -> dict[str, Any]:
    return sync_store._make_message_dict(_raw_message(msg_id), msg_id)


def _db(tmp_path: Path) -> sqlite3.Connection:
    return sync.db_init(tmp_path / "gmail.db")


def _incremental_db(tmp_path: Path) -> sqlite3.Connection:
    conn = _db(tmp_path)
    sync._sync_set(conn, "last_history_id", "START")
    conn.commit()
    return conn


def test_batch_classifies_success_not_found_and_unresolved() -> None:
    service = _service_with_callbacks(
        [
            (_raw_message("ok"), None),
            (None, _http_error(404)),
            (None, RuntimeError("temporary failure")),
        ]
    )

    result = sync_store.fetch_messages_batch(service, ["ok", "gone", "retry"])

    assert len(result.messages) == 1
    assert result.not_found == 1
    assert result.unresolved == 1


def test_batch_missing_callback_payload_is_unresolved() -> None:
    result = sync_store.fetch_messages_batch(_service_with_callbacks([(None, None)]), ["missing"])

    assert result.unresolved == 1
    assert result.failed == 1


def test_batch_auth_failure_raises_gmail_auth_error() -> None:
    service = _service_with_callbacks([(None, _http_error(401))])

    with pytest.raises(GmailAuthError, match="authentication"):
        sync_store.fetch_messages_batch(service, ["private"])


def test_batch_refresh_failure_raises_gmail_auth_error() -> None:
    batch = BatchHttpRequest()
    service = MagicMock()
    service.new_batch_http_request.return_value = batch
    service.users.return_value.messages.return_value.get.return_value = HttpRequest(
        http=MagicMock(),
        postproc=lambda response, content: {},
        uri="https://gmail.googleapis.com/gmail/v1/users/me/messages/example",
    )

    def execute_batch(http: Any, order: list[str], requests: dict[str, Any]) -> None:
        del http, requests
        batch._responses = {request_id: (Response({"status": "401"}), b"{}") for request_id in order}

    with (
        patch.object(batch, "_execute", side_effect=execute_batch),
        patch.object(
            batch,
            "_refresh_and_apply_credentials",
            side_effect=RefreshError("invalid_grant: Token has been revoked"),
        ) as refresh,
        patch("googleapiclient.http._auth.get_credentials_from_http", return_value=None),
        pytest.raises(GmailAuthError, match="authentication"),
    ):
        sync_store.fetch_messages_batch(service, ["example"])

    refresh.assert_called_once()


def test_batch_quota_failure_is_unresolved() -> None:
    service = _service_with_callbacks([(None, _http_error(403, reason="quotaExceeded"))])

    result = sync_store.fetch_messages_batch(service, ["limited"])

    assert result.unresolved == 1


def test_batch_failure_logs_exclude_message_ids(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="gmail-sync")
    service = _service_with_callbacks([(None, _http_error(404)), (None, RuntimeError("failed"))])

    result = sync_store.fetch_messages_batch(service, ["sensitive-one", "sensitive-two"])

    assert result.failed == 2
    assert "sensitive-one" not in caplog.text
    assert "sensitive-two" not in caplog.text


def test_api_call_with_retry_maps_401_to_gmail_auth_error() -> None:
    def fail() -> None:
        raise _http_error(401)

    with pytest.raises(GmailAuthError, match="authentication"):
        _api_call_with_retry(fail, context="profile")


def test_api_call_with_retry_maps_permanent_403_to_gmail_auth_error() -> None:
    def fail() -> None:
        raise _http_error(403, reason="forbidden")

    with pytest.raises(GmailAuthError, match="access denied"):
        _api_call_with_retry(fail, context="profile")


def test_api_call_with_retry_retries_retryable_refresh_error() -> None:
    retryable = RefreshError("temporarily unavailable", retryable=True)
    call = MagicMock(side_effect=[retryable, {"ok": True}])
    retry_without_wait = _api_call_with_retry.with_policy(  # type: ignore[reportFunctionMemberAccess]
        wait_min=0, wait_max=0
    )

    result = retry_without_wait(call, context="batch.execute")

    assert result == {"ok": True}
    assert call.call_count == 2


def test_partial_exception_maps_to_exit_one() -> None:
    result = cli_exit.handle_cli_exception(GmailSyncPartialError("one message unavailable"))

    assert result == cli_exit.EXIT_PARTIAL


def test_full_sync_unresolved_fetch_retains_current_page(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    sync._sync_set(conn, "last_page_token", "CURRENT")
    conn.commit()
    outcome = BatchFetchResult(messages=[_stored_message("ok")], unresolved=1)

    with (
        patch.object(sync, "list_messages", return_value=([{"id": "ok"}], "NEXT")),
        patch.object(sync, "fetch_messages_batch", return_value=outcome),
    ):
        result = sync._full_sync(MagicMock(), conn, 0)

    assert result.unresolved == 1
    assert sync._sync_get(conn, "last_page_token") == "CURRENT"
    assert sync._sync_get(conn, "initial_sync_complete") is None


def test_full_sync_not_found_advances_to_next_page(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    sync._sync_set(conn, "last_page_token", "CURRENT")
    conn.commit()
    outcome = BatchFetchResult(messages=[], not_found=1)

    with (
        patch.object(sync, "list_messages", side_effect=[([{"id": "gone"}], "NEXT"), ([], None)]),
        patch.object(sync, "fetch_messages_batch", return_value=outcome),
    ):
        result = sync._full_sync(MagicMock(), conn, 1)

    assert result.not_found == 1
    assert sync._sync_get(conn, "last_page_token") == "NEXT"


def test_full_sync_unresolved_fetch_stops_before_later_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path)
    monkeypatch.setattr(sync, "BATCH_SIZE", 1)
    fetch = MagicMock(return_value=BatchFetchResult(messages=[], unresolved=1))

    with (
        patch.object(sync, "list_messages", return_value=([{"id": "first"}, {"id": "later"}], "NEXT")),
        patch.object(sync, "fetch_messages_batch", fetch),
    ):
        result = sync._full_sync(MagicMock(), conn, 0)

    assert result.unresolved == 1
    assert fetch.call_count == 1
    assert sync._sync_get(conn, "last_page_token") in (None, "")


def test_full_sync_second_chunk_auth_retains_current_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path)
    sync._sync_set(conn, "last_page_token", "CURRENT")
    conn.commit()
    monkeypatch.setattr(sync, "BATCH_SIZE", 1)
    fetch = MagicMock(
        side_effect=[
            BatchFetchResult(messages=[_stored_message("first")]),
            GmailAuthError("authentication failed"),
        ]
    )

    with (
        patch.object(sync, "list_messages", return_value=([{"id": "first"}, {"id": "second"}], "NEXT")),
        patch.object(sync, "fetch_messages_batch", fetch),
        pytest.raises(GmailAuthError, match="authentication"),
    ):
        sync._full_sync(MagicMock(), conn, 0)

    assert sync._sync_get(conn, "last_page_token") == "CURRENT"


def test_full_sync_consecutive_capped_runs_advance_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path)
    monkeypatch.setattr(sync, "BATCH_SIZE", 1)
    listed_tokens: list[str | None] = []

    def list_page(
        _service: Any,
        token: str | None = None,
        *,
        query: str | None = None,
        max_results: int = 500,
    ) -> tuple[list[dict[str, str]], str | None]:
        listed_tokens.append(token)
        assert query is None
        assert max_results == 1
        return ([{"id": f"{token or 'start'}-1"}], "P2" if token is None else "P3")

    def fetch(_service: Any, ids: list[str]) -> BatchFetchResult:
        return BatchFetchResult(messages=[_stored_message(ids[0])])

    with (
        patch.object(sync, "list_messages", side_effect=list_page),
        patch.object(sync, "fetch_messages_batch", side_effect=fetch),
    ):
        first = sync._full_sync(MagicMock(), conn, 1)
        second = sync._full_sync(MagicMock(), conn, 2)

    assert first.added == 1
    assert second.added == 1
    assert listed_tokens == [None, "P2"]
    assert sync._sync_get(conn, "last_page_token") == "P3"


def test_full_sync_list_auth_raises_gmail_auth_error(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    with (
        patch.object(sync, "list_messages", side_effect=GmailAuthError("authentication failed")),
        pytest.raises(GmailAuthError, match="authentication"),
    ):
        sync._full_sync(MagicMock(), conn, 0)


def test_label_sync_auth_raises_gmail_auth_error(tmp_path: Path) -> None:
    conn = _db(tmp_path)

    with (
        patch.object(sync, "_api_call_with_retry", side_effect=GmailAuthError("authentication failed")),
        pytest.raises(GmailAuthError, match="authentication"),
    ):
        sync.sync_labels(MagicMock(), conn)


def test_full_sync_profile_auth_raises_gmail_auth_error(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.side_effect = _http_error(401)

    with (
        patch.object(sync, "list_messages", return_value=([{"id": "ok"}], None)),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[_stored_message("ok")])),
        pytest.raises(GmailAuthError, match="authentication"),
    ):
        sync._full_sync(service, conn, 0)


def _history_page(history_id: str, *, next_token: str | None = None) -> dict[str, Any]:
    page: dict[str, Any] = {
        "historyId": history_id,
        "history": [{"messagesAdded": [{"message": {"id": history_id, "labelIds": ["INBOX"]}}]}],
    }
    if next_token is not None:
        page["nextPageToken"] = next_token
    return page


def test_incremental_sync_unresolved_fetch_retains_history_id(tmp_path: Path) -> None:
    conn = _incremental_db(tmp_path)
    service = MagicMock()

    with (
        patch.object(sync, "_api_call_with_retry", return_value=_history_page("H1")),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[], unresolved=1)),
    ):
        result = incremental_sync(service, conn)

    assert result == SyncSummary(unresolved=1)
    assert result.unresolved == 1
    assert conn.execute("SELECT value FROM sync_state WHERE key = 'last_history_id'").fetchone() == ("START",)


def test_incremental_sync_not_found_advances_history_id(tmp_path: Path) -> None:
    stored_conn = _incremental_db(tmp_path)
    conn = MagicMock(wraps=stored_conn)

    with (
        patch.object(sync, "_api_call_with_retry", return_value=_history_page("PROCESSED")),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[], not_found=1)),
    ):
        result = incremental_sync(MagicMock(), conn)

    assert result == SyncSummary(not_found=1)
    assert result.not_found == 1
    conn.commit.assert_called()
    assert stored_conn.execute("SELECT value FROM sync_state WHERE key = 'last_history_id'").fetchone() == (
        "PROCESSED",
    )


def test_incremental_sync_uses_final_history_response_id(tmp_path: Path) -> None:
    conn = _incremental_db(tmp_path)
    pages = [_history_page("H1", next_token="PAGE2"), _history_page("H2")]

    with (
        patch.object(sync, "_api_call_with_retry", side_effect=pages),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[], not_found=1)),
    ):
        result = incremental_sync(MagicMock(), conn)

    assert result == SyncSummary(not_found=2)
    assert result.not_found == 2
    assert conn.execute("SELECT value FROM sync_state WHERE key = 'last_history_id'").fetchone() == ("H2",)


def test_incremental_sync_unresolved_fetch_stops_before_next_page(tmp_path: Path) -> None:
    conn = _incremental_db(tmp_path)
    history = MagicMock(return_value=_history_page("H1", next_token="PAGE2"))

    with (
        patch.object(sync, "_api_call_with_retry", history),
        patch.object(sync, "fetch_messages_batch", return_value=BatchFetchResult(messages=[], unresolved=1)),
    ):
        result = incremental_sync(MagicMock(), conn)

    assert result == SyncSummary(unresolved=1)
    assert result.unresolved == 1
    assert history.call_count == 1
    checkpoint = conn.execute("SELECT value FROM sync_state WHERE key = 'last_history_id'").fetchone()
    assert checkpoint == ("START",)


def test_incremental_missing_history_does_not_query_profile(tmp_path: Path) -> None:
    """Incremental sync never replaces its processed boundary with a profile snapshot."""
    conn = _incremental_db(tmp_path)
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.side_effect = GmailAuthError("must not be called")

    with patch.object(sync, "_api_call_with_retry", return_value={"history": []}):
        result = incremental_sync(service, conn)

    assert result == SyncSummary()
    checkpoint = conn.execute("SELECT value FROM sync_state WHERE key = 'last_history_id'").fetchone()
    assert checkpoint == ("START",)
    service.users.return_value.getProfile.assert_not_called()


def test_missing_history_checkpoint_raises_data_error(tmp_path: Path) -> None:
    conn = _db(tmp_path)

    with pytest.raises(RuntimeError, match="without last_history_id") as exc_info:
        incremental_sync(MagicMock(), conn)

    assert "last_history_id" in str(exc_info.value)


def test_cli_mixed_fetch_outcome_exits_partial(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    summary = SyncSummary(added=1, not_found=1)

    with (
        patch.object(sync, "db_init", return_value=conn),
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        patch.object(sync, "sync_labels"),
        patch.object(sync, "_full_sync", return_value=summary),
        pytest.raises(GmailSyncPartialError, match="1 added, 1 failed"),
    ):
        sync_command.cli.main(args=["--db", str(tmp_path / "gmail.db")], standalone_mode=False)

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
