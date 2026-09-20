"""Unit tests for fieldkit.commands.shadowbot.client.

All tests are @pytest.mark.unit and use tmp_path for filesystem isolation.
No real network calls are made — httpx is mocked at the boundary.
"""

import json
import time
from collections.abc import Iterable
from inspect import signature
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fieldkit.config import TIMEOUT_SHADOWBOT_QUERY
from fieldkit.shadowbot.auth import ShadowbotAuthError
from fieldkit.shadowbot.client import (
    ShadowbotClient,
    ShadowbotQueryError,
    ShadowbotResponse,
    _call_until_deadline,
    _lines_until_deadline,
    _parse_sse_stream,
    query,
)

# ---------------------------------------------------------------------------
# SSE fixture helpers
# ---------------------------------------------------------------------------

# LangGraph wire format: event: values\ndata: {...}\n\n
_AI_MSG_STRING = json.dumps({"messages": [{"type": "ai", "content": "Hello from ShadowBot"}]})
_AI_MSG_LIST = json.dumps(
    {
        "messages": [
            {
                "type": "ai",
                "content": [
                    {"type": "text", "text": "Part one "},
                    {"type": "text", "text": "part two"},
                ],
            }
        ]
    }
)
_HUMAN_MSG = json.dumps({"messages": [{"type": "human", "content": "user question"}]})


def _sse_lines(*events: tuple[str, str]) -> list[str]:
    """Build SSE line list from (event_type, data_json) pairs."""
    lines: list[str] = []
    for event_type, data in events:
        lines.append(f"event: {event_type}")
        lines.append(f"data: {data}")
        lines.append("")
    return lines


def _make_stream_response(
    status_code: int = 200,
    content_type: str = "text/event-stream",
    lines: list[str] | None = None,
) -> MagicMock:
    """Build a mock httpx streaming response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.headers = {"content-type": content_type}
    mock_resp.iter_lines.return_value = iter(lines or [])
    mock_resp.text = ""
    return mock_resp


def _make_thread_response(thread_id: str = "test-thread-id-123") -> MagicMock:
    """Build a mock POST /threads response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"thread_id": thread_id}
    return mock_resp


def _mock_send(mock_client: MagicMock, request: httpx.Request, *, stream: bool = False) -> httpx.Response | MagicMock:
    """Route client-owned sends through the existing HTTP boundary mocks."""
    if stream:
        return mock_client.stream("POST", str(request.url)).__enter__()
    return httpx.post(str(request.url))


@pytest.mark.unit
def test_query_defaults_to_shared_timeout() -> None:
    """Client and module query entry points share the 300-second deadline."""
    client_timeout = signature(ShadowbotClient.query).parameters["timeout"].default
    module_timeout = signature(query).parameters["timeout"].default

    assert client_timeout == TIMEOUT_SHADOWBOT_QUERY
    assert module_timeout == TIMEOUT_SHADOWBOT_QUERY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FAKE_API_BASE = "https://your-shadowbot-host.example.com/sales-assistant"


