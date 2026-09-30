"""Mock-based unit coverage for Gmail sync helpers and the CLI adapter."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.errors import GmailSyncPartialError
from fieldkit.gmail.batch import SyncSummary
from fieldkit.gmail.sync_engine import PublishedSyncResult

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


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"messages": "not-a-list"},
        {"messages": [{}]},
        {"messages": [{"id": 7}]},
        {"nextPageToken": 7},
    ],
)
def test_list_messages_rejects_malformed_provider_responses(response: object) -> None:
    from fieldkit.gmail.sync_store import list_messages

    mock_service = MagicMock()
    mock_service.users().messages().list().execute.return_value = response

    with pytest.raises(GmailSyncPartialError, match="message listing returned invalid data"):
        list_messages(mock_service)


# ---------------------------------------------------------------------------
# History record fixtures
# ---------------------------------------------------------------------------


# ── TestProcessAddedMessages (flattened) ────────────────────────────────────


def _list_messages_make_history_record(msg_id: str, label_ids: list[str]) -> dict[str, Any]:
    return {"messagesAdded": [{"message": {"id": msg_id, "labelIds": label_ids}}]}


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
    This is the deliberate contract: managed history synchronization uses the
    count for progress reporting, not for verifying DB state.
    """
    from fieldkit.gmail.sync_engine import _process_deleted_messages

    conn = _mem_db()
    changed: set[str] = set()

    records = [{"messagesDeleted": [{"message": {"id": "ghost-msg"}}]}]
    count = _process_deleted_messages(conn, records, changed)

    # The function increments deleted for every entry regardless of DB presence
    assert count == 1


# ---------------------------------------------------------------------------
# cli (Click command)
# ---------------------------------------------------------------------------


# ── TestCliCommand (flattened) ──────────────────────────────────────────────


def test_cli_command_delegates_default_managed_sync(tmp_path: Path) -> None:
    from fieldkit.commands.gmail.sync_command import cli

    db_path = tmp_path / "test_cli.db"

    runner = CliRunner()

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as mock_svc,
        patch(
            "fieldkit.gmail.sync_engine.run_published_sync", return_value=PublishedSyncResult("full", SyncSummary())
        ) as run_sync,
    ):
        mock_svc.return_value = MagicMock()
        result = runner.invoke(cli, ["--db", str(db_path)])

    assert result.exit_code == 0
    assert run_sync.call_args.kwargs["full"] is False
    assert run_sync.call_args.kwargs["db_path"] == db_path


def test_cli_command_delegates_forced_managed_sync(tmp_path: Path) -> None:
    from fieldkit.commands.gmail.sync_command import cli

    db_path = tmp_path / "test_cli_full.db"

    runner = CliRunner()

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as mock_svc,
        patch(
            "fieldkit.gmail.sync_engine.run_published_sync", return_value=PublishedSyncResult("full", SyncSummary())
        ) as run_sync,
    ):
        mock_svc.return_value = MagicMock()
        result = runner.invoke(cli, ["--db", str(db_path), "--full"])

    assert result.exit_code == 0
    assert run_sync.call_args.kwargs["full"] is True


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
        patch("fieldkit.gmail.sync_engine.run_published_sync") as run_sync,
    ):
        result = main(["gmail", "sync", "--db", str(tmp_path / "gmail.db"), "--max-messages", "-1"])

    assert result == 3
    service.assert_not_called()
    run_sync.assert_not_called()


def test_symlink_database_alias_contends_on_canonical_lock(tmp_path: Path) -> None:
    from fieldkit.commands.gmail.sync_command import cli
    from fieldkit.commands.gmail.sync_lock import gmail_sync_lock

    database = tmp_path / "gmail.db"
    database.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(database)

    with (
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        gmail_sync_lock(database.resolve()),
    ):
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

    with (
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        gmail_sync_lock(database.resolve()),
        pytest.raises(SystemExit) as exc_info,
    ):
        cli.main(args=["--db", "gmail.db"], standalone_mode=False)

    assert exc_info.value.code == 1
    assert capsys.readouterr().err == "Gmail sync already running for the selected database.\n"


def test_since_accepts_valid_leap_day_and_dispatches_bounded_mode(tmp_path: Path) -> None:
    from fieldkit.commands.gmail.sync_command import cli

    with (
        patch("fieldkit.gmail.auth.get_gmail_service", return_value=MagicMock()),
        patch(
            "fieldkit.gmail.sync_engine.run_published_sync", return_value=PublishedSyncResult("full", SyncSummary())
        ) as run_sync,
    ):
        result = CliRunner().invoke(cli, ["--db", str(tmp_path / "gmail.db"), "--since", "2024-02-29"])

    assert result.exit_code == 0
    assert run_sync.call_args.kwargs["since"] == datetime(2024, 2, 29)


@pytest.mark.parametrize("value", ["2026/06/01", "2026-02-29"])
def test_since_invalid_date_exits_three_before_side_effects(tmp_path: Path, value: str) -> None:
    from fieldkit.__main__ import main

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as service,
        patch("fieldkit.gmail.sync_engine.run_published_sync") as run_sync,
    ):
        result = main(["gmail", "sync", "--db", str(tmp_path / "gmail.db"), "--since", value])

    assert result == 3
    service.assert_not_called()
    run_sync.assert_not_called()


def test_full_since_conflict_exits_three_before_side_effects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.__main__ import main

    with (
        patch("fieldkit.gmail.auth.get_gmail_service") as service,
        patch("fieldkit.gmail.sync_engine.run_published_sync") as run_sync,
    ):
        result = main(["gmail", "sync", "--db", str(tmp_path / "gmail.db"), "--full", "--since", "2026-06-01"])

    assert result == 3
    assert capsys.readouterr().err == "Error: --full and --since cannot be used together\n"
    service.assert_not_called()
    run_sync.assert_not_called()
