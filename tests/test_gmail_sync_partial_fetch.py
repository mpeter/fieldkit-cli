"""Regression coverage for historic regression Gmail batch-fetch accounting."""

import json
import logging
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


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"id": "example", "threadId": "thread", "payload": {"headers": [{"name": "From"}]}},
        {"id": "example", "threadId": "thread", "payload": {"headers": []}, "labelIds": [7]},
    ],
)
def test_batch_malformed_provider_message_is_typed_unresolved(response: object) -> None:
    result = sync_store.fetch_messages_batch(_service_with_callbacks([(response, None)]), ["example"])

    assert result == BatchFetchResult(messages=[], unresolved=1)


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


def _history_page(history_id: str, *, next_token: str | None = None) -> dict[str, Any]:
    page: dict[str, Any] = {
        "historyId": history_id,
        "history": [{"messagesAdded": [{"message": {"id": history_id, "labelIds": ["INBOX"]}}]}],
    }
    if next_token is not None:
        page["nextPageToken"] = next_token
    return page


def test_cli_mixed_fetch_outcome_exits_partial(tmp_path: Path) -> None:
    summary = SyncSummary(added=1, not_found=1)

    with (
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        patch.object(sync, "run_published_sync", return_value=sync.PublishedSyncResult("full", summary)),
        pytest.raises(GmailSyncPartialError, match="1 added, 1 failed"),
    ):
        sync_command.cli.main(args=["--db", str(tmp_path / "gmail.db")], standalone_mode=False)
