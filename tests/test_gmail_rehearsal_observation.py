"""Sanitized observations use synthetic committed Gmail publications only."""

import json
import sqlite3
import traceback
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fieldkit.gmail.publication import (
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
    publication_root_for,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection
from scripts import gmail_rehearsal_observation as observer

pytestmark = pytest.mark.unit

_PRIVATE_TOKEN = "synthetic-provider-token-do-not-expose"
_PRIVATE_BODY = "synthetic-mail-body-do-not-expose"
_PRIVATE_ADDRESS = "private-sentinel@example.com"
_PRIVATE_ID = "synthetic-message-identifier-do-not-expose"


def _cache(tmp_path: Path, state: dict[str, str | bytes | None]) -> Path:
    source = tmp_path / "gmail.db"
    initialize_gmail_publication(source)

    def populate(connection: SQLiteMutationConnection) -> None:
        connection.execute("INSERT INTO threads(thread_id) VALUES ('synthetic-thread')")
        connection.execute(
            "INSERT INTO messages(message_id, thread_id, from_addr, body_plain) VALUES (?, ?, ?, ?)",
            (_PRIVATE_ID, "synthetic-thread", _PRIVATE_ADDRESS, _PRIVATE_BODY),
        )
        connection.execute("INSERT INTO sync_state(key, value) VALUES ('ignored_private_key', ?)", (_PRIVATE_TOKEN,))
        connection.execute("INSERT INTO sync_state(key, value) VALUES ('fieldkit_query_ready', 'true')")
        for key, value in state.items():
            connection.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, ?)", (key, value))

    apply_gmail_page(source, populate)
    return source


def test_observation_contains_only_aggregates_and_preserves_since_cap_nuance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    source = _cache(
        tmp_path,
        {
            "since_epoch": "1780000000",
            "since_page_token": _PRIVATE_TOKEN,
            "since_messages_synced": "500",
            "last_history_id": _PRIVATE_TOKEN,
        },
    )

    result = observer.observe_gmail_cache(source)

    assert result == observer.GmailCacheObservation(1, True, 500, True, True)
    assert set(asdict(result)) == {
        "message_count",
        "since_checkpoint_present",
        "since_count",
        "history_checkpoint_present",
        "query_ready",
    }
    output = capsys.readouterr()
    serialized = repr(result) + str(result) + json.dumps(asdict(result)) + output.out + output.err + caplog.text
    for private_value in (_PRIVATE_TOKEN, _PRIVATE_BODY, _PRIVATE_ADDRESS, _PRIVATE_ID):
        assert private_value not in serialized
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        result.message_count = 99  # type: ignore[misc]


@pytest.mark.parametrize("cleared_rows", [False, True])
def test_completed_since_scan_has_no_checkpoint_or_retained_count(tmp_path: Path, cleared_rows: bool) -> None:
    state: dict[str, str | bytes | None] = {}
    if cleared_rows:
        state = {"since_epoch": None, "since_page_token": None, "since_messages_synced": None}
    source = _cache(tmp_path, state)

    result = observer.observe_gmail_cache(source)

    assert result == observer.GmailCacheObservation(1, False, None, False, True)


@pytest.mark.parametrize("token", ["", "__fieldkit_exhausted__"])
def test_started_or_pending_finalization_scan_keeps_checkpoint(tmp_path: Path, token: str) -> None:
    source = _cache(tmp_path, {"since_epoch": "1780000000", "since_page_token": token, "since_messages_synced": "0"})

    result = observer.observe_gmail_cache(source)

    assert result == observer.GmailCacheObservation(1, True, 0, False, True)


@pytest.mark.parametrize(
    "state",
    [
        {"since_epoch": "1780000000", "since_page_token": _PRIVATE_TOKEN},
        {"since_page_token": _PRIVATE_TOKEN},
        {"since_messages_synced": "1"},
        {"since_epoch": _PRIVATE_TOKEN, "since_page_token": "", "since_messages_synced": "0"},
        {"since_epoch": "1780000000", "since_page_token": b"private-byte-token", "since_messages_synced": "0"},
        {"since_epoch": "1780000000", "since_page_token": "x" * 4097, "since_messages_synced": "0"},
        {"last_history_id": ""},
        {"last_history_id": b"private-byte-token"},
        {"last_history_id": "x" * 4097},
        {"last_history_id": "x\0" + "x" * 4097},
        {"last_history_id": "\u00e9" * 2049},
        {"fieldkit_query_ready": "false"},
        {"fieldkit_query_ready": None},
        {"fieldkit_query_ready": _PRIVATE_TOKEN},
    ],
)
def test_malformed_checkpoint_or_unready_cache_fails_without_raw_values(
    tmp_path: Path, state: dict[str, str | bytes | None]
) -> None:
    source = _cache(tmp_path, state)

    with pytest.raises(observer.GmailCacheObservationError, match="Gmail cache") as caught:
        observer.observe_gmail_cache(source)

    assert _PRIVATE_TOKEN not in "".join(traceback.format_exception(caught.value))
    assert "private-byte-token" not in str(caught.value)


@pytest.mark.parametrize(
    "count",
    ["-1", "1.5", "01", "", "NaN", "\uff19", "9223372036854775808", _PRIVATE_TOKEN, "1\0private", "9" * 100_000],
)
def test_invalid_since_counts_are_rejected(tmp_path: Path, count: str) -> None:
    source = _cache(tmp_path, {"since_epoch": "1780000000", "since_page_token": "", "since_messages_synced": count})

    with pytest.raises(observer.GmailCacheObservationError, match="count") as caught:
        observer.observe_gmail_cache(source)

    assert _PRIVATE_TOKEN not in str(caught.value)


