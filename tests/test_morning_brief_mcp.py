"""Unit tests for MCPSession in watch/morning_brief_mcp.py.

Spec 032 — implementation note: MCPSession was refactored to use httpx.Client internally
for connection pooling.  These tests verify the HTTP interaction layer,
error handling, and context manager lifecycle without making real network calls.
"""

import json
import re
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from fieldkit.watch.morning_brief_mcp import MCPSession

pytestmark = pytest.mark.unit

_BASE_URL = "http://127.0.0.1:8080/v0/groups/fieldkit-calendar/mcp"
_SESSION_ID = "test-session-id-abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(
    *,
    status_code: int = 200,
    body: dict | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """Build a MagicMock that mimics an httpx.Response for _post() consumption."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()  # no-op by default (success path)
    resp.headers = headers or {}
    body_dict = body if body is not None else {"jsonrpc": "2.0", "id": 1, "result": {}}
    resp.text = json.dumps(body_dict)
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_retired_command_layer_mcp_module_is_not_importable() -> None:
    """The completed domain migration leaves no command-layer MCP alias."""
    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module("fieldkit.commands.watch.morning_brief_mcp")

    assert exc_info.value.name == "fieldkit.commands.watch.morning_brief_mcp"


def test_post_sends_correct_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_post() sends Content-Type, Accept, and Mcp-Session-Id headers correctly."""
    captured_kwargs: dict = {}

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _make_mock_response()

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session._http, "post", _fake_post)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    session._post(payload, session_id=_SESSION_ID)

    sent_headers: dict = captured_kwargs.get("headers", {})
    assert sent_headers.get("Content-Type") == "application/json", (
        f"Expected Content-Type: application/json, got: {sent_headers}"
    )
    assert sent_headers.get("Accept") == "application/json", f"Expected Accept: application/json, got: {sent_headers}"
    assert sent_headers.get("Mcp-Session-Id") == _SESSION_ID, (
        f"Expected Mcp-Session-Id: {_SESSION_ID}, got: {sent_headers}"
    )


def test_post_omits_session_id_header_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """_post() does NOT include Mcp-Session-Id header when session_id=None."""
    captured_kwargs: dict = {}

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _make_mock_response()

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session._http, "post", _fake_post)

    payload = {"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1}
    session._post(payload, session_id=None)

    sent_headers: dict = captured_kwargs.get("headers", {})
    assert "Mcp-Session-Id" not in sent_headers, "Mcp-Session-Id should not be present when session_id=None"


def test_http_error_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx.HTTPError from post() is wrapped in RuntimeError with 'MCP HTTP error'."""

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        raise httpx.HTTPError("connection refused")

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session._http, "post", _fake_post)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    with pytest.raises(RuntimeError, match="MCP HTTP error"):
        session._post(payload, session_id=_SESSION_ID)


def test_post_retries_transient_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: a transient transport failure is retried; second attempt succeeds."""
    call_count = {"n": 0}

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.ConnectError("transient connection reset")
        return _make_mock_response(body={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session._http, "post", _fake_post)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    _headers, body = session._post(payload, session_id=_SESSION_ID)

    assert call_count["n"] == 2, "Expected exactly 2 calls (1 transient failure + 1 success)"
    assert body["result"] == {"ok": True}


def test_post_all_retries_exhausted_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: a transport failure on all 3 attempts still raises RuntimeError."""
    call_count = {"n": 0}

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        raise httpx.ConnectError("persistent connection refused")

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session._http, "post", _fake_post)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    with pytest.raises(RuntimeError, match="MCP HTTP error"):
        session._post(payload, session_id=_SESSION_ID)

    assert call_count["n"] == 3, "Expected exactly 3 attempts (stop_after_attempt(3))"


def test_empty_body_returns_empty_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """When resp.text is an empty string, _post() returns ({}, {}) without raising."""

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.headers = {}
        resp.text = ""  # empty body — MCP teardown path
        return resp

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session._http, "post", _fake_post)

    payload = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    result = session._post(payload, session_id=_SESSION_ID)

    assert result == ({}, {}), f"Expected ({{}}, {{}}) for empty body, got: {result}"


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

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        resp = _make_mock_response(body={}, status_code=status)
        request = httpx.Request("POST", _BASE_URL)
        raise httpx.HTTPStatusError(f"HTTP {status}", request=request, response=resp)

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session._http, "post", _fake_post)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    payload = {"jsonrpc": "2.0", "method": "tools/call", "params": {}, "id": 1}
    with pytest.raises(RuntimeError, match="MCP HTTP error"):
        session._post(payload, session_id=_SESSION_ID)

    assert call_count["n"] == 1, f"HTTP {status} must be attempted exactly once"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_post_retries_transient_status(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """The shared transient status set is retried and recovers."""
    call_count = {"n": 0}

    def _fake_post(url: str, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        if call_count["n"] == 1:
            resp = _make_mock_response(body={}, status_code=status)
            request = httpx.Request("POST", _BASE_URL)
            raise httpx.HTTPStatusError(f"HTTP {status}", request=request, response=resp)
        return _make_mock_response(body={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    session = MCPSession(_BASE_URL)
    monkeypatch.setattr(session._http, "post", _fake_post)
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
