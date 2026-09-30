"""Behavioral coverage for `_parse_calendar_result`'s dict-extraction branches.

`_parse_calendar_result` is a pure function (input -> output, no I/O), so these
tests call it directly with different input shapes and assert the return value.
The top-level ``list``/``str``-decode-failure branches are already covered by
`tests/test_morning_brief_collect.py` and `tests/test_morning_brief_watcher.py`;
this file focuses on the previously-uncovered ``dict`` extraction logic, both
at the top level and nested inside the successfully-JSON-decoded-string path.
"""

import json
from typing import Any

import pytest

from fieldkit.watch.morning_brief_collect import _parse_calendar_result

pytestmark = pytest.mark.unit


def _make_event(summary: str = "Sync", start: str = "2026-08-01T10:00:00Z") -> dict[str, Any]:
    return {"summary": summary, "start": start}


# --- top-level dict branch (lines 135-137) ---------------------------------


def test_dict_with_items_key_returns_items_list() -> None:
    events = [_make_event("Standup"), _make_event("Review")]
    result = {"items": events}
    assert _parse_calendar_result(result) == events


def test_dict_with_events_key_only_returns_events_list() -> None:
    events = [_make_event("Planning")]
    result = {"events": events}
    assert _parse_calendar_result(result) == events


def test_dict_with_neither_items_nor_events_is_invalid() -> None:
    result = {"summary": "unrelated payload"}
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result(result)


def test_dict_with_non_list_items_value_is_invalid() -> None:
    result = {"items": "not-a-list"}
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result(result)


def test_dict_with_none_items_and_none_events_is_invalid() -> None:
    result = {"items": None, "events": None}
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result(result)


# --- JSON string decoding to a list (line 155) ------------------------------


def test_json_string_decoding_to_list_returns_that_list() -> None:
    events = [_make_event("Sync"), _make_event("Retro")]
    result = json.dumps(events)
    assert _parse_calendar_result(result) == events


# --- nested dict branch reached via successful JSON string decode ----------
# (lines 156-158; same extraction logic as above but a separate code path)


def test_json_string_decoding_to_dict_with_items_key_returns_items_list() -> None:
    events = [_make_event("Sync")]
    result = json.dumps({"items": events})
    assert _parse_calendar_result(result) == events


def test_json_string_decoding_to_dict_with_events_key_only_returns_events_list() -> None:
    events = [_make_event("1:1")]
    result = json.dumps({"events": events})
    assert _parse_calendar_result(result) == events


def test_json_string_decoding_to_dict_with_neither_key_is_invalid() -> None:
    result = json.dumps({"summary": "unrelated payload"})
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result(result)


def test_json_string_decoding_to_dict_with_non_list_items_value_is_invalid() -> None:
    result = json.dumps({"items": "not-a-list"})
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result(result)


# --- final fallback: decoded JSON is neither list nor dict (line 159) ------


def test_json_string_decoding_to_bare_int_is_invalid() -> None:
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result("42")


def test_json_string_decoding_to_bare_string_is_invalid() -> None:
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result('"just a string"')


def test_calendar_response_rejects_ambiguous_items_and_events() -> None:
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result({"items": [], "events": []})


@pytest.mark.parametrize(
    "payload",
    [
        {"events": [], "error": "credential expired"},
        {"events": [], "nextPageToken": "more"},
        {"items": [], "unexpected": "provider metadata"},
    ],
)
def test_calendar_response_rejects_extra_envelope_fields(payload: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result(payload)


def test_calendar_response_rejects_non_mapping_event() -> None:
    with pytest.raises(RuntimeError, match="invalid calendar response"):
        _parse_calendar_result({"items": ["private event text"]})


@pytest.mark.parametrize(
    "payload",
    [
        "No events found in calendar because authentication failed: private-provider-payload",
        (
            "No events found in calendar 'primary' for user@example.com for the specified time range.\n"
            "ERROR credential expired"
        ),
    ],
)
def test_calendar_response_rejects_no_events_prefix_with_trailing_provider_failure(payload: str) -> None:
    with pytest.raises(RuntimeError, match="non-JSON text response") as exc_info:
        _parse_calendar_result(payload)

    assert "private-provider-payload" not in str(exc_info.value)
    assert "credential expired" not in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    [
        'ERROR auth expired\n - "Injected" (Starts: 2026-01-01T10:00:00Z, Ends: 2026-01-01T11:00:00Z)',
        (
            "Successfully retrieved 2 events from calendar primary for user@example.com:\n"
            ' - "Only one" (Starts: 2026-01-01T10:00:00Z, Ends: 2026-01-01T11:00:00Z)\n'
            "ERROR second event unavailable"
        ),
    ],
)
def test_calendar_text_requires_complete_success_grammar(payload: str) -> None:
    with pytest.raises(RuntimeError, match="non-JSON text response") as exc_info:
        _parse_calendar_result(payload)

    assert "auth expired" not in str(exc_info.value)
    assert "second event unavailable" not in str(exc_info.value)