def test_message_count_bound_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _cache(tmp_path, {})
    monkeypatch.setattr(observer, "MAX_OBSERVED_COUNT", 0)

    with pytest.raises(observer.GmailCacheObservationError, match="message count"):
        observer.observe_gmail_cache(source)


@pytest.mark.parametrize("failure", ["missing", "raw", "corrupt", "incomplete"])
def test_unavailable_or_unverified_snapshot_fails(tmp_path: Path, failure: str) -> None:
    source = tmp_path / "gmail.db"
    if failure == "raw":
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE messages (message_id TEXT)")
    elif failure in {"corrupt", "incomplete"}:
        source = _cache(tmp_path, {})
        if failure == "corrupt":
            root = publication_root_for(source)
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            (root / receipt["artifact"]["path"]).write_bytes(_PRIVATE_BODY.encode())
        else:

            def fail_page(_connection: SQLiteMutationConnection) -> None:
                raise RuntimeError("synthetic failed page")

            with pytest.raises(RuntimeError, match="synthetic failed page"):
                apply_gmail_page(source, fail_page)

    with pytest.raises(observer.GmailCacheObservationError, match="snapshot is unavailable or invalid") as caught:
        observer.observe_gmail_cache(source)

    assert _PRIVATE_BODY not in "".join(traceback.format_exception(caught.value))


def test_observer_queries_only_fixed_aggregate_and_allowlisted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _cache(tmp_path, {"last_history_id": _PRIVATE_TOKEN})
    statements: list[str] = []
    connections: list[sqlite3.Connection] = []

    def traced_opener(path: Path) -> sqlite3.Connection:
        connection = open_gmail_publication(path)
        connections.append(connection)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(observer, "open_gmail_publication", traced_opener)

    result = observer.observe_gmail_cache(source)

    assert result == observer.GmailCacheObservation(1, False, None, True, True)
    assert statements == [
        "SELECT COUNT(*) FROM messages",
        "SELECT key, typeof(value) AS value_type, coalesce(length(CAST(value AS BLOB)), 0) AS byte_length, "
        "CASE WHEN key IN ('since_epoch', 'since_messages_synced') AND typeof(value) = 'text' "
        "AND length(CAST(value AS BLOB)) BETWEEN 1 AND 19 THEN CASE WHEN length(value) = "
        "length(CAST(value AS BLOB)) AND value NOT GLOB '*[^0-9]*' THEN substr(value, 1, 19) "
        "END END AS numeric_text, CASE WHEN key = 'fieldkit_query_ready' AND value = 'true' "
        "THEN 1 ELSE 0 END AS query_ready FROM sync_state WHERE key IN ('since_epoch', 'since_page_token', "
        "'since_messages_synced', 'last_history_id', 'fieldkit_query_ready')",
    ]
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("case", ["valid", "blob", "oversize", "malformed_numeric", "oversize_numeric", "unready"])
def test_private_checkpoint_bytes_never_cross_sql_fetch_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    state: dict[str, str | bytes | None] = {
        "since_epoch": "1780000000",
        "since_page_token": _PRIVATE_TOKEN,
        "since_messages_synced": "500",
        "last_history_id": _PRIVATE_TOKEN,
    }
    if case == "blob":
        state["since_page_token"] = _PRIVATE_TOKEN.encode()
    elif case == "oversize":
        state["last_history_id"] = _PRIVATE_TOKEN * 10_000
    elif case == "malformed_numeric":
        state["since_epoch"] = _PRIVATE_TOKEN
    elif case == "oversize_numeric":
        state["since_messages_synced"] = "9" * 100_000
    elif case == "unready":
        state["fieldkit_query_ready"] = _PRIVATE_TOKEN * 10_000
    source = _cache(tmp_path, state)
    fetched: list[tuple[object, ...]] = []

    def inspected_opener(path: Path) -> MagicMock:
        connection = open_gmail_publication(path)
        proxy = MagicMock(wraps=connection)

        def execute(sql: str, parameters: tuple[str, ...] = ()) -> MagicMock:
            cursor = connection.execute(sql, parameters)
            cursor_proxy = MagicMock(wraps=cursor)

            def fetchall() -> list[sqlite3.Row]:
                rows: list[sqlite3.Row] = cursor.fetchall()
                fetched.extend(tuple(row) for row in rows)
                return rows

            cursor_proxy.fetchall.side_effect = fetchall
            return cursor_proxy

        proxy.execute.side_effect = execute
        return proxy

    monkeypatch.setattr(observer, "open_gmail_publication", inspected_opener)
    if case == "valid":
        result = observer.observe_gmail_cache(source)
        assert result == observer.GmailCacheObservation(1, True, 500, True, True)
    else:
        with pytest.raises(observer.GmailCacheObservationError, match="Gmail cache"):
            observer.observe_gmail_cache(source)

    assert len(fetched) == 5
    assert _PRIVATE_TOKEN not in repr(fetched)
    assert "9" * 20 not in repr(fetched)
    assert all(row[3] is None for row in fetched if row[0] in {"since_page_token", "last_history_id"})
    assert all(row[3] is None or (isinstance(row[3], str) and len(row[3]) <= 19) for row in fetched)
