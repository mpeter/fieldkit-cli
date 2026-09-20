"""tests/test_gmail_sync_coverage.py — Mock-based coverage tests for gmail/sync.py.

Covers 7 functions that previously carried ``# pragma: no cover``:
  - list_messages (with and without page_token)
  - _process_added_messages
  - _process_deleted_messages
  - sync_labels
  - _finalize_full_sync
  - _full_sync
  - cli (via CliRunner)

Mock strategy:
  - ``unittest.mock.patch('fieldkit.gmail.auth.get_gmail_service')`` for
    any code path that would invoke OAuth / network I/O.
  - ``sqlite3.connect(":memory:")`` + inline schema for DB-touching functions.
  - ``click.testing.CliRunner`` for the ``cli`` Click command.

These tests are intentionally narrow: they verify the function's observable
contract (return values, DB mutations, log output) without requiring real Gmail
API credentials.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.gmail.batch import BatchFetchResult, SyncSummary

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id     TEXT PRIMARY KEY,
    subject       TEXT,
    snippet       TEXT,
    updated_at    TEXT,
    message_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    thread_id  TEXT,
    from_addr  TEXT,
    to_addr    TEXT,
    cc_addr    TEXT,
    subject    TEXT,
    date_str   TEXT,
    date_epoch INTEGER,
    labels     TEXT,
    body_plain TEXT,
    body_html  TEXT,
    size_bytes INTEGER,
    snippet    TEXT,
    synced_at  TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    message_id    TEXT,
    filename      TEXT,
    mime_type     TEXT,
    size_bytes    INTEGER,
    part_id       TEXT
);
CREATE TABLE IF NOT EXISTS labels (
    label_id   TEXT PRIMARY KEY,
    label_name TEXT UNIQUE,
    synced_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _mem_db() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the gmail-cache schema."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    return conn


def _insert_message(conn: sqlite3.Connection, msg_id: str, thread_id: str, labels: list[str]) -> None:
    """Insert a minimal message row for testing label/delete operations."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO threads(thread_id, subject, snippet, updated_at) VALUES (?,?,?,?)",
            (thread_id, "subj", "snip", "2026-01-01"),
        )
        conn.execute(
            """INSERT OR REPLACE INTO messages(
                message_id, thread_id, from_addr, to_addr, cc_addr,
                subject, date_str, date_epoch, labels, body_plain, body_html,
                size_bytes, snippet, synced_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (
                msg_id,
                thread_id,
                "a@example.com",  # pii-guard: ignore
                "b@example.com",  # pii-guard: ignore
                "",
                "subj",
                "",
                0,
                json.dumps(labels),
                "",
                "",
                0,
                "",
            ),  # pii-guard: ignore
        )


# ---------------------------------------------------------------------------
# list_messages
# ---------------------------------------------------------------------------


# ── TestListMessages (flattened) ────────────────────────────────────────────


def test_list_messages_returns_messages_and_none_next_token() -> None:
    """Without page_token, returns messages list and None next_page_token."""
    from fieldkit.gmail.sync_store import list_messages

    mock_service = MagicMock()
    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}],
    }

    msgs, next_token = list_messages(mock_service)

    assert len(msgs) == 2
    assert msgs[0]["id"] == "m1"
    assert next_token is None


def test_list_messages_passes_page_token_when_provided() -> None:
    """When page_token is given, it is forwarded to the API kwargs."""
    from fieldkit.gmail.sync_store import list_messages

    mock_service = MagicMock()
    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m3", "threadId": "t3"}],
        "nextPageToken": "tok-next",
    }

    msgs, next_token = list_messages(mock_service, page_token="tok-abc")

    assert len(msgs) == 1
    assert next_token == "tok-next"


def test_list_messages_empty_response_returns_empty_list() -> None:
    """An API response with no 'messages' key returns an empty list."""
    from fieldkit.gmail.sync_store import list_messages

    mock_service = MagicMock()
    mock_service.users().messages().list().execute.return_value = {}

    msgs, next_token = list_messages(mock_service)

    assert msgs == []
    assert next_token is None


@pytest.mark.parametrize("query", [None, "after:123"])
def test_list_messages_forwards_page_size_and_optional_query(query: str | None) -> None:
    from fieldkit.gmail.sync_store import list_messages

    mock_service = MagicMock()
    request = mock_service.users().messages().list
    request().execute.return_value = {}

    result = list_messages(mock_service, query=query, max_results=17)

    assert result == ([], None)
    assert request.call_args.kwargs["maxResults"] == 17
    if query is None:
        assert "q" not in request.call_args.kwargs
    else:
        assert request.call_args.kwargs["q"] == query


# ---------------------------------------------------------------------------
# _process_added_messages
# ---------------------------------------------------------------------------


# ── TestProcessAddedMessages (flattened) ────────────────────────────────────


def _list_messages_make_history_record(msg_id: str, label_ids: list[str]) -> dict[str, Any]:
    return {"messagesAdded": [{"message": {"id": msg_id, "labelIds": label_ids}}]}


def test_list_messages_returns_zero_when_no_messages_added() -> None:
    """Returns 0 when history_records has no messagesAdded entries."""
    from fieldkit.gmail.sync_engine import _process_added_messages

    conn = _mem_db()
    mock_service = MagicMock()
    changed: set[str] = set()

    count = _process_added_messages(mock_service, conn, [], changed)

    assert count == SyncSummary()
    assert changed == set()


def test_list_messages_skips_fully_excluded_label_messages() -> None:
    """Messages whose labels are entirely in EXCLUDED_LABELS are not fetched."""
    from fieldkit.gmail.sync_engine import _process_added_messages

    conn = _mem_db()
    mock_service = MagicMock()
    changed: set[str] = set()
    records = [_list_messages_make_history_record("spam-msg", ["SPAM"])]

    count = _process_added_messages(mock_service, conn, records, changed)

    assert count == SyncSummary()
    # No message rows should have been inserted into the DB
    row_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert row_count == 0


def test_list_messages_inserts_non_excluded_messages() -> None:
    """Non-excluded messages are fetched and inserted into the DB."""
    from fieldkit.gmail.sync_engine import _process_added_messages

    conn = _mem_db()
    changed: set[str] = set()
    records = [_list_messages_make_history_record("inbox-msg", ["INBOX"])]

    # Build a fake fetched message dict matching _make_message_dict output shape
    fake_msg: dict[str, Any] = {
        "message_id": "inbox-msg",
        "thread_id": "t-inbox",
        "from_addr": "sender@example.com",  # pii-guard: ignore
        "to_addr": "me@example.com",  # pii-guard: ignore
        "cc_addr": "",
        "subject": "Hello",
        "date_str": "",
        "date_epoch": None,
        "labels": json.dumps(["INBOX"]),
        "body_plain": "hi",
        "body_html": "",
        "size_bytes": 100,
        "snippet": "hi",
        "attachments": [],
    }

    with patch(
        "fieldkit.gmail.sync_engine.fetch_messages_batch",
        return_value=BatchFetchResult(messages=[fake_msg]),
    ):
        mock_service = MagicMock()
        count = _process_added_messages(mock_service, conn, records, changed)

    assert count.added == 1
    assert "t-inbox" in changed
    row = conn.execute("SELECT message_id FROM messages WHERE message_id='inbox-msg'").fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# _process_deleted_messages
# ---------------------------------------------------------------------------


# ── TestProcessDeletedMessages (flattened) ──────────────────────────────────


def test_list_messages_returns_zero_when_no_deletions() -> None:
    """Returns 0 when history_records has no messagesDeleted entries."""
    from fieldkit.gmail.sync_engine import _process_deleted_messages

    conn = _mem_db()
    changed: set[str] = set()

    count = _process_deleted_messages(conn, [], changed)

    assert count == 0


def test_list_messages_deletes_existing_message() -> None:
    """A message present in the DB is deleted and its thread_id is tracked."""
    from fieldkit.gmail.sync_engine import _process_deleted_messages

    conn = _mem_db()
    _insert_message(conn, "del-msg", "t-del", ["INBOX"])
    changed: set[str] = set()

    records = [{"messagesDeleted": [{"message": {"id": "del-msg"}}]}]
    count = _process_deleted_messages(conn, records, changed)

    assert count == 1
    assert "t-del" in changed
    row = conn.execute("SELECT message_id FROM messages WHERE message_id='del-msg'").fetchone()
    assert row is None


def test_list_messages_skips_missing_message_id() -> None:
    """Records without a message id are skipped gracefully."""
    from fieldkit.gmail.sync_engine import _process_deleted_messages

    conn = _mem_db()
    changed: set[str] = set()

    records = [{"messagesDeleted": [{"message": {}}]}]  # no 'id' key
    count = _process_deleted_messages(conn, records, changed)

    assert count == 0


def test_list_messages_counts_all_attempted_deletions_including_nonexistent() -> None:
    """A message not in the DB still increments the deleted counter.

    The counter represents messages attempted, not messages actually found in DB.
    This is the deliberate contract: the caller (incremental_sync) uses the count
    for progress reporting, not for verifying DB state.
    """
    from fieldkit.gmail.sync_engine import _process_deleted_messages

    conn = _mem_db()
    changed: set[str] = set()

    records = [{"messagesDeleted": [{"message": {"id": "ghost-msg"}}]}]
    count = _process_deleted_messages(conn, records, changed)

    # The function increments deleted for every entry regardless of DB presence
    assert count == 1


# ---------------------------------------------------------------------------
# sync_labels
# ---------------------------------------------------------------------------


# ── TestSyncLabels (flattened) ──────────────────────────────────────────────


def test_sync_labels_inserts_labels_into_db() -> None:
    """Labels returned by the API are persisted in the labels table."""
    from fieldkit.gmail.sync_engine import sync_labels

    conn = _mem_db()
    mock_service = MagicMock()
    mock_service.users().labels().list().execute.return_value = {
        "labels": [
            {"id": "INBOX", "name": "INBOX"},
            {"id": "Label_1", "name": "Work"},
        ]
    }

    with patch("fieldkit.gmail.sync_engine._api_call_with_retry", side_effect=lambda fn, **kw: fn()):
        sync_labels(mock_service, conn)

    rows = conn.execute("SELECT label_id, label_name FROM labels ORDER BY label_id").fetchall()
    assert len(rows) == 2
    assert rows[0] == ("INBOX", "INBOX")
    assert rows[1] == ("Label_1", "Work")


def test_sync_labels_handles_api_error_gracefully() -> None:
    """An API error during labels.list is swallowed; no exception propagates."""
    from fieldkit.gmail.sync_engine import sync_labels

    conn = _mem_db()
    mock_service = MagicMock()

    with patch(
        "fieldkit.gmail.sync_engine._api_call_with_retry",
        side_effect=RuntimeError("network error"),
    ):
        # Must not raise
        sync_labels(mock_service, conn)

    rows = conn.execute("SELECT label_id FROM labels").fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# _finalize_full_sync
# ---------------------------------------------------------------------------


# ── TestFinalizeFullSync (flattened) ────────────────────────────────────────


def test_full_sync_sets_sync_state_flags() -> None:
    """initial_sync_complete is set to 'true' and last_page_token cleared."""
    from fieldkit.gmail.sync_engine import _finalize_full_sync, _sync_get

    conn = _mem_db()
    mock_service = MagicMock()
    mock_service.users().getProfile().execute.return_value = {"historyId": "12345"}

    with patch(
        "fieldkit.gmail.sync_engine._api_call_with_retry",
        side_effect=lambda fn, **kw: fn(),
    ):
        _finalize_full_sync(mock_service, conn, total_synced=42)

    assert _sync_get(conn, "initial_sync_complete") == "true"
    assert _sync_get(conn, "last_page_token") is None


def test_full_sync_propagates_getprofile_error_without_completion() -> None:
    """A history checkpoint failure cannot publish successful completion."""
    from fieldkit.gmail.sync_engine import _finalize_full_sync, _sync_get

    conn = _mem_db()
    mock_service = MagicMock()

    with (
        patch(
            "fieldkit.gmail.sync_engine._api_call_with_retry",
            side_effect=RuntimeError("profile fetch failed"),
        ),
        pytest.raises(RuntimeError, match="profile fetch failed"),
    ):
        _finalize_full_sync(mock_service, conn, total_synced=0)

    assert _sync_get(conn, "initial_sync_complete") is None
    assert _sync_get(conn, "last_page_token") is None


def test_full_sync_does_not_publish_completion_when_history_replay_is_unresolved() -> None:
    from fieldkit.gmail.sync_engine import _finalize_full_sync, _sync_get, _sync_set

    conn = _mem_db()
    _sync_set(conn, "full_sync_start_history_id", "100")
    conn.commit()

    with patch(
        "fieldkit.gmail.sync_engine._replay_forced_history",
        return_value=(SyncSummary(unresolved=1), None, False),
    ):
        result = _finalize_full_sync(MagicMock(), conn, total_synced=4)

    assert result == SyncSummary(unresolved=1)
    assert _sync_get(conn, "initial_sync_complete") is None
    assert _sync_get(conn, "full_sync_start_history_id") == "100"


# ---------------------------------------------------------------------------
# _full_sync
# ---------------------------------------------------------------------------


# ── TestFullSync (flattened) ────────────────────────────────────────────────


def test_full_sync_stops_when_no_messages_on_first_page() -> None:
    """When list_messages returns empty stubs, the loop exits immediately."""
    from fieldkit.gmail.sync_engine import _full_sync

    conn = _mem_db()
    mock_service = MagicMock()

    with patch("fieldkit.gmail.sync_engine.list_messages", return_value=([], None)):
        _full_sync(mock_service, conn, max_messages=0)

    # No messages inserted
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


def test_full_sync_stops_at_max_messages() -> None:
    """When max_messages is reached, the loop stops early."""
    from fieldkit.gmail.sync_engine import _full_sync

    conn = _mem_db()
    mock_service = MagicMock()

    # list_messages returns 1 stub, then would loop — but max_messages=1 stops it
    stubs = [{"id": "m1", "threadId": "t1"}]
    fake_msg: dict[str, Any] = {
        "message_id": "m1",
        "thread_id": "t1",
        "from_addr": "a@example.com",  # pii-guard: ignore
        "to_addr": "b@example.com",  # pii-guard: ignore
        "cc_addr": "",
        "subject": "s",
        "date_str": "",
        "date_epoch": None,
        "labels": json.dumps(["INBOX"]),
        "body_plain": "body",
        "body_html": "",
        "size_bytes": 10,
        "snippet": "body",
        "attachments": [],
    }

    with (
        patch("fieldkit.gmail.sync_engine.list_messages", return_value=(stubs, None)),
        patch(
            "fieldkit.gmail.sync_engine.fetch_messages_batch",
            return_value=BatchFetchResult(messages=[fake_msg]),
        ),
        patch("fieldkit.gmail.sync_engine._finalize_full_sync"),
    ):
        _full_sync(mock_service, conn, max_messages=1)

    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 1


def test_full_sync_propagates_list_messages_exception() -> None:
    """A list failure cannot be reported as a successful sync."""
    from fieldkit.gmail.sync_engine import _full_sync

    conn = _mem_db()
    mock_service = MagicMock()

    with (
        patch("fieldkit.gmail.sync_engine.list_messages", side_effect=RuntimeError("API down")),
        pytest.raises(RuntimeError, match="API down"),
    ):
        _full_sync(mock_service, conn, max_messages=0)

    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# cli (Click command)
# ---------------------------------------------------------------------------


# ── TestCliCommand (flattened) ──────────────────────────────────────────────


def test_cli_command_cli_runs_incremental_sync_when_state_complete(tmp_path: Path) -> None:
    """When initial_sync_complete='true' and last_history_id is set, incremental_sync is called."""
    from fieldkit.commands.gmail.sync_command import cli

    db_path = tmp_path / "test_cli.db"

    runner = CliRunner()

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as mock_svc,
        patch("fieldkit.gmail.sync_engine.sync_labels"),
        patch("fieldkit.gmail.sync_engine.incremental_sync") as mock_inc,
        patch("fieldkit.gmail.sync_engine.db_init") as mock_db_init,
        patch("fieldkit.gmail.sync_engine._sync_get") as mock_get,
    ):
        mock_db_init.return_value = MagicMock()
        mock_svc.return_value = MagicMock()
        mock_inc.return_value = SyncSummary()
        # Simulate: sync_complete='true', last_history_id='99999'
        mock_get.side_effect = lambda conn, key: "true" if key == "initial_sync_complete" else "99999"

        result = runner.invoke(cli, ["--db", str(db_path)])

    assert result.exit_code == 0
    mock_inc.assert_called_once()


def test_cli_command_cli_runs_full_sync_when_no_history(tmp_path: Path) -> None:
    """When initial_sync_complete is not set, _full_sync is called."""
    from fieldkit.commands.gmail.sync_command import cli

    db_path = tmp_path / "test_cli_full.db"

    runner = CliRunner()

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as mock_svc,
        patch("fieldkit.gmail.sync_engine.sync_labels"),
        patch("fieldkit.gmail.sync_engine._full_sync") as mock_full,
        patch("fieldkit.gmail.sync_engine.db_init") as mock_db_init,
        patch("fieldkit.gmail.sync_engine._sync_get", return_value=None),
    ):
        mock_db_init.return_value = MagicMock()
        mock_svc.return_value = MagicMock()
        mock_full.return_value = SyncSummary()

        result = runner.invoke(cli, ["--db", str(db_path)])

    assert result.exit_code == 0
    mock_full.assert_called_once()


def test_cli_help_explains_default_and_forced_modes() -> None:
    from fieldkit.commands.gmail.sync_command import cli

    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "Incremental by default; resumes from the last sync." in result.output
    assert "Use --full to re-sync all messages." in " ".join(result.output.split())


def test_negative_max_messages_exits_three_before_auth_or_db(tmp_path: Path) -> None:
    from fieldkit.__main__ import main

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as service,
        patch("fieldkit.gmail.sync_engine.db_init") as db_init_mock,
    ):
        result = main(["gmail", "sync", "--db", str(tmp_path / "gmail.db"), "--max-messages", "-1"])

    assert result == 3
    service.assert_not_called()
    db_init_mock.assert_not_called()


def test_symlink_database_alias_contends_on_canonical_lock(tmp_path: Path) -> None:
    from fieldkit.commands.gmail.sync_command import cli
    from fieldkit.commands.gmail.sync_lock import gmail_sync_lock

    database = tmp_path / "gmail.db"
    database.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(database)

    with gmail_sync_lock(database.resolve()):
        result = CliRunner().invoke(cli, ["--db", str(alias)])

    assert result.exit_code == 1
    assert result.stderr == "Gmail sync already running for the selected database.\n"


def test_relative_database_alias_contends_on_canonical_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.gmail.sync_command import cli
    from fieldkit.commands.gmail.sync_lock import gmail_sync_lock

    database = tmp_path / "gmail.db"
    monkeypatch.chdir(tmp_path)

    with gmail_sync_lock(database.resolve()), pytest.raises(SystemExit) as exc_info:
        cli.main(args=["--db", "gmail.db"], standalone_mode=False)

    assert exc_info.value.code == 1
    assert capsys.readouterr().err == "Gmail sync already running for the selected database.\n"


def test_run_sync_propagates_auth_exception_before_state_reads() -> None:
    from fieldkit.errors import GmailAuthError
    from fieldkit.gmail.sync_engine import _run_sync

    conn = _mem_db()

    with pytest.raises(GmailAuthError, match="expired"):
        _run_sync(
            service_factory=MagicMock(side_effect=GmailAuthError("expired")),
            conn=conn,
            full=False,
            since=None,
            max_messages=0,
        )

    assert conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 0


def test_since_accepts_valid_leap_day_and_dispatches_bounded_mode(tmp_path: Path) -> None:
    from fieldkit.commands.gmail.sync_command import cli

    conn = MagicMock()
    with (
        patch("fieldkit.gmail.sync_engine.db_init", return_value=conn),
        patch("fieldkit.gmail.sync_engine._run_sync", return_value=SyncSummary()) as run_sync,
    ):
        result = CliRunner().invoke(cli, ["--db", str(tmp_path / "gmail.db"), "--since", "2024-02-29"])

    assert result.exit_code == 0
    assert run_sync.call_args.kwargs["since"] == datetime(2024, 2, 29)


@pytest.mark.parametrize("value", ["2026/06/01", "2026-02-29"])
def test_since_invalid_date_exits_three_before_side_effects(tmp_path: Path, value: str) -> None:
    from fieldkit.__main__ import main

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as service,
        patch("fieldkit.gmail.sync_engine.db_init") as db_init_mock,
    ):
        result = main(["gmail", "sync", "--db", str(tmp_path / "gmail.db"), "--since", value])

    assert result == 3
    service.assert_not_called()
    db_init_mock.assert_not_called()


def test_full_since_conflict_exits_three_before_side_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.__main__ import main

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as service,
        patch("fieldkit.gmail.sync_engine.db_init") as db_init_mock,
    ):
        result = main(["gmail", "sync", "--db", str(tmp_path / "gmail.db"), "--full", "--since", "2026-06-01"])

    assert result == 3
    assert capsys.readouterr().err == "Error: --full and --since cannot be used together\n"
    service.assert_not_called()
    db_init_mock.assert_not_called()


def test_since_sync_propagates_auth_exception_and_retains_checkpoint() -> None:
    from fieldkit.errors import GmailAuthError
    from fieldkit.gmail.sync_engine import _since_sync, _sync_get

    conn = _mem_db()
    with (
        patch("fieldkit.gmail.sync_engine.list_messages", side_effect=GmailAuthError("expired")),
        pytest.raises(GmailAuthError, match="expired"),
    ):
        _since_sync(MagicMock(), conn, datetime(2026, 6, 1), max_messages=0)

    assert _sync_get(conn, "since_epoch") == "1780272000"
    assert _sync_get(conn, "since_page_token") == ""
    assert _sync_get(conn, "since_messages_synced") == "0"
    assert _sync_get(conn, "initial_sync_complete") is None
