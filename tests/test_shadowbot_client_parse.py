"""Tests for fieldkit.commands.shadowbot.client._parse_sse_stream — full branch coverage.

Covers all SSE event types, content shapes, error paths, and edge cases
not covered in the existing test_shadowbot_client.py.
"""

import json
import warnings

import pytest

from fieldkit.shadowbot.client import (
    ShadowbotQueryError,
    _parse_sse_stream,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse(event_type: str, data: str) -> list[str]:
    """Build minimal SSE line group for one event."""
    return [f"event: {event_type}", f"data: {data}", ""]


def _ai_event(content: object) -> str:
    return json.dumps({"messages": [{"type": "ai", "content": content}]})


def _human_event(content: str = "user query") -> str:
    return json.dumps({"messages": [{"type": "human", "content": content}]})


# ---------------------------------------------------------------------------
# Heartbeat lines skipped silently
# ---------------------------------------------------------------------------


# ── TestHeartbeatSkipping (flattened) ───────────────────────────────────────


def test_heartbeat_skipping_single_heartbeat_before_valid_event() -> None:
    lines = [": heartbeat", *_sse("values", _ai_event("ok"))]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "ok"


def test_heartbeat_skipping_multiple_heartbeats_interleaved() -> None:
    lines = [
        ": heartbeat",
        *_sse("values", json.dumps({"messages": [{"type": "human", "content": "q"}]})),
        ": heartbeat",
        ": keep-alive",
        *_sse("values", _ai_event("final")),
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "final"


def test_heartbeat_skipping_heartbeat_with_carriage_return() -> None:
    """Lines with \\r\\n must have the \\r stripped before processing."""
    lines = [": heartbeat\r", "event: values\r", f"data: {_ai_event('hello')}\r", "\r"]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "hello"


# ---------------------------------------------------------------------------
# event: metadata skipped
# ---------------------------------------------------------------------------


# ── TestMetadataEventSkipped (flattened) ────────────────────────────────────


def test_metadata_event_skipped_metadata_event_not_counted_in_raw_events() -> None:
    metadata = json.dumps({"run_id": "run-abc", "thread_id": "t1"})
    lines = [
        *_sse("metadata", metadata),
        *_sse("values", _ai_event("answer")),
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "answer"
    assert result.raw_event_count == 1


def test_metadata_event_skipped_only_metadata_event_raises() -> None:
    metadata = json.dumps({"run_id": "run-abc"})
    lines = _sse("metadata", metadata)
    with pytest.raises(ShadowbotQueryError, match="no AI response"):
        _parse_sse_stream(iter(lines))


def test_metadata_event_skipped_unknown_event_type_skipped() -> None:
    lines = [
        *_sse("updates", json.dumps({"foo": "bar"})),
        *_sse("values", _ai_event("real answer")),
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "real answer"
    assert result.raw_event_count == 1


# ---------------------------------------------------------------------------
# data: line without preceding event: — must be skipped (no event type set)
# ---------------------------------------------------------------------------


# ── TestDataWithoutEventType (flattened) ────────────────────────────────────


def test_process_sse_data_line_orphan_data_line_skipped() -> None:
    """data: line with no preceding event: must be ignored (current_event_type is None)."""
    orphan_data = _ai_event("orphan")
    lines = [
        f"data: {orphan_data}",
        "",
        *_sse("values", _ai_event("real")),
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "real"


def test_process_sse_data_line_empty_line_resets_event_type() -> None:
    """Empty line resets current_event_type so next data: line is ignored."""
    lines = [
        "event: values",
        "",  # reset
        f"data: {_ai_event('should-be-ignored')}",
        "",
        *_sse("values", _ai_event("good")),
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "good"


# ---------------------------------------------------------------------------
# Non-data line (not starting with data: or event:) — skipped
# ---------------------------------------------------------------------------


# ── TestNonDataLines (flattened) ────────────────────────────────────────────


def test_process_sse_data_line_id_line_ignored() -> None:
    lines = [
        "event: values",
        "id: 42",
        f"data: {_ai_event('content')}",
        "",
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "content"


def test_process_sse_data_line_retry_line_ignored() -> None:
    lines = [
        "event: values",
        "retry: 3000",
        f"data: {_ai_event('content')}",
        "",
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "content"


# ---------------------------------------------------------------------------
# AI message content shapes
# ---------------------------------------------------------------------------


# ── TestAiContentShapes (flattened) ─────────────────────────────────────────


def test_extract_ai_content_string_content() -> None:
    lines = _sse("values", _ai_event("string response"))
    result = _parse_sse_stream(iter(lines))
    assert result.content == "string response"
    assert result.raw_event_count == 1


def test_extract_ai_content_list_content_text_blocks_joined() -> None:
    content = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
    lines = _sse("values", _ai_event(content))
    result = _parse_sse_stream(iter(lines))
    assert result.content == "AB"


def test_extract_ai_content_list_content_non_text_blocks_skipped() -> None:
    content = [
        {"type": "tool_use", "tool": "search"},
        {"type": "text", "text": "only this"},
    ]
    lines = _sse("values", _ai_event(content))
    result = _parse_sse_stream(iter(lines))
    assert result.content == "only this"


def test_extract_ai_content_list_content_empty_text_block() -> None:
    content = [{"type": "text", "text": ""}]
    lines = _sse("values", _ai_event(content))
    result = _parse_sse_stream(iter(lines))
    assert result.content == ""


def test_extract_ai_content_list_content_block_missing_text_key() -> None:
    content = [{"type": "text"}]  # no 'text' key
    lines = _sse("values", _ai_event(content))
    result = _parse_sse_stream(iter(lines))
    assert result.content == ""  # str("") = ""


def test_extract_ai_content_empty_string_content_is_valid() -> None:
    """Empty string content is a valid AI response (not None)."""
    lines = _sse("values", _ai_event(""))
    result = _parse_sse_stream(iter(lines))
    assert result.content == ""


def test_extract_ai_content_last_ai_message_wins() -> None:
    """When multiple values events contain AI messages, the last one wins."""
    lines = [
        *_sse("values", _ai_event("first")),
        *_sse("values", _ai_event("second")),
        *_sse("values", _ai_event("third")),
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "third"
    assert result.raw_event_count == 3


# ---------------------------------------------------------------------------
# messages field edge cases
# ---------------------------------------------------------------------------


# ── TestMessagesFieldEdgeCases (flattened) ──────────────────────────────────


def test_messages_field_edge_cases_messages_not_a_list() -> None:
    """messages field that is not a list must be skipped without crash."""
    payload = json.dumps({"messages": "not a list"})
    lines = [*_sse("values", payload), *_sse("values", _ai_event("fallback"))]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "fallback"


def test_messages_field_edge_cases_messages_empty_list() -> None:
    """Empty messages list must be skipped without crash."""
    payload = json.dumps({"messages": []})
    lines = [*_sse("values", payload), *_sse("values", _ai_event("fallback"))]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "fallback"


def test_messages_field_edge_cases_last_message_not_a_dict() -> None:
    """messages[-1] that is not a dict must be skipped."""
    payload = json.dumps({"messages": ["string-not-dict"]})
    lines = [*_sse("values", payload), *_sse("values", _ai_event("ok"))]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "ok"


def test_messages_field_edge_cases_last_message_type_human() -> None:
    """messages[-1] with type != 'ai' must be skipped (human message)."""
    lines = [*_sse("values", _human_event()), *_sse("values", _ai_event("ai-response"))]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "ai-response"


def test_messages_field_edge_cases_last_message_missing_type() -> None:
    """messages[-1] with no 'type' key must be skipped."""
    payload = json.dumps({"messages": [{"content": "no type"}]})
    lines = [*_sse("values", payload), *_sse("values", _ai_event("ok"))]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "ok"


def test_messages_field_edge_cases_no_messages_key_in_event() -> None:
    """Event with no 'messages' key must be skipped without crash."""
    payload = json.dumps({"other_key": "value"})
    lines = [*_sse("values", payload), *_sse("values", _ai_event("ok"))]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "ok"


def test_messages_field_edge_cases_messages_is_none() -> None:
    """messages: null must be treated as falsy and skipped."""
    payload = json.dumps({"messages": None})
    lines = [*_sse("values", payload), *_sse("values", _ai_event("ok"))]
    result = _parse_sse_stream(iter(lines))
    assert result.content == "ok"


# ---------------------------------------------------------------------------
# Malformed JSON — UserWarning emitted, parsing continues
# ---------------------------------------------------------------------------


# ── TestMalformedJson (flattened) ───────────────────────────────────────────


def test_malformed_json_malformed_json_warns_and_continues() -> None:
    lines = [
        "event: values",
        "data: {broken json",
        "",
        *_sse("values", _ai_event("recovered")),
    ]
    with pytest.warns(UserWarning, match="malformed JSON"):
        result = _parse_sse_stream(iter(lines))
    assert result.content == "recovered"


def test_malformed_json_multiple_malformed_lines_each_warns() -> None:
    lines = [
        "event: values",
        "data: bad1",
        "",
        "event: values",
        "data: bad2",
        "",
        *_sse("values", _ai_event("good")),
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _parse_sse_stream(iter(lines))

    warn_msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert len(warn_msgs) == 2
    assert all("malformed JSON" in m for m in warn_msgs)
    assert result.content == "good"


def test_malformed_json_all_malformed_raises_no_ai_response() -> None:
    lines = [
        "event: values",
        "data: bad",
        "",
    ]
    with pytest.warns(UserWarning), pytest.raises(ShadowbotQueryError, match="no AI response"):
        _parse_sse_stream(iter(lines))


# ---------------------------------------------------------------------------
# Empty / minimal streams
# ---------------------------------------------------------------------------


# ── TestEmptyStreams (flattened) ────────────────────────────────────────────


def test_empty_streams_empty_lines_raises() -> None:
    with pytest.raises(ShadowbotQueryError, match="no AI response"):
        _parse_sse_stream(iter([]))


def test_empty_streams_only_empty_lines_raises() -> None:
    with pytest.raises(ShadowbotQueryError, match="no AI response"):
        _parse_sse_stream(iter(["", "", ""]))


def test_empty_streams_only_heartbeats_raises() -> None:
    with pytest.raises(ShadowbotQueryError, match="no AI response"):
        _parse_sse_stream(iter([": heartbeat", ": heartbeat"]))


# ---------------------------------------------------------------------------
# ShadowbotResponse properties
# ---------------------------------------------------------------------------


# ── TestShadowbotResponseProperties (flattened) ─────────────────────────────


def test_shadowbot_response_properties_raw_event_count_increments_for_each_values_event() -> None:
    lines = [
        *_sse("values", json.dumps({"messages": [{"type": "human", "content": "q"}]})),
        *_sse("values", _ai_event("r1")),
        *_sse("values", _ai_event("r2")),
    ]
    result = _parse_sse_stream(iter(lines))
    assert result.raw_event_count == 3


def test_shadowbot_response_properties_response_has_no_thread_id_by_default() -> None:
    lines = _sse("values", _ai_event("hi"))
    result = _parse_sse_stream(iter(lines))
    assert result.thread_id is None


def test_shadowbot_response_properties_response_repr_omits_raw_events() -> None:
    lines = _sse("values", _ai_event("hi"))
    result = _parse_sse_stream(iter(lines))
    rep = repr(result)
    assert "_raw_events" not in rep