@pytest.fixture(autouse=True)
def _patch_shadowbot_api_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """org-agnostic-config: get_shadowbot_api_base() now raises ConfigError when absent.
    Patch it for all tests in this file so they don't require a real config.yaml."""
    monkeypatch.setattr("fieldkit.shadowbot.client.get_shadowbot_api_base", lambda: _FAKE_API_BASE)
    for variable in ("SHADOWBOT_STATE_FILE", "SHADOWBOT_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(variable, raising=False)


def _patch_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Patch get_state_dir to return tmp_path."""
    monkeypatch.setattr("fieldkit.shadowbot.state.get_state_dir", lambda: tmp_path)
    return tmp_path


def _write_state_file(tmp_path: Path, thread_id: str) -> None:
    """Write a state file with the given thread_id."""
    state_file = tmp_path / "shadowbot-state.json"
    state_file.write_text(json.dumps({"thread_id": thread_id}), encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_sse_stream tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "variant,lines,expected_content",
    [
        (
            "string_content",
            _sse_lines(("values", _AI_MSG_STRING)),
            "Hello from ShadowBot",
        ),
        (
            "list_content",
            _sse_lines(("values", _AI_MSG_LIST)),
            "Part one part two",
        ),
        (
            "multiple_values",
            _sse_lines(
                ("values", json.dumps({"messages": [{"type": "ai", "content": "first"}]})),
                ("values", json.dumps({"messages": [{"type": "ai", "content": "last"}]})),
            ),
            "last",
        ),
        (
            "skips_heartbeat",
            [": heartbeat", "event: values", f"data: {_AI_MSG_STRING}", ""],
            "Hello from ShadowBot",
        ),
    ],
)
def test_parse_sse_stream_happy_path(variant: str, lines: list[str], expected_content: str) -> None:
    """_parse_sse_stream correctly extracts AI message content in all happy-path variants."""
    result = _parse_sse_stream(iter(lines))
    assert isinstance(result, ShadowbotResponse)
    assert result.content == expected_content


@pytest.mark.unit
def test_parse_sse_stream_skips_malformed_json() -> None:
    """_parse_sse_stream emits UserWarning for malformed JSON and continues parsing."""
    lines = [
        "event: values",
        "data: {not valid json!!!}",
        "",
        "event: values",
        f"data: {_AI_MSG_STRING}",
        "",
    ]

    with pytest.warns(UserWarning, match="malformed JSON"):
        result = _parse_sse_stream(iter(lines))

    # Should still return the valid event's content
    assert result.content == "Hello from ShadowBot"


@pytest.mark.unit
def test_parse_sse_stream_no_ai_message() -> None:
    """_parse_sse_stream raises ShadowbotQueryError when no AI message is found."""
    lines = _sse_lines(("values", _HUMAN_MSG))

    with pytest.raises(ShadowbotQueryError, match="no AI response in stream"):
        _parse_sse_stream(iter(lines))


@pytest.mark.unit
def test_parse_sse_stream_raw_event_count() -> None:
    """raw_event_count counts only event: values events, not event: metadata."""
    metadata_data = json.dumps({"run_id": "abc123", "thread_id": "t1"})
    values_data_1 = json.dumps({"messages": [{"type": "human", "content": "q"}]})
    values_data_2 = _AI_MSG_STRING

    lines = [
        "event: metadata",
        f"data: {metadata_data}",
        "",
        "event: values",
        f"data: {values_data_1}",
        "",
        "event: values",
        f"data: {values_data_2}",
        "",
    ]

    result = _parse_sse_stream(iter(lines))
    assert result.raw_event_count == 2


# ---------------------------------------------------------------------------
# ShadowbotClient.query tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_client_query_success_no_existing_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query() creates a new thread and returns parsed response when no thread exists."""
    _patch_state_dir(monkeypatch, tmp_path)

    thread_resp = _make_thread_response("new-thread-abc")
    stream_lines = _sse_lines(("values", _AI_MSG_STRING))
    stream_resp = _make_stream_response(lines=stream_lines)

    with (
        patch("httpx.post", return_value=thread_resp) as mock_post,
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = lambda s: stream_resp
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_stream_ctx

        client = ShadowbotClient("test-token")
        result = client.query("hello")

    assert result.content == "Hello from ShadowBot"
    assert result.thread_id == "new-thread-abc"
    mock_post.assert_called_once()

    # State file should have been written
    state_file = tmp_path / "shadowbot-state.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["thread_id"] == "new-thread-abc"


@pytest.mark.unit
def test_client_query_reuses_existing_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query() reuses persisted thread_id without calling POST /threads."""
    _patch_state_dir(monkeypatch, tmp_path)
    _write_state_file(tmp_path, "existing-thread-xyz")

    stream_lines = _sse_lines(("values", _AI_MSG_STRING))
    stream_resp = _make_stream_response(lines=stream_lines)

    with (
        patch("httpx.post") as mock_post,
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = lambda s: stream_resp
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_stream_ctx

        client = ShadowbotClient("test-token")
        result = client.query("hello")

    assert result.content == "Hello from ShadowBot"
    assert result.thread_id == "existing-thread-xyz"
    # POST /threads should NOT have been called
    mock_post.assert_not_called()


@pytest.mark.unit
def test_client_query_new_thread_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query(new_thread=True) creates a fresh thread even when state file exists."""
    _patch_state_dir(monkeypatch, tmp_path)
    _write_state_file(tmp_path, "old-thread-id")

    thread_resp = _make_thread_response("brand-new-thread")
    stream_lines = _sse_lines(("values", _AI_MSG_STRING))
    stream_resp = _make_stream_response(lines=stream_lines)

    with (
        patch("httpx.post", return_value=thread_resp) as mock_post,
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = lambda s: stream_resp
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_stream_ctx

        client = ShadowbotClient("test-token")
        result = client.query("hello", new_thread=True)

    assert result.thread_id == "brand-new-thread"
    mock_post.assert_called_once()


@pytest.mark.unit
def test_client_query_401_on_thread_creation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query() raises ShadowbotAuthError when thread creation returns 401."""
    _patch_state_dir(monkeypatch, tmp_path)
    thread_response = _make_thread_response()
    thread_response.status_code = 401
    thread_response.text = "Unauthorized"
    with patch("httpx.Client") as client_class:
        http_client = MagicMock()
        http_client.send.return_value = thread_response
        client_class.return_value.__enter__ = lambda _: http_client
        client_class.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(ShadowbotAuthError, match="401"):
            ShadowbotClient("bad-token").query("hello")


@pytest.mark.unit
def test_client_query_500_on_thread_creation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query() raises ShadowbotQueryError when thread creation returns 500."""
    _patch_state_dir(monkeypatch, tmp_path)
    thread_response = _make_thread_response()
    thread_response.status_code = 500
    thread_response.text = "Internal Server Error"
    with patch("httpx.Client") as client_class:
        http_client = MagicMock()
        http_client.send.return_value = thread_response
        client_class.return_value.__enter__ = lambda _: http_client
        client_class.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(ShadowbotQueryError, match="500"):
            ShadowbotClient("test-token").query("hello")


@pytest.mark.unit
def test_client_query_thread_creation_retries_transient_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """historic regression: a failure to connect is retried — the request never reached the server."""
    _patch_state_dir(monkeypatch, tmp_path)

    call_count = {"n": 0}
    thread_resp = _make_thread_response("thread-abc-123")

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("transient connection reset")
        return thread_resp

    stream_lines = _sse_lines(("values", _AI_MSG_STRING))
    stream_resp = _make_stream_response(lines=stream_lines)

    with (
        patch("httpx.post", side_effect=_fake_post),
        patch("httpx.Client") as mock_client_cls,
        patch("time.sleep"),
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = lambda s: stream_resp
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_stream_ctx

        client = ShadowbotClient("test-token")
        client.query("hello")

    assert call_count["n"] == 2, "Expected exactly 2 thread-creation calls (1 transient failure + 1 success)"


@pytest.mark.unit
def test_client_query_thread_creation_all_retries_exhausted_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Persistent connect failures stop after the configured attempts."""
    _patch_state_dir(monkeypatch, tmp_path)
    with patch("httpx.Client") as client_class, patch("fieldkit.shadowbot.client.time.sleep"):
        http_client = MagicMock()
        http_client.send.side_effect = httpx.ConnectError("persistent connection refused")
        client_class.return_value.__enter__ = lambda _: http_client
        client_class.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(ShadowbotQueryError, match="Connection error"):
            ShadowbotClient("test-token").query("hello")
    assert http_client.send.call_count == 3


@pytest.mark.unit
def test_client_query_401_on_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query() raises ShadowbotAuthError when stream endpoint returns 401."""
    _patch_state_dir(monkeypatch, tmp_path)

    thread_resp = _make_thread_response("thread-for-401")
    stream_resp = _make_stream_response(status_code=401)

    with (
        patch("httpx.post", return_value=thread_resp),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = lambda s: stream_resp
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_stream_ctx

        client = ShadowbotClient("test-token")
        with pytest.raises(ShadowbotAuthError, match="401"):
            client.query("hello")


@pytest.mark.unit
def test_client_query_non_sse_content_type(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query() raises ShadowbotQueryError when stream returns 200 with text/html Content-Type."""
    _patch_state_dir(monkeypatch, tmp_path)

    thread_resp = _make_thread_response("thread-html")
    stream_resp = _make_stream_response(status_code=200, content_type="text/html; charset=utf-8")

    with (
        patch("httpx.post", return_value=thread_resp),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__ = lambda s: stream_resp
        mock_stream_ctx.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_stream_ctx

        client = ShadowbotClient("test-token")
        with pytest.raises(ShadowbotQueryError, match="text/event-stream"):
            client.query("hello")


@pytest.mark.unit
def test_client_query_404_on_stream_retries_with_new_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """404 on stream triggers stale-thread recovery: new thread created, stream retried once.

    Both the initial thread creation and the recovery thread creation go via
    POST /threads, so the total call count must be exactly 2.
    """
    _patch_state_dir(monkeypatch, tmp_path)
    # No pre-existing state file — initial thread is created via POST /threads
    # (this ensures POST /threads is called twice: once for initial, once for recovery)

    # Both POST /threads calls return different thread IDs
    initial_thread_resp = _make_thread_response("initial-thread-id")
    recovery_thread_resp = _make_thread_response("recovery-thread-id")
    thread_call_count = [0]

    def _thread_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
        thread_call_count[0] += 1
        if thread_call_count[0] == 1:
            return initial_thread_resp
        return recovery_thread_resp

    # First stream: 404 (stale thread)
    stale_stream_resp = _make_stream_response(status_code=404)
    # Second stream: success
    success_stream_lines = _sse_lines(("values", _AI_MSG_STRING))
    success_stream_resp = _make_stream_response(lines=success_stream_lines)

    def _make_stream_ctx(resp: MagicMock) -> MagicMock:
        ctx = MagicMock()
        ctx.__enter__ = lambda s: resp
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    with (
        patch("httpx.post", side_effect=_thread_side_effect) as mock_post,
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        stream_call_count = [0]

        def _stream_side_effect_fn(*args: Any, **kwargs: Any) -> MagicMock:
            stream_call_count[0] += 1
            if stream_call_count[0] == 1:
                return _make_stream_ctx(stale_stream_resp)
            return _make_stream_ctx(success_stream_resp)

        mock_client.stream.side_effect = _stream_side_effect_fn

        client = ShadowbotClient("test-token")
        result = client.query("hello")

    # POST /threads called exactly twice: once for initial thread, once for recovery
    assert mock_post.call_count == 2
    assert result.content == "Hello from ShadowBot"
    assert result.thread_id == "recovery-thread-id"

    # State file should have the new thread_id from recovery
    data = json.loads((tmp_path / "shadowbot-state.json").read_text(encoding="utf-8"))
    assert data["thread_id"] == "recovery-thread-id"


@pytest.mark.unit
def test_client_query_404_retry_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """404 → new thread → second stream also fails → ShadowbotQueryError (no further recursion)."""
    _patch_state_dir(monkeypatch, tmp_path)
    _write_state_file(tmp_path, "stale-thread-id")

    thread_resp = _make_thread_response("recovery-thread-id")

    # Both stream calls return 404
    stale_stream_resp = _make_stream_response(status_code=404)

    def _make_stream_ctx(resp: MagicMock) -> MagicMock:
        ctx = MagicMock()
        ctx.__enter__ = lambda s: resp
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    with (
        patch("httpx.post", return_value=thread_resp),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = _make_stream_ctx(stale_stream_resp)

        client = ShadowbotClient("test-token")
        with pytest.raises(ShadowbotQueryError, match="Thread not found after recovery"):
            client.query("hello")


@pytest.mark.unit
def test_client_query_500_on_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query() raises ShadowbotQueryError when stream endpoint returns 500."""
    _patch_state_dir(monkeypatch, tmp_path)

    thread_resp = _make_thread_response("thread-500")
    stream_resp = _make_stream_response(status_code=500)
    stream_resp.text = "Internal Server Error"

    def _make_stream_ctx(resp: MagicMock) -> MagicMock:
        ctx = MagicMock()
        ctx.__enter__ = lambda s: resp
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    with (
        patch("httpx.post", return_value=thread_resp),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = _make_stream_ctx(stream_resp)

        client = ShadowbotClient("test-token")
        with pytest.raises(ShadowbotQueryError, match="500"):
            client.query("hello")


@pytest.mark.unit
def test_client_query_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """query() raises ShadowbotQueryError on timeout."""
    _patch_state_dir(monkeypatch, tmp_path)

    thread_resp = _make_thread_response("thread-timeout")

    with (
        patch("httpx.post", return_value=thread_resp),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda s: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.stream.side_effect = httpx.TimeoutException("timed out")

        client = ShadowbotClient("test-token")
        with pytest.raises(ShadowbotQueryError, match="timed out"):
            client.query("hello")


@pytest.mark.unit
def test_client_query_closes_client_when_thread_establishment_exceeds_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocked thread-creation send is cancelled at the total query deadline."""
    _patch_state_dir(monkeypatch, tmp_path)
    release_send = Event()

    def blocked_send(*args: object, **kwargs: object) -> MagicMock:
        release_send.wait()
        raise httpx.ConnectError("client closed")

    with patch("httpx.Client") as client_class:
        http_client = MagicMock()
        http_client.send.side_effect = blocked_send
        http_client.close.side_effect = release_send.set
        client_class.return_value.__enter__ = lambda _: http_client
        client_class.return_value.__exit__ = MagicMock(return_value=False)
        started = time.monotonic()
        with pytest.raises(ShadowbotQueryError, match="exceeded total timeout"):
            ShadowbotClient("test-token").query("hello", timeout=0.05, new_thread=True)
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert http_client.close.call_count == 1
    assert release_send.is_set()


@pytest.mark.unit
def test_client_query_closes_client_when_stream_establishment_exceeds_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocked stream-establishment send is cancelled at the total deadline."""
    _patch_state_dir(monkeypatch, tmp_path)
    _write_state_file(tmp_path, "thread-blocked-stream")
    release_send = Event()

    def blocked_send(*args: object, **kwargs: object) -> MagicMock:
        release_send.wait()
        raise httpx.ConnectError("client closed")

    with patch("httpx.Client") as client_class:
        http_client = MagicMock()
        http_client.send.side_effect = blocked_send
        http_client.close.side_effect = release_send.set
        client_class.return_value.__enter__ = lambda _: http_client
        client_class.return_value.__exit__ = MagicMock(return_value=False)
        started = time.monotonic()
        with pytest.raises(ShadowbotQueryError, match="exceeded total timeout"):
            ShadowbotClient("test-token").query("hello", timeout=0.05)
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert http_client.close.call_count == 1
    assert release_send.is_set()


@pytest.mark.unit
def test_client_query_uses_one_absolute_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Thread creation and streaming receive the same absolute query deadline."""
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.client._monotonic", lambda: 0.0)
    client = ShadowbotClient("test-token")
    with (
        patch.object(client, "_create_thread", return_value="thread-budget") as create_thread,
        patch.object(client, "_stream_query", return_value=ShadowbotResponse(content="done")) as stream_query,
        patch("httpx.Client") as client_class,
    ):
        client_class.return_value.__enter__ = lambda _: MagicMock()
        client_class.return_value.__exit__ = MagicMock(return_value=False)
        response = client.query("hello", timeout=10.0, new_thread=True)
    assert response.content == "done"
    assert create_thread.call_args.args[2] == pytest.approx(10.0)
    assert stream_query.call_args.kwargs["deadline"] == pytest.approx(10.0)


@pytest.mark.unit
@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_client_query_rejects_non_finite_or_non_positive_timeout(timeout: float) -> None:
    with pytest.raises(ShadowbotQueryError, match="finite positive"):
        ShadowbotClient("test-token").query("hello", timeout=timeout)


@pytest.mark.unit
def test_client_query_deadline_interrupts_silence_after_late_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A late heartbeat cannot grant a full new read timeout to a silent stream."""
    _patch_state_dir(monkeypatch, tmp_path)
    thread_resp = _make_thread_response("thread-silent")
    release_reader = Event()

    def heartbeat_then_silence() -> Iterable[str]:
        Event().wait(0.1)
        yield ": heartbeat"
        release_reader.wait()

    stream_resp = _make_stream_response()
    stream_resp.iter_lines.return_value = heartbeat_then_silence()
    stream_resp.close.side_effect = release_reader.set
    stream_context = MagicMock()
    stream_context.__enter__ = lambda _: stream_resp
    stream_context.__exit__ = MagicMock(return_value=False)

    with (
        patch("fieldkit.config.get_shadowbot_assistant_id", return_value="sales_assistant_v2"),
        patch("httpx.post", return_value=thread_resp),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.send.side_effect = lambda request, stream=False: _mock_send(mock_client, request, stream=stream)
        mock_client_cls.return_value.__enter__ = lambda _: mock_client
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = stream_context

        started = time.monotonic()
        client = ShadowbotClient("test-token")
        with pytest.raises(ShadowbotQueryError, match="exceeded total timeout"):
            client.query("hello", timeout=0.2, new_thread=True)
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert release_reader.is_set()


@pytest.mark.unit
def test_lines_until_deadline_yields_heartbeat_before_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued heartbeat is delivered before the following deadline check expires."""
    timestamps = iter([0.0, 11.0])
    monkeypatch.setattr("fieldkit.shadowbot.client._monotonic", lambda: next(timestamps))
    response = _make_stream_response(lines=[": heartbeat"])
    lines = iter(_lines_until_deadline(response, deadline=10.0, total_timeout=10.0))

    heartbeat = next(lines)

    assert heartbeat == ": heartbeat"
    with pytest.raises(ShadowbotQueryError, match="exceeded total timeout of 10s"):
        next(lines)


@pytest.mark.unit
def test_call_until_deadline_does_not_start_expired_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.shadowbot.client._monotonic", lambda: 11.0)
    client = MagicMock()
    operation = MagicMock()

    with pytest.raises(ShadowbotQueryError, match="exceeded total timeout of 10s"):
        _call_until_deadline(client, operation, deadline=10.0, total_timeout=10.0)

    operation.assert_not_called()


# ---------------------------------------------------------------------------
# implementation change: assistant_id config override
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_query_uses_default_assistant_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_stream_query includes the configured default assistant ID."""
    _patch_state_dir(monkeypatch, tmp_path)
    http_client = MagicMock()
    stream_response = _make_stream_response(lines=_sse_lines(("values", _AI_MSG_STRING)))
    http_client.send.return_value = stream_response
    with patch("fieldkit.config.get_shadowbot_assistant_id", return_value="sales_assistant_v2"):
        ShadowbotClient("fake-token")._stream_query(
            client=http_client,
            thread_id="test-thread",
            prompt="hello",
            headers={},
            deadline=time.monotonic() + 10.0,
            total_timeout=10.0,
        )
    assert http_client.build_request.call_args.kwargs["json"]["assistant_id"] == "sales_assistant_v2"


@pytest.mark.unit
def test_stream_query_uses_config_override_assistant_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_stream_query includes an overridden assistant ID."""
    _patch_state_dir(monkeypatch, tmp_path)
    http_client = MagicMock()
    stream_response = _make_stream_response(lines=_sse_lines(("values", _AI_MSG_STRING)))
    http_client.send.return_value = stream_response
    with patch("fieldkit.config.get_shadowbot_assistant_id", return_value="custom_assistant_v3"):
        ShadowbotClient("fake-token")._stream_query(
            client=http_client,
            thread_id="test-thread",
            prompt="hello",
            headers={},
            deadline=time.monotonic() + 10.0,
            total_timeout=10.0,
        )
    assert http_client.build_request.call_args.kwargs["json"]["assistant_id"] == "custom_assistant_v3"


@pytest.mark.unit
def test_client_query_thread_creation_does_not_retry_lost_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read timeout is not replayed because the POST may have succeeded."""
    _patch_state_dir(monkeypatch, tmp_path)
    with patch("httpx.Client") as client_class:
        http_client = MagicMock()
        http_client.send.side_effect = httpx.ReadTimeout("response lost after transmission")
        client_class.return_value.__enter__ = lambda _: http_client
        client_class.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(ShadowbotQueryError, match="Timed out creating ShadowBot thread"):
            ShadowbotClient("test-token").query("hello")
    assert http_client.send.call_count == 1


@pytest.mark.unit
def test_client_query_thread_creation_does_not_retry_server_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 5xx is not replayed because the POST reached the server."""
    _patch_state_dir(monkeypatch, tmp_path)
    thread_response = _make_thread_response()
    thread_response.status_code = 503
    thread_response.text = "service unavailable"
    with patch("httpx.Client") as client_class:
        http_client = MagicMock()
        http_client.send.return_value = thread_response
        client_class.return_value.__enter__ = lambda _: http_client
        client_class.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(ShadowbotQueryError, match="503"):
            ShadowbotClient("test-token").query("hello")
    assert http_client.send.call_count == 1
