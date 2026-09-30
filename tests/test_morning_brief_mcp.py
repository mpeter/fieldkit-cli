"""Unit tests for MCPSession in watch/mcp.py.

Spec 032 — implementation note: MCPSession uses a pooled httpx.AsyncClient on a
private event loop. These tests verify the HTTP interaction layer,
error handling, and context manager lifecycle without making real network calls.
"""

import asyncio
import json
import logging
import re
import time
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from fieldkit.watch import mcp as mcp_module
from fieldkit.watch.mcp import MCPAuthError, MCPSession

pytestmark = pytest.mark.unit

_BASE_URL = "https://gateway.example.com/calendar/mcp"
_SESSION_ID = "test-session-id-abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    *,
    status_code: int = 200,
    body: dict | None = None,
    headers: dict | None = None,
    request: httpx.Request | None = None,
) -> httpx.Response:
    """Build one real response for the async mock transport."""
    body_dict = body if body is not None else {"jsonrpc": "2.0", "id": 1, "result": {}}
    return httpx.Response(status_code, headers=headers, content=json.dumps(body_dict).encode("utf-8"), request=request)


def _install_transport(
    session: MCPSession,
    handler: httpx.MockTransport,
    *,
    timeout: float | None = None,
) -> None:
    """Replace a session transport on its private loop without leaking the old pool."""
    session._runner.run(session._http.aclose())
    session._http = httpx.AsyncClient(transport=handler, timeout=timeout or session.timeout)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    ["fieldkit.watch.morning_brief_mcp", "fieldkit.commands.watch.morning_brief_mcp"],
)
def test_retired_mcp_module_names_are_not_importable(module_name: str) -> None:
    """The canonical client has no compatibility alias at either retired path."""
    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module(module_name)

    assert exc_info.value.name == module_name


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), True, "1"])
def test_session_rejects_invalid_total_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        MCPSession(_BASE_URL, timeout=timeout)  # type: ignore[arg-type]


