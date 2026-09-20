"""Tests for MCPSession.call_tool() — covering uncovered branches.

cc=8, cov=44%, target: cover error/isError/json/raw/None branches.
"""

from unittest.mock import patch

import pytest

from fieldkit.watch.morning_brief_mcp import MCPSession

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_with_id(session_id: str = "sess-123") -> MCPSession:
    """Return an MCPSession that has already been initialised (session_id set)."""
    session = MCPSession(base_url="http://127.0.0.1:8080/mcp")
    session._session_id = session_id
    return session


def _mock_post(headers: dict, body: dict):
    """Patch MCPSession._post to return fixed (headers, body)."""
    return patch.object(MCPSession, "_post", return_value=(headers, body))


# ---------------------------------------------------------------------------
# No session_id guard
# ---------------------------------------------------------------------------


# ── TestCallToolNoSession (flattened) ───────────────────────────────────────


def test_call_tool_no_session_raises_when_no_session_id() -> None:
    """call_tool raises RuntimeError when initialize() has not been called."""
    session = MCPSession(base_url="http://127.0.0.1:8080/mcp")
    assert session._session_id is None

    with pytest.raises(RuntimeError, match="initialize"):
        session.call_tool("some_tool", {})


# ---------------------------------------------------------------------------
# MCP-level error in response body
# ---------------------------------------------------------------------------


# ── TestCallToolMCPError (flattened) ────────────────────────────────────────


def test_call_tool_mcp_error_raises_on_error_key() -> None:
    """call_tool raises RuntimeError when body contains 'error' key."""
    session = _session_with_id()
    body = {"jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found"}, "id": 1}

    with _mock_post({}, body), pytest.raises(RuntimeError, match="returned error"):
        session.call_tool("unknown_tool", {})


# ---------------------------------------------------------------------------
# isError=true in result
# ---------------------------------------------------------------------------


# ── TestCallToolIsError (flattened) ─────────────────────────────────────────


def test_call_tool_is_error_raises_on_is_error_true() -> None:
    """call_tool raises RuntimeError when result.isError is True."""
    session = _session_with_id()
    body = {
        "jsonrpc": "2.0",
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": "Tool execution failed: permission denied"}],
        },
        "id": 1,
    }

    with _mock_post({}, body), pytest.raises(RuntimeError, match="isError=true"):
        session.call_tool("some_tool", {})


def test_call_tool_is_error_is_error_message_includes_content_texts() -> None:
    """The RuntimeError message joins all text content blocks."""
    session = _session_with_id()
    body = {
        "jsonrpc": "2.0",
        "result": {
            "isError": True,
            "content": [
                {"type": "text", "text": "Error A"},
                {"type": "text", "text": "Error B"},
            ],
        },
        "id": 1,
    }

    with _mock_post({}, body), pytest.raises(RuntimeError, match="Error A"):
        session.call_tool("some_tool", {})


# ---------------------------------------------------------------------------
# Successful JSON result
# ---------------------------------------------------------------------------


# ── TestCallToolJSONResult (flattened) ──────────────────────────────────────


def test_call_tool_json_result_returns_parsed_json_from_text_content() -> None:
    """call_tool parses and returns JSON from the first text content block."""
    session = _session_with_id()
    body = {
        "jsonrpc": "2.0",
        "result": {"content": [{"type": "text", "text": '{"events": ["standup", "review"]}'}]},
        "id": 1,
    }

    with _mock_post({}, body):
        result = session.call_tool("get_events", {"date": "2026-06-19"})

    assert result == {"events": ["standup", "review"]}


def test_call_tool_json_result_returns_list_when_json_is_array() -> None:
    """call_tool parses a JSON array response correctly."""
    session = _session_with_id()
    body = {
        "jsonrpc": "2.0",
        "result": {"content": [{"type": "text", "text": "[1, 2, 3]"}]},
        "id": 1,
    }

    with _mock_post({}, body):
        result = session.call_tool("list_tool", {})

    assert result == [1, 2, 3]


# ---------------------------------------------------------------------------
# Raw string result (non-JSON text)
# ---------------------------------------------------------------------------


# ── TestCallToolRawString (flattened) ───────────────────────────────────────


def test_call_tool_raw_string_returns_raw_string_when_not_json() -> None:
    """call_tool returns the raw string when text content is not valid JSON."""
    session = _session_with_id()
    body = {
        "jsonrpc": "2.0",
        "result": {"content": [{"type": "text", "text": "plain text response"}]},
        "id": 1,
    }

    with _mock_post({}, body):
        result = session.call_tool("plain_tool", {})

    assert result == "plain text response"


# ---------------------------------------------------------------------------
# No text content → None
# ---------------------------------------------------------------------------


# ── TestCallToolNoContent (flattened) ───────────────────────────────────────


def test_call_tool_no_content_returns_none_when_content_is_empty() -> None:
    """call_tool returns None when the result has no content blocks."""
    session = _session_with_id()
    body = {
        "jsonrpc": "2.0",
        "result": {"content": []},
        "id": 1,
    }

    with _mock_post({}, body):
        result = session.call_tool("empty_tool", {})

    assert result is None


def test_call_tool_no_content_returns_none_when_result_key_missing() -> None:
    """call_tool returns None when result key is entirely absent."""
    session = _session_with_id()
    body = {"jsonrpc": "2.0", "id": 1}

    with _mock_post({}, body):
        result = session.call_tool("missing_result_tool", {})

    assert result is None


def test_call_tool_no_content_returns_none_when_only_non_text_content() -> None:
    """call_tool returns None when content blocks are non-text type."""
    session = _session_with_id()
    body = {
        "jsonrpc": "2.0",
        "result": {"content": [{"type": "image", "data": "base64..."}]},
        "id": 1,
    }

    with _mock_post({}, body):
        result = session.call_tool("image_tool", {})

    assert result is None


# ---------------------------------------------------------------------------
# call_id increments
# ---------------------------------------------------------------------------


# ── TestCallToolIdIncrement (flattened) ─────────────────────────────────────


def test_call_tool_id_increment_call_id_increments_on_each_call() -> None:
    """Each call_tool invocation uses an incrementing ID."""
    session = _session_with_id()
    body = {"jsonrpc": "2.0", "result": {"content": []}, "id": 1}
    captured_payloads: list[dict] = []

    def capture_post(payload, session_id):
        captured_payloads.append(payload)
        return {}, body

    with patch.object(session, "_post", side_effect=capture_post):
        session.call_tool("tool_a", {})
        session.call_tool("tool_b", {})

    ids = [p["id"] for p in captured_payloads]
    assert ids[1] > ids[0], "Second call must have a higher ID than the first"
