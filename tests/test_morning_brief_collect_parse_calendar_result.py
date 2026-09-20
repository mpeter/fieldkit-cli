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


def test_dict_with_neither_items_nor_events_returns_empty_list() -> None:
    result = {"summary": "unrelated payload"}
    assert _parse_calendar_result(result) == []


def test_dict_with_non_list_items_value_returns_empty_list() -> None:
    result = {"items": "not-a-list"}
    assert _parse_calendar_result(result) == []


def test_dict_with_none_items_and_none_events_returns_empty_list() -> None:
    result = {"items": None, "events": None}
    assert _parse_calendar_result(result) == []


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


def test_json_string_decoding_to_dict_with_neither_key_returns_empty_list() -> None:
    result = json.dumps({"summary": "unrelated payload"})
    assert _parse_calendar_result(result) == []


def test_json_string_decoding_to_dict_with_non_list_items_value_returns_empty_list() -> None:
    result = json.dumps({"items": "not-a-list"})
    assert _parse_calendar_result(result) == []


# --- final fallback: decoded JSON is neither list nor dict (line 159) ------


def test_json_string_decoding_to_bare_int_returns_empty_list() -> None:
    assert _parse_calendar_result("42") == []


def test_json_string_decoding_to_bare_string_returns_empty_list() -> None:
    assert _parse_calendar_result('"just a string"') == []