def test_post_sends_correct_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_post() sends Content-Type, Accept, and Mcp-Session-Id headers correctly."""
    captured_headers = httpx.Headers()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = request.headers
        return _make_response(request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    session._post(payload, session_id=_SESSION_ID)

    assert captured_headers.get("Content-Type") == "application/json", (
        f"Expected Content-Type: application/json, got: {captured_headers}"
    )
    assert captured_headers.get("Accept") == "application/json", (
        f"Expected Accept: application/json, got: {captured_headers}"
    )
    assert captured_headers.get("Mcp-Session-Id") == _SESSION_ID, (
        f"Expected Mcp-Session-Id: {_SESSION_ID}, got: {captured_headers}"
    )


def test_post_omits_session_id_header_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """_post() does NOT include Mcp-Session-Id header when session_id=None."""
    captured_headers = httpx.Headers()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = request.headers
        return _make_response(request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))

    payload = {"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1}
    session._post(payload, session_id=None)

    assert "Mcp-Session-Id" not in captured_headers, "Mcp-Session-Id should not be present when session_id=None"


def test_post_rejects_declared_response_over_byte_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_module, "_MCP_RESPONSE_MAX_BYTES", 32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "33"}, content=b"x" * 33, request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="invalid response") as exc_info:
            session._post({"jsonrpc": "2.0", "method": "tools/call", "id": 1}, _SESSION_ID)
    finally:
        session.close()

    assert "x" not in str(exc_info.value)


def test_post_rejects_streamed_response_over_actual_byte_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_module, "_MCP_RESPONSE_MAX_BYTES", 32)

    class UnboundedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"y" * 16
            yield b"y" * 17

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=UnboundedStream(), request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="invalid response") as exc_info:
            session._post({"jsonrpc": "2.0", "method": "tools/call", "id": 1}, _SESSION_ID)
    finally:
        session.close()

    assert "y" not in str(exc_info.value)


def test_post_interrupts_a_blocking_stream_at_the_total_deadline() -> None:
    class BlockingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.sleep(0.25)
            yield b" "

        async def aclose(self) -> None:
            self.closed = True

    stream = BlockingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    session = MCPSession(_BASE_URL, timeout=0.03)
    _install_transport(session, httpx.MockTransport(handler), timeout=0.03)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="invalid response"):
            session._post({"jsonrpc": "2.0", "method": "tools/call", "id": 1}, _SESSION_ID)
    finally:
        session.close()

    assert time.monotonic() - started < 0.08
    assert stream.closed is True


@pytest.mark.parametrize("close_mode", ["slow", "never"])
def test_post_bounds_response_cleanup_within_the_total_deadline(close_mode: str) -> None:
    class BlockingCloseStream(httpx.AsyncByteStream):
        close_started = False
        close_cancelled = False

        async def __aiter__(self):
            yield b'{"jsonrpc":"2.0","id":1,"result":{}}'

        async def aclose(self) -> None:
            self.close_started = True
            try:
                if close_mode == "slow":
                    await asyncio.sleep(0.25)
                else:
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise

    stream = BlockingCloseStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    session = MCPSession(_BASE_URL, timeout=0.03)
    _install_transport(session, httpx.MockTransport(handler), timeout=0.03)

    async def invoke_with_test_watchdog() -> list[asyncio.Task[object]]:
        with pytest.raises(mcp_module._InvalidMCPResponse):
            await asyncio.wait_for(
                mcp_module._post_once(
                    session._http,
                    _BASE_URL,
                    b"{}",
                    {"content-type": "application/json"},
                    0.03,
                ),
                timeout=0.2,
            )
        await asyncio.sleep(0)
        current = asyncio.current_task()
        return [task for task in asyncio.all_tasks() if task is not current and not task.done()]

    started = time.monotonic()
    try:
        pending = session._runner.run(invoke_with_test_watchdog())
    finally:
        session.close()

    assert time.monotonic() - started < 0.08
    assert stream.close_started is True
    assert stream.close_cancelled is True
    assert pending == []


def test_post_fast_response_and_cleanup_succeed_with_a_small_total_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _make_response(request=request)

    session = MCPSession(_BASE_URL, timeout=0.03)
    _install_transport(session, httpx.MockTransport(handler), timeout=0.03)
    try:
        _headers, body = session._post(
            {"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            _SESSION_ID,
        )
    finally:
        session.close()

    assert body["result"] == {}


def test_slow_response_cleanup_does_not_mask_authentication_failure() -> None:
    class SlowCloseStream(httpx.AsyncByteStream):
        close_cancelled = False

        async def __aiter__(self):
            yield b"denied"

        async def aclose(self) -> None:
            try:
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise

    stream = SlowCloseStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, stream=stream, request=request)

    session = MCPSession(_BASE_URL, timeout=0.03)
    _install_transport(session, httpx.MockTransport(handler), timeout=0.03)
    try:
        with pytest.raises(MCPAuthError, match="authentication failed"):
            session._post({"jsonrpc": "2.0", "method": "initialize", "id": 1}, None)
    finally:
        session.close()

    assert stream.close_cancelled is True


@pytest.mark.parametrize("status_code", [401, 403])
def test_response_cleanup_error_does_not_mask_authentication_failure(status_code: int) -> None:
    class FailingCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"denied"

        async def aclose(self) -> None:
            raise RuntimeError("fictional cleanup payload")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, stream=FailingCloseStream(), request=request)

    session = MCPSession(_BASE_URL, timeout=0.03)
    _install_transport(session, httpx.MockTransport(handler), timeout=0.03)
    try:
        with pytest.raises(MCPAuthError, match="authentication failed") as captured:
            session._post({"jsonrpc": "2.0", "method": "initialize", "id": 1}, None)
    finally:
        session.close()

    assert "fictional" not in str(captured.value)


def test_response_cleanup_error_after_success_is_sanitized() -> None:
    class FailingCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"jsonrpc":"2.0","id":1,"result":{}}'

        async def aclose(self) -> None:
            raise RuntimeError("fictional cleanup payload")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=FailingCloseStream(), request=request)

    session = MCPSession(_BASE_URL, timeout=0.03)
    _install_transport(session, httpx.MockTransport(handler), timeout=0.03)
    try:
        with pytest.raises(RuntimeError, match="invalid response") as captured:
            session._post({"jsonrpc": "2.0", "method": "tools/call", "id": 1}, _SESSION_ID)
    finally:
        session.close()

    assert "fictional" not in str(captured.value)


@pytest.mark.parametrize("close_mode", ["slow", "never"])
def test_session_close_is_bounded_for_cancellation_cooperative_client_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    close_mode: str,
) -> None:
    session = MCPSession(_BASE_URL, timeout=0.03)
    real_close = session._http.aclose
    close_started = False
    close_cancelled = False

    async def blocking_close() -> None:
        nonlocal close_started, close_cancelled
        close_started = True
        try:
            if close_mode == "slow":
                await asyncio.sleep(0.25)
            else:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            close_cancelled = True
            await real_close()
            raise

    monkeypatch.setattr(session._http, "aclose", blocking_close)
    started = time.monotonic()

    session.close()

    assert time.monotonic() - started < 0.08
    assert close_started is True
    assert close_cancelled is True


def test_http_error_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport details are replaced with bounded retry guidance."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPError("connection refused")

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    with pytest.raises(RuntimeError, match="MCP request failed; retry later"):
        session._post(payload, session_id=_SESSION_ID)


def test_post_retries_transient_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: a transient transport failure is retried; second attempt succeeds."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("transient connection reset", request=request)
        return _make_response(body={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    _headers, body = session._post(payload, session_id=_SESSION_ID)

    assert call_count["n"] == 2, "Expected exactly 2 calls (1 transient failure + 1 success)"
    assert body["result"] == {"ok": True}


def test_post_all_retries_exhausted_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: a transport failure on all 3 attempts still raises RuntimeError."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ConnectError("persistent connection refused", request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    with pytest.raises(RuntimeError, match="MCP request failed; retry later"):
        session._post(payload, session_id=_SESSION_ID)

    assert call_count["n"] == 3, "Expected exactly 3 attempts (stop_after_attempt(3))"


def test_notification_accepts_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A notification has no response body by JSON-RPC design."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))

    payload = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    result = session._post(payload, session_id=_SESSION_ID)

    assert result == ({}, {}), f"Expected ({{}}, {{}}) for empty body, got: {result}"


@pytest.mark.parametrize(
    ("response_text", "message"),
    [
        ("", "empty response"),
        ("not-json", "invalid response"),
        ('{"jsonrpc":"2.0","id":1,"result":{},"result":{"content":[]}}', "invalid response"),
        ('{"jsonrpc":"2.0","id":1}', "result or error"),
        ('{"jsonrpc":"2.0","id":2,"result":{}}', "response id"),
        ('{"jsonrpc":"1.0","id":1,"result":{}}', "JSON-RPC version"),
    ],
)
def test_request_rejects_invalid_json_rpc_envelope(
    monkeypatch: pytest.MonkeyPatch, response_text: str, message: str
) -> None:
    session = MCPSession(_BASE_URL)
    _install_transport(
        session,
        httpx.MockTransport(
            lambda request: httpx.Response(200, content=response_text.encode("utf-8"), request=request)
        ),
    )

    with pytest.raises(RuntimeError, match=message):
        session._post({"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}, _SESSION_ID)


def test_initialize_validates_negotiation_then_notifies_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MCPSession(_BASE_URL)
    calls: list[tuple[dict, str | None]] = []

    def post(payload: dict, session_id: str | None):
        calls.append((payload, session_id))
        if payload["method"] == "initialize":
            return {"mcp-session-id": _SESSION_ID}, {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "example-server", "version": "1"},
                },
            }
        return {}, {}

    monkeypatch.setattr(session, "_post", post)

    session.initialize()

    assert [payload["method"] for payload, _session_id in calls] == [
        "initialize",
        "notifications/initialized",
    ]
    assert calls[1][1] == _SESSION_ID


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "x", "version": "1"}},
        {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "x", "version": "1"}},
        {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {}},
    ],
)
def test_initialize_rejects_invalid_negotiation(monkeypatch: pytest.MonkeyPatch, result: dict[str, object]) -> None:
    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(
        session,
        "_post",
        lambda payload, session_id: (
            {"mcp-session-id": _SESSION_ID},
            {"jsonrpc": "2.0", "id": payload["id"], "result": result},
        ),
    )

    with pytest.raises(RuntimeError, match="negotiation"):
        session.initialize()

    assert session._session_id is None


def test_context_manager_auth_failure_closes_without_masking_original(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MCPSession(_BASE_URL)
    auth = MCPAuthError("authentication failed")
    close = AsyncMock(side_effect=RuntimeError("close failed"))
    monkeypatch.setattr(session, "_post", MagicMock(side_effect=auth))
    monkeypatch.setattr(session._http, "aclose", close)

    with pytest.raises(MCPAuthError) as exc_info, session:
        pytest.fail("context body must not run after failed initialization")

    assert exc_info.value is auth
    close.assert_awaited_once()


def test_initialize_base_exception_closes_without_masking_original(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MCPSession(_BASE_URL)
    interrupted = KeyboardInterrupt()
    close = AsyncMock(side_effect=RuntimeError("close failed"))
    monkeypatch.setattr(session, "_post", MagicMock(side_effect=interrupted))
    monkeypatch.setattr(session._http, "aclose", close)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        session.initialize()

    assert exc_info.value is interrupted
    close.assert_awaited_once()


def test_initialize_cleanup_base_exception_does_not_mask_original(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MCPSession(_BASE_URL)
    original = SystemExit(7)
    monkeypatch.setattr(session, "_post", MagicMock(side_effect=original))
    monkeypatch.setattr(session._http, "aclose", AsyncMock(side_effect=KeyboardInterrupt))

    with pytest.raises(SystemExit) as exc_info:
        session.initialize()

    assert exc_info.value is original


@pytest.mark.parametrize("session_id", ["", "contains space", "line\nbreak", "\x7f", "a" * 1025])
def test_initialize_rejects_invalid_session_id(monkeypatch: pytest.MonkeyPatch, session_id: str) -> None:
    session = MCPSession(_BASE_URL)
    close = AsyncMock()
    monkeypatch.setattr(session._http, "aclose", close)
    monkeypatch.setattr(
        session,
        "_post",
        lambda payload, session_id: (
            {"mcp-session-id": session_id},
            {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "example-server", "version": "1"},
                },
            },
        ),
    )

    with pytest.raises(RuntimeError, match="negotiation"):
        session.initialize()

    close.assert_awaited_once()


def test_initialize_rejects_duplicate_session_id_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MCPSession(_BASE_URL)
    headers = httpx.Headers([("Mcp-Session-Id", "first"), ("Mcp-Session-Id", "second")])
    monkeypatch.setattr(
        session,
        "_post",
        lambda payload, session_id: (
            headers,
            {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "example-server", "version": "1"},
                },
            },
        ),
    )

    with pytest.raises(RuntimeError, match="negotiation"):
        session.initialize()


def test_draft_queue_auth_failure_closes_without_masking_original(monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.watch import draft_queue

    session = MCPSession(_BASE_URL)
    auth = MCPAuthError("authentication failed")
    close = AsyncMock(side_effect=RuntimeError("close failed"))
    monkeypatch.setattr(session, "_post", MagicMock(side_effect=auth))
    monkeypatch.setattr(session._http, "aclose", close)
    monkeypatch.setattr("fieldkit.watch.mcp.MCPSession", lambda _endpoint: session)
    monkeypatch.setattr(draft_queue, "get_mcp_endpoint", lambda _name: _BASE_URL)
    monkeypatch.setattr(draft_queue, "_resolve_user_email", lambda: "user@example.com")
    monkeypatch.setattr(draft_queue, "watcher_logging", MagicMock())

    with pytest.raises(MCPAuthError) as exc_info:
        draft_queue._run_draft_queue(dry_run=False)

    assert exc_info.value is auth
    close.assert_awaited_once()


def test_backstory_auth_failure_closes_without_masking_original(monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.watch import backstory_health

    session = MCPSession(_BASE_URL)
    auth = MCPAuthError("authentication failed")
    close = AsyncMock(side_effect=RuntimeError("close failed"))
    monkeypatch.setattr(session, "_post", MagicMock(side_effect=auth))
    monkeypatch.setattr(session._http, "aclose", close)
    monkeypatch.setattr(backstory_health, "MCPSession", lambda _endpoint: session)

    with pytest.raises(MCPAuthError) as exc_info:
        backstory_health._open_mcp_session(_BASE_URL)

    assert exc_info.value is auth
    close.assert_awaited_once()


def test_close_only_closes_http(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MCPSession(_BASE_URL)
    session._session_id = _SESSION_ID
    post = MagicMock()
    close = AsyncMock()
    monkeypatch.setattr(session, "_post", post)
    monkeypatch.setattr(session._http, "aclose", close)

    session.close()

    post.assert_not_called()
    close.assert_awaited_once()
    assert session._session_id is None


def test_circuit_breaker_never_logs_or_raises_endpoint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = MCPSession("https://private.example.invalid/secret-route")
    failure = MagicMock(side_effect=RuntimeError("bounded provider failure"))
    monkeypatch.setattr(session, "_call_tool_inner", failure)
    caplog.set_level(logging.INFO)

    for _ in range(3):
        with pytest.raises(RuntimeError, match="bounded provider failure"):
            session.call_tool("tool", {})
    with pytest.raises(RuntimeError, match="temporarily unavailable") as exc_info:
        session.call_tool("tool", {})

    session._breaker._opened_at = 0.0
    monkeypatch.setattr("fieldkit.circuit_breaker.time.monotonic", lambda: 1_000.0)
    monkeypatch.setattr(session, "_call_tool_inner", MagicMock(return_value={"ok": True}))
    assert session.call_tool("tool", {}) == {"ok": True}
    assert "optional-mcp" in caplog.text
    assert "private.example.invalid" not in caplog.text
    assert "private.example.invalid" not in str(exc_info.value)


def test_context_manager_calls_initialize_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """__enter__ calls initialize() and __exit__ calls close()."""
    initialize_called = []
    close_called = []

    def _fake_initialize() -> None:
        initialize_called.append(True)

    def _fake_close() -> None:
        close_called.append(True)

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session, "initialize", _fake_initialize)
    monkeypatch.setattr(session, "close", _fake_close)

    with session as ctx:
        assert ctx is session, "__enter__ must return self"
        assert len(initialize_called) == 1, "initialize() must be called on __enter__"
        assert len(close_called) == 0, "close() must NOT be called before __exit__"

    assert len(close_called) == 1, "close() must be called on __exit__"


# ---------------------------------------------------------------------------
# Retry classification (shared policy)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_post_does_not_retry_client_error(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """A 4xx means the gateway understood and rejected the request — repeating cannot help.

    The original fix matched `httpx.HTTPError`, the base of both RequestError and
    HTTPStatusError, so every 401/403/404 was retried three times before surfacing an
    error that was never going to change.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _make_response(body={}, status_code=status, request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    expected = MCPAuthError if status in {401, 403} else RuntimeError
    with pytest.raises(expected):
        session._post(payload, session_id=_SESSION_ID)

    assert call_count["n"] == 1, f"HTTP {status} must be attempted exactly once"


@pytest.mark.parametrize("status", [401, 403])
def test_post_auth_failure_is_payload_free(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """Authentication failures carry fixed guidance without endpoint or provider text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _make_response(
            body={"private": "provider response with secret-token"}, status_code=status, request=request
        )

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))
    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}

    with pytest.raises(MCPAuthError) as exc_info:
        session._post(payload, session_id=_SESSION_ID)

    message = str(exc_info.value)
    assert "authentication failed" in message
    assert _BASE_URL not in message
    assert "secret-token" not in message


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_post_retries_transient_status(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """The shared transient status set is retried and recovers."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_response(body={}, status_code=status, request=request)
        return _make_response(body={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, request=request)

    session = MCPSession(_BASE_URL)
    _install_transport(session, httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    _headers, body = session._post(payload, session_id=_SESSION_ID)

    assert body["result"] == {"ok": True}
    assert call_count["n"] == 2


_REVIEWED_MCP_TOOLS = frozenset(
    {
        "backstory__find_account",
        "backstory__get_account_status",
        "google_workspace__search_gmail_messages",
        "google_workspace__get_events",
    }
)
"""Every MCP tool this codebase invokes. All four are reads."""


def test_mcp_tool_allowlist_is_reviewed() -> None:
    """_post_with_retry uses transient_retry on the strength of every tool being a read.

    That is an assumption about the tools, not about JSON-RPC: call_tool() takes an
    arbitrary name, so a mutating tool added later silently inherits full transient
    retry and gets re-run on a 5xx. This fails when the tool set changes, forcing that
    judgement to be made rather than inherited.

    If the new tool is a read, add it here. If it mutates, it needs its own retry
    classification — see fieldkit.config.retry.
    """
    watch_dir = Path(__file__).resolve().parent.parent / "src" / "fieldkit" / "watch"
    found: set[str] = set()
    for path in watch_dir.rglob("*.py"):
        found |= set(re.findall(r'call_tool\(\s*"([a-z0-9_]+__[a-z0-9_]+)"', path.read_text(encoding="utf-8")))

    assert found == set(_REVIEWED_MCP_TOOLS), (
        f"MCP tool set changed. New: {sorted(found - _REVIEWED_MCP_TOOLS)}; "
        f"gone: {sorted(set(_REVIEWED_MCP_TOOLS) - found)}. "
        "Confirm every tool is safe to repeat before updating _REVIEWED_MCP_TOOLS."
    )
