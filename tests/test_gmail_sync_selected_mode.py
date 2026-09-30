"""Selected-mode and persisted retry reporting through the real Gmail engine."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.sync_command import cli
from fieldkit.gmail import auth, sync_engine
from fieldkit.gmail.batch import BatchFetchResult, SyncSummary
from fieldkit.gmail.publication import apply_gmail_page, initialize_gmail_publication, open_gmail_publication
from fieldkit.gmail.sync_store import sync_get, sync_set
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("unresolved", [False, True], ids=["complete", "retry"])
@pytest.mark.parametrize("cache_state", ["fresh", "resumed", "incremental", "since", "replay"])
def test_json_reports_actual_selected_mode_and_persisted_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache_state: str,
    unresolved: bool,
) -> None:
    database = tmp_path / "gmail.db"
    since = datetime(2026, 6, 1, tzinfo=UTC)
    states: dict[str, dict[str, str]] = {
        "resumed": {
            "full_sync_requested": "initial",
            "full_sync_start_history_id": "100",
            "last_page_token": "full-next",
            "messages_synced": "2",
        },
        "incremental": {"initial_sync_complete": "true", "last_history_id": "100"},
        "since": {
            "initial_sync_complete": "true",
            "last_history_id": "100",
            "since_epoch": str(int(since.timestamp())),
            "since_page_token": "since-next",
            "since_messages_synced": "2",
        },
        "replay": {
            "full_sync_requested": "initial",
            "full_sync_start_history_id": "100",
            "full_scan_exhausted": "true",
            "full_replay_page_token": "replay-next",
        },
    }
    if cache_state != "fresh":
        initialize_gmail_publication(database)

        def seed(connection: SQLiteMutationConnection) -> None:
            for key, value in states[cache_state].items():
                sync_set(connection, key, value)

        apply_gmail_page(database, seed)

    monkeypatch.setattr(auth, "get_gmail_service", object)
    monkeypatch.setattr(sync_engine, "_fetch_labels", lambda _service: [])
    monkeypatch.setattr(sync_engine, "_capture_history_id", lambda _service: "100")
    list_calls: list[tuple[str | None, str | None]] = []
    history_calls: list[tuple[str, str | None]] = []

    def list_page(
        _service: object,
        page_token: str | None,
        *,
        query: str | None,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        del max_results
        list_calls.append((page_token, query))
        return ([{"id": "message-1"}] if unresolved else []), None

    def history_page(
        _service: object,
        *,
        start_history_id: str,
        page_token: str | None,
    ) -> dict[str, Any]:
        history_calls.append((start_history_id, page_token))
        return {
            "historyId": "101",
            "history": [{"messagesAdded": [{"message": {"id": "message-1"}}]}] if unresolved else [],
        }

    monkeypatch.setattr(sync_engine, "list_messages", list_page)
    monkeypatch.setattr(sync_engine, "_history_page", history_page)
    monkeypatch.setattr(
        sync_engine,
        "fetch_messages_batch",
        lambda *_args, **_kwargs: BatchFetchResult(messages=[], unresolved=1),
    )
    engine_results: list[sync_engine.PublishedSyncResult] = []
    run_sync = sync_engine.run_published_sync

    def capture_result(**kwargs: Any) -> sync_engine.PublishedSyncResult:
        result = run_sync(**kwargs)
        engine_results.append(result)
        return result

    monkeypatch.setattr(sync_engine, "run_published_sync", capture_result)
    arguments = ["--db", str(database), "--json"]
    if cache_state == "since":
        arguments += ["--since", "2026-06-01"]
    result = CliRunner().invoke(cli, arguments)

    assert result.exit_code == (1 if unresolved else 0), result.output
    expected_mode = "since" if cache_state == "since" else "incremental" if cache_state == "incremental" else "full"
    checkpoints = {
        "fresh": ("last_page_token", ""),
        "resumed": ("last_page_token", "full-next"),
        "incremental": ("last_history_id", "100"),
        "since": ("since_page_token", "since-next"),
        "replay": ("full_replay_page_token", "replay-next"),
    }
    checkpoint_key, checkpoint = checkpoints[cache_state]
    assert len(engine_results) == 1
    engine_result = engine_results[0]
    assert engine_result.summary == SyncSummary(unresolved=int(unresolved))
    assert engine_result.mode == expected_mode
    assert engine_result.checkpoint_key == (checkpoint_key if unresolved else None)
    assert engine_result.checkpoint == (checkpoint if unresolved else None)
    assert json.loads(result.output) == {
        "mode": expected_mode,
        "added": 0,
        "failed": int(unresolved),
        "not_found": 0,
        "unresolved": int(unresolved),
        "partial": unresolved,
        "retry": {
            "required": unresolved,
            "checkpoint_key": checkpoint_key if unresolved else None,
            "checkpoint": checkpoint if unresolved else None,
        },
    }
    assert str(tmp_path) not in result.output
    if cache_state in {"incremental", "replay"}:
        assert list_calls == []
        assert history_calls == [("100", "replay-next" if cache_state == "replay" else None)]
    else:
        expected_token = "since-next" if cache_state == "since" else "full-next" if cache_state == "resumed" else ""
        assert list_calls == [(expected_token, "after:1780271999" if cache_state == "since" else None)]
    with open_gmail_publication(database) as connection:
        if unresolved:
            assert sync_get(connection, checkpoint_key) == checkpoint
            assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        if cache_state == "since":
            assert sync_get(connection, "last_history_id") == "100"
            assert sync_get(connection, "initial_sync_complete") == "true"
        elif not unresolved:
            assert sync_get(connection, "last_history_id") == "101"
            assert sync_get(connection, "initial_sync_complete") == "true"
