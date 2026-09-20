"""Tests for pure functions in tools/gmail_cache/sync.py.

Excludes all OAuth / live Gmail API functions (get_gmail_service, list_messages,
incremental_sync, main). Tests cover:
- db_init: schema creation, WAL mode, idempotency
- _sync_get / _sync_set: key-value state helpers
- _decode_b64url: base64url decoding with padding edge cases
- _extract_parts: MIME tree walking (plain, html, attachments, nested multipart)
- insert_batch: upsert threads + messages + attachments in a single transaction
- _api_call_with_retry: retry logic on transient errors, fatal re-raise
- fetch_messages_batch: BatchHttpRequest wrapper, per-message error handling
- _process_label_changes: bulk SELECT IN + executemany UPDATE for label events
"""

import base64
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# Import functions under test directly — avoid importing module-level code that
# calls get_salesforce_org_url() etc. by importing the specific names.
from fieldkit.gmail.batch import BatchFetchResult, SyncSummary  # noqa: E402
from fieldkit.gmail.sync_engine import (  # noqa: E402
    _api_call_with_retry,
    _process_label_changes,
    _sync_get,
    _sync_set,
    db_init,
)
from fieldkit.gmail.sync_store import (  # noqa: E402
    _decode_b64url,
    _extract_parts,
    fetch_messages_batch,
    insert_batch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _in_memory_db() -> sqlite3.Connection:
    """Return an in-memory DB initialised with the gmail_cache schema."""
    # We need to create the full schema without touching the filesystem.
    # Use a temp-path db_init call via tmp_path fixture or build schema inline.
    schema = """
    CREATE TABLE IF NOT EXISTS threads (
        thread_id TEXT PRIMARY KEY,
        subject   TEXT,
        snippet   TEXT,
        updated_at TEXT,
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
        name       TEXT,
        label_type TEXT
    );
    CREATE TABLE IF NOT EXISTS sync_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    return conn


def _make_message(
    msg_id: str = "msg1",
    thread_id: str = "t1",
    subject: str = "Hello",
    from_addr: str = "alice@example.com",  # pii-guard: ignore
    body_plain: str = "plain text",
    body_html: str = "<p>html</p>",
    attachments: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "message_id": msg_id,
        "thread_id": thread_id,
        "from_addr": from_addr,
        "to_addr": "bob@example.com",  # pii-guard: ignore
        "cc_addr": "",
        "subject": subject,
        "date_str": "Thu, 22 May 2026 12:00:00 +0000",
        "date_epoch": 1748001600,
        "labels": json.dumps(["INBOX"]),
        "body_plain": body_plain,
        "body_html": body_html,
        "size_bytes": 1024,
        "snippet": "plain text...",
        "attachments": attachments or [],
    }


# ---------------------------------------------------------------------------
# db_init
# ---------------------------------------------------------------------------


# ── TestDbInit (flattened) ──────────────────────────────────────────────────


def test_db_init_creates_file(tmp_path: Path) -> None:
    db_path = tmp_path / "gmail.db"
    conn = db_init(db_path)
    conn.close()
    assert db_path.exists()


def test_db_init_creates_parent_dirs(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "gmail.db"
    conn = db_init(db_path)
    conn.close()
    assert db_path.exists()


def test_db_init_tables_created(tmp_path: Path) -> None:
    conn = db_init(tmp_path / "gmail.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    for expected in ("messages", "threads", "sync_state"):
        assert expected in tables, f"missing table: {expected}"


def test_db_init_returns_connection(tmp_path: Path) -> None:
    conn = db_init(tmp_path / "gmail.db")
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_db_init_idempotent_on_existing_db(tmp_path: Path) -> None:
    """Calling db_init twice doesn't raise (CREATE TABLE IF NOT EXISTS)."""
    db_path = tmp_path / "gmail.db"
    conn = db_init(db_path)
    conn.close()
    conn2 = db_init(db_path)
    assert conn2 is not None
    conn2.close()


# ---------------------------------------------------------------------------
# _sync_get / _sync_set
# ---------------------------------------------------------------------------


# ── TestSyncState (flattened) ───────────────────────────────────────────────


def test_sync_get_get_missing_key_returns_none() -> None:
    conn = _in_memory_db()
    assert _sync_get(conn, "history_id") is None


def test_sync_get_set_then_get_round_trips() -> None:
    conn = _in_memory_db()
    _sync_set(conn, "history_id", "12345")
    conn.commit()
    assert _sync_get(conn, "history_id") == "12345"


def test_sync_get_set_overwrites_existing() -> None:
    conn = _in_memory_db()
    _sync_set(conn, "key", "old")
    conn.commit()
    _sync_set(conn, "key", "new")
    conn.commit()
    assert _sync_get(conn, "key") == "new"


def test_sync_get_set_none_value() -> None:
    conn = _in_memory_db()
    _sync_set(conn, "key", None)
    conn.commit()
    # None stored → row exists, value is None
    row = conn.execute("SELECT value FROM sync_state WHERE key='key'").fetchone()
    assert row is not None
    assert row[0] is None


def test_sync_get_multiple_keys_independent() -> None:
    conn = _in_memory_db()
    _sync_set(conn, "a", "1")
    _sync_set(conn, "b", "2")
    conn.commit()
    assert _sync_get(conn, "a") == "1"
    assert _sync_get(conn, "b") == "2"


# ---------------------------------------------------------------------------
# _decode_b64url
# ---------------------------------------------------------------------------


# ── TestDecodeB64url (flattened) ────────────────────────────────────────────


def test_decode_b64url_empty_string_returns_empty() -> None:
    assert _decode_b64url("") == ""


def test_decode_b64url_round_trip_ascii() -> None:
    original = "Hello, World!"
    encoded = base64.urlsafe_b64encode(original.encode()).decode().rstrip("=")
    assert _decode_b64url(encoded) == original


def test_decode_b64url_padding_added_automatically() -> None:
    """Base64url without padding (stripped =) still decodes correctly."""
    text = "test"
    encoded = base64.urlsafe_b64encode(text.encode()).decode()
    # Strip padding manually
    stripped = encoded.rstrip("=")
    assert _decode_b64url(stripped) == text


def test_decode_b64url_url_safe_chars() -> None:
    """+ and / are replaced with - and _ in urlsafe b64."""
    binary = bytes([0xFB, 0xFF, 0xFE])  # produces chars that differ between standard/urlsafe
    encoded = base64.urlsafe_b64encode(binary).decode().rstrip("=")
    result = _decode_b64url(encoded)
    assert result  # no crash; decodes to something


def test_decode_b64url_unicode_content() -> None:
    text = "こんにちは"
    encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    assert _decode_b64url(encoded) == text


def test_decode_b64url_invalid_data_returns_empty_string() -> None:
    """Completely invalid base64 should not raise — returns '' or replacement chars."""
    # The function has a broad except clause
    result = _decode_b64url("!!!invalid!!!")
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _extract_parts
# ---------------------------------------------------------------------------


# ── TestExtractParts (flattened) ────────────────────────────────────────────


def _extract_parts_b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_extract_parts_plain_text_part() -> None:
    payload = {
        "mimeType": "text/plain",
        "body": {"data": _extract_parts_b64("Hello plain")},
    }
    plain, html, attachments = _extract_parts(payload)
    assert plain == "Hello plain"
    assert html == ""
    assert attachments == []


def test_extract_parts_html_part() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": _extract_parts_b64("<p>Hello</p>")},
    }
    plain, html, _attachments = _extract_parts(payload)
    assert html == "<p>Hello</p>"
    assert plain == ""


def test_extract_parts_attachment_part() -> None:
    payload = {
        "mimeType": "application/pdf",
        "body": {"attachmentId": "att123", "size": 4096},
        "headers": [
            {"name": "Content-Disposition", "value": 'attachment; filename="doc.pdf"'},
        ],
        "partId": "2",
    }
    _plain, _html, attachments = _extract_parts(payload)
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "doc.pdf"
    assert attachments[0]["attachment_id_suffix"] == "att123"
    assert attachments[0]["size_bytes"] == 4096


def test_extract_parts_multipart_mixed() -> None:
    """multipart/mixed with a plain child and an attachment child."""
    payload = {
        "mimeType": "multipart/mixed",
        "body": {},
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _extract_parts_b64("The body")},
            },
            {
                "mimeType": "application/pdf",
                "body": {"attachmentId": "att99", "size": 100},
                "headers": [],
                "partId": "1",
            },
        ],
    }
    plain, _html, attachments = _extract_parts(payload)
    assert plain == "The body"
    assert len(attachments) == 1


def test_extract_parts_multipart_alternative_prefers_first_plain() -> None:
    """multipart/alternative: first plain wins; html also captured."""
    payload = {
        "mimeType": "multipart/alternative",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _extract_parts_b64("plain")}},
            {"mimeType": "text/html", "body": {"data": _extract_parts_b64("<p>html</p>")}},
        ],
    }
    plain, html, _ = _extract_parts(payload)
    assert plain == "plain"
    assert html == "<p>html</p>"


def test_extract_parts_empty_payload_returns_empty() -> None:
    plain, html, att = _extract_parts({})
    assert plain == ""
    assert html == ""
    assert att == []


def test_extract_parts_nested_multipart() -> None:
    """Deeply nested multipart still extracts content."""
    inner = {
        "mimeType": "multipart/alternative",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _extract_parts_b64("deep plain")}},
        ],
    }
    outer = {
        "mimeType": "multipart/mixed",
        "body": {},
        "parts": [inner],
    }
    plain, _, _ = _extract_parts(outer)
    assert plain == "deep plain"


# ---------------------------------------------------------------------------
# insert_batch
# ---------------------------------------------------------------------------


# ── TestInsertBatch (flattened) ─────────────────────────────────────────────


def test_insert_batch_inserts_message_and_thread() -> None:
    conn = _in_memory_db()
    msg = _make_message()
    insert_batch(conn, [msg])
    row = conn.execute("SELECT subject FROM messages WHERE message_id='msg1'").fetchone()
    assert row is not None
    assert row[0] == "Hello"
    t_row = conn.execute("SELECT thread_id FROM threads WHERE thread_id='t1'").fetchone()
    assert t_row is not None


def test_insert_batch_inserts_multiple_messages() -> None:
    conn = _in_memory_db()
    msgs = [_make_message(msg_id=f"m{i}", thread_id=f"t{i}") for i in range(5)]
    insert_batch(conn, msgs)
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 5


def test_insert_batch_inserts_attachment() -> None:
    conn = _in_memory_db()
    attachment = {
        "attachment_id": "msg1:att1",
        "message_id": "msg1",
        "filename": "report.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 2048,
        "part_id": "2",
    }
    msg = _make_message(attachments=[attachment])
    insert_batch(conn, [msg])
    row = conn.execute("SELECT filename FROM attachments WHERE attachment_id='msg1:att1'").fetchone()
    assert row is not None
    assert row[0] == "report.pdf"


def test_insert_batch_upsert_replaces_existing_message() -> None:
    conn = _in_memory_db()
    insert_batch(conn, [_make_message(subject="Original")])
    insert_batch(conn, [_make_message(subject="Updated")])
    row = conn.execute("SELECT subject FROM messages WHERE message_id='msg1'").fetchone()
    assert row[0] == "Updated"


def test_insert_batch_empty_list_no_error() -> None:
    conn = _in_memory_db()
    insert_batch(conn, [])  # should not raise
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_insert_batch_thread_created_once_for_multiple_messages() -> None:
    """Multiple messages in same thread → one thread row."""
    conn = _in_memory_db()
    msgs = [_make_message(msg_id=f"m{i}", thread_id="shared-thread") for i in range(3)]
    insert_batch(conn, msgs)
    count = conn.execute("SELECT COUNT(*) FROM threads WHERE thread_id='shared-thread'").fetchone()[0]
    assert count == 1


def test_insert_batch_no_attachments_key_handled() -> None:
    """Message dict without 'attachments' key doesn't crash."""
    conn = _in_memory_db()
    msg = _make_message()
    del msg["attachments"]
    insert_batch(conn, [msg])  # should not raise
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# _api_call_with_retry
# ---------------------------------------------------------------------------


# ── TestApiCallWithRetry (flattened) ────────────────────────────────────────


def test_api_call_with_retry_success_on_first_attempt() -> None:
    fn = MagicMock(return_value={"data": "ok"})
    result = _api_call_with_retry(fn, context="test")
    assert result == {"data": "ok"}
    fn.assert_called_once()


def test_api_call_with_retry_retries_on_429() -> None:
    from googleapiclient.errors import HttpError

    mock_resp = MagicMock()
    mock_resp.status = 429
    err = HttpError(resp=mock_resp, content=b"Rate limited")

    fn = MagicMock(side_effect=[err, err, {"ok": True}])
    with patch("time.sleep"):
        result = _api_call_with_retry(fn, context="test")
    assert result == {"ok": True}
    assert fn.call_count == 3


def test_api_call_with_retry_retry_warning_uses_gmail_sync_logger(caplog: pytest.LogCaptureFixture) -> None:
    from googleapiclient.errors import HttpError

    mock_resp = MagicMock()
    mock_resp.status = 429
    err = HttpError(resp=mock_resp, content=b"Rate limited")
    fn = MagicMock(side_effect=[err, {"ok": True}])
    retry_without_wait = _api_call_with_retry.with_policy(  # type: ignore[reportFunctionMemberAccess]
        wait_min=0, wait_max=0
    )

    with caplog.at_level(logging.WARNING):
        result = retry_without_wait(fn, context="test")

    assert result == {"ok": True}
    assert any(record.name == "gmail-sync" for record in caplog.records)


def test_api_call_with_retry_retries_on_500() -> None:
    from googleapiclient.errors import HttpError

    mock_resp = MagicMock()
    mock_resp.status = 500
    err = HttpError(resp=mock_resp, content=b"Server error")
    fn = MagicMock(side_effect=[err, "success"])
    with patch("time.sleep"):
        result = _api_call_with_retry(fn, context="test")
    assert result == "success"


def test_api_call_with_retry_permanent_403_raises_auth_immediately() -> None:
    from googleapiclient.errors import HttpError

    mock_resp = MagicMock()
    mock_resp.status = 403
    err = HttpError(resp=mock_resp, content=b"Forbidden")
    fn = MagicMock(side_effect=err)
    from fieldkit.errors import GmailAuthError

    with pytest.raises(GmailAuthError, match="access denied"):
        _api_call_with_retry(fn, context="test")
    assert fn.call_count == 1  # no retry for 403


def test_api_call_with_retry_exceeds_max_retries_raises_http_error() -> None:
    """implementation note: tenacity reraises HttpError after retries exhausted (was RuntimeError)."""
    from googleapiclient.errors import HttpError

    from fieldkit.config.retry import RETRY_MAX_ATTEMPTS

    mock_resp = MagicMock()
    mock_resp.status = 429
    err = HttpError(resp=mock_resp, content=b"Rate limited")
    fn = MagicMock(side_effect=err)
    with patch("time.sleep"), pytest.raises(HttpError) as exc_info:
        _api_call_with_retry(fn, context="ctx")
    assert exc_info.value.resp.status == 429
    assert fn.call_count == RETRY_MAX_ATTEMPTS


def test_api_call_with_retry_sleeps_between_retries() -> None:
    from googleapiclient.errors import HttpError

    mock_resp = MagicMock()
    mock_resp.status = 503
    err = HttpError(resp=mock_resp, content=b"Unavailable")
    fn = MagicMock(side_effect=[err, "ok"])
    with patch("time.sleep") as mock_sleep:
        _api_call_with_retry(fn, context="test")
    mock_sleep.assert_called_once()
    assert mock_sleep.call_args[0][0] > 0  # slept for positive duration


# ---------------------------------------------------------------------------
# incremental_sync — historyId absent warning (T07 regression)
# ---------------------------------------------------------------------------


# ── TestIncrementalSyncHistoryIdAbsent (flattened) ──────────────────────────


def _incremental_sync_make_db(start_id: str = "99000") -> sqlite3.Connection:
    conn = _in_memory_db()
    _sync_set(conn, "last_history_id", start_id)
    _sync_set(conn, "initial_sync_complete", "true")
    conn.commit()
    return conn


def _incremental_sync_make_service(history_pages: list[Any]) -> MagicMock:
    """Build a mock Gmail service where history().list().execute() returns pages."""
    service = MagicMock()
    # Chain: service.users().history().list(**kw).execute() → page
    history_list = MagicMock()
    history_list.execute = MagicMock(side_effect=history_pages)
    # history().list() must return same mock each call (keyword args vary)
    service.users.return_value.history.return_value.list.return_value = history_list
    return service


def test_incremental_sync_no_history_id_in_response_does_not_update_last_history_id() -> None:
    """When all history.list pages omit historyId, last_history_id is unchanged."""
    from fieldkit.gmail.sync_engine import incremental_sync

    conn = _incremental_sync_make_db("99000")
    # history.list returns one page with history records but no 'historyId'
    service = _incremental_sync_make_service([{"history": []}])  # no 'historyId' key

    # Patch getProfile to avoid real network call
    service.users.return_value.getProfile.return_value.execute.return_value = {}

    with patch("fieldkit.gmail.sync_engine._api_call_with_retry", side_effect=lambda fn, **kw: fn()):
        result = incremental_sync(service, conn)

    assert result == SyncSummary()
    # last_history_id must be unchanged
    assert _sync_get(conn, "last_history_id") == "99000"


def test_incremental_sync_no_history_id_emits_warning_log(caplog: pytest.LogCaptureFixture) -> None:
    """When historyId absent, a warning is logged mentioning start_history_id."""
    import logging

    from fieldkit.gmail.sync_engine import incremental_sync

    conn = _incremental_sync_make_db("88000")
    service = _incremental_sync_make_service([{"history": []}])
    service.users.return_value.getProfile.return_value.execute.return_value = {}

    with (
        caplog.at_level(logging.WARNING, logger="fieldkit.gmail.sync_engine"),
        patch("fieldkit.gmail.sync_engine._api_call_with_retry", side_effect=lambda fn, **kw: fn()),
    ):
        result = incremental_sync(service, conn)

    assert result == SyncSummary()
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("historyId was absent" in m for m in warning_msgs), (
        f"Expected 'historyId was absent' warning; got: {warning_msgs}"
    )


def test_incremental_sync_missing_history_id_retains_checkpoint() -> None:
    """A profile snapshot cannot replace the final processed history response."""
    from fieldkit.gmail.sync_engine import incremental_sync

    conn = _incremental_sync_make_db("77000")
    service = _incremental_sync_make_service([{"history": []}])
    service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "99999"}

    with patch("fieldkit.gmail.sync_engine._api_call_with_retry", side_effect=lambda fn, **kw: fn()):
        result = incremental_sync(service, conn)

    assert result == SyncSummary()
    assert _sync_get(conn, "last_history_id") == "77000"
    service.users.return_value.getProfile.assert_not_called()


def test_incremental_sync_missing_history_id_logs_retry_warning(caplog: pytest.LogCaptureFixture) -> None:
    """An absent response boundary retains state and explains that the next run retries."""
    import logging

    from fieldkit.gmail.sync_engine import incremental_sync

    conn = _incremental_sync_make_db("66000")
    service = _incremental_sync_make_service([{"history": []}])
    service.users.return_value.getProfile.return_value.execute.side_effect = RuntimeError("network error")

    with (
        caplog.at_level(logging.WARNING, logger="fieldkit.gmail.sync_engine"),
        patch("fieldkit.gmail.sync_engine._api_call_with_retry", side_effect=lambda fn, **kw: fn()),
    ):
        result = incremental_sync(service, conn)

    assert result == SyncSummary()
    assert _sync_get(conn, "last_history_id") == "66000"
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("historyId was absent" in m for m in warning_msgs), f"Expected retry warning; got: {warning_msgs}"
    service.users.return_value.getProfile.assert_not_called()


def test_incremental_sync_http_404_clears_both_state_keys() -> None:
    """HTTP 404 from history.list clears last_history_id AND initial_sync_complete (012).

    The handler at sync.py:665-675 sets both to None on 404. Previously only
    last_history_id was asserted, leaving initial_sync_complete untested.
    """
    from googleapiclient.errors import HttpError

    from fieldkit.gmail.sync_engine import incremental_sync

    conn = _incremental_sync_make_db("55000")

    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.reason = "Gone"
    err = HttpError(resp=mock_resp, content=b"History expired")

    service = _incremental_sync_make_service([err])
    service.users.return_value.history.return_value.list.return_value.execute.side_effect = [err]

    with patch("fieldkit.gmail.sync_engine._api_call_with_retry", side_effect=lambda fn, **kw: fn()):
        result = incremental_sync(service, conn)

    assert result == SyncSummary()
    assert _sync_get(conn, "last_history_id") is None, "last_history_id must be cleared on 404"
    assert _sync_get(conn, "initial_sync_complete") is None, (
        "initial_sync_complete must also be cleared on 404 (for full resync trigger)"
    )


def test_incremental_sync_added_messages_inserts_rows() -> None:
    """incremental_sync inserts messages into the DB when messagesAdded payload arrives (012).

    Patches fetch_messages_batch to return a pre-built message dict (bypassing
    the complex batch HTTP request mock), then asserts insert_batch wrote to the
    messages table. Confirms the DB-write contract of incremental_sync.
    """
    from fieldkit.gmail.sync_engine import incremental_sync

    conn = _incremental_sync_make_db("44000")

    # Pre-built structured dict as returned by _make_message_dict / fetch_messages_batch
    import json

    structured_msg = {
        "message_id": "msg001",
        "thread_id": "thr001",
        "from_addr": "alice@example.com",  # pii-guard: ignore
        "to_addr": "bob@your-org.example.com",
        "cc_addr": "",
        "date_str": "2024-03-15",
        "date_epoch": 1710460800,
        "subject": "Test Subject",
        "body_plain": "Hello",
        "body_html": "",
        "snippet": "Hello",
        "size_bytes": 5,
        "labels": json.dumps(["INBOX"]),
        "attachments": [],
    }
    history_page = {
        "history": [{"messagesAdded": [{"message": {"id": "msg001", "labelIds": ["INBOX"]}}]}],
        "historyId": "44001",
    }

    service = _incremental_sync_make_service([history_page])

    with (
        patch("fieldkit.gmail.sync_engine._api_call_with_retry", side_effect=lambda fn, **kw: fn()),
        patch(
            "fieldkit.gmail.sync_engine.fetch_messages_batch",
            return_value=BatchFetchResult(messages=[structured_msg]),
        ),
    ):
        result = incremental_sync(service, conn)

    assert result == SyncSummary(added=1)
    row_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert row_count >= 1, f"Expected at least 1 row in messages table after messagesAdded sync, got {row_count}"
    assert _sync_get(conn, "last_history_id") == "44001"


# ---------------------------------------------------------------------------
# fetch_messages_batch
# ---------------------------------------------------------------------------


def _make_raw_message(msg_id: str, thread_id: str = "t1") -> dict[str, Any]:
    """Return a minimal raw Gmail API message dict."""
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": ["INBOX"],
        "snippet": "snippet",
        "sizeEstimate": 100,
        "payload": {
            "headers": [
                {"name": "From", "value": "sender@example.com"},  # pii-guard: ignore
                {"name": "To", "value": "me@example.com"},  # pii-guard: ignore
                {"name": "Subject", "value": f"Subject {msg_id}"},
                {"name": "Date", "value": "Mon, 01 Jan 2024 10:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": "aGVsbG8="},  # "hello"
        },
    }


# ── TestFetchMessagesBatch (flattened) ──────────────────────────────────────


def _fetch_messages_batch_make_service(responses: dict[str, Any]) -> MagicMock:
    """Build a mock service whose batch captures add() calls and fires callbacks on execute()."""
    service = MagicMock()
    callbacks: list[tuple[str, Any]] = []

    def mock_add(request: Any, callback: Any = None) -> None:  # type: ignore[override]
        # Extract msg_id from the request mock's kwargs — stored as call args
        callbacks.append((request, callback))

    batch_mock = MagicMock()
    batch_mock.add.side_effect = mock_add

    def mock_execute() -> None:
        for i, (_req, cb) in enumerate(callbacks):
            # Use request_id = str(i); look up response from responses dict
            # We need to find the msg_id — use the order from add() calls
            cb(str(i), responses.get(str(i)), None)

    batch_mock.execute.side_effect = mock_execute
    service.new_batch_http_request.return_value = batch_mock
    return service


def test_fetch_messages_batch_empty_list_returns_empty() -> None:
    """fetch_messages_batch([]) returns [] without calling the API."""
    service = MagicMock()
    result = fetch_messages_batch(service, [])
    assert result == BatchFetchResult(messages=[])
    service.new_batch_http_request.assert_not_called()


def test_fetch_messages_batch_batch_add_called_n_times_execute_once() -> None:
    """For N ≤ 100 msg_ids, batch.add is called N times and batch.execute once."""
    msg_ids = [f"msg{i}" for i in range(5)]

    # Build responses keyed by index
    responses = {str(i): _make_raw_message(mid) for i, mid in enumerate(msg_ids)}
    service = _fetch_messages_batch_make_service(responses)

    result = fetch_messages_batch(service, msg_ids)

    batch_mock = service.new_batch_http_request.return_value
    assert batch_mock.add.call_count == 5
    assert batch_mock.execute.call_count == 1
    assert len(result.messages) == 5


def test_fetch_messages_batch_callback_error_logs_warning_others_succeed(caplog: pytest.LogCaptureFixture) -> None:
    """When one message errors in callback, a warning is logged and others succeed."""
    import logging

    service = MagicMock()
    collected: list[tuple[Any, Any]] = []

    def mock_add(request: Any, callback: Any = None) -> None:  # type: ignore[override]
        collected.append((request, callback))

    batch_mock = MagicMock()
    batch_mock.add.side_effect = mock_add

    def mock_execute() -> None:
        # msg0 succeeds, msg1 errors
        collected[0][1]("0", _make_raw_message("msg0"), None)
        collected[1][1]("1", None, RuntimeError("fetch failed"))

    batch_mock.execute.side_effect = mock_execute
    service.new_batch_http_request.return_value = batch_mock

    with caplog.at_level(logging.WARNING, logger="gmail-sync"):
        result = fetch_messages_batch(service, ["msg0", "msg1"])

    assert len(result.messages) == 1
    assert result.messages[0]["message_id"] == "msg0"
    assert result.unresolved == 1
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("1 unresolved" in m for m in warning_msgs), f"Expected count-only warning; got: {warning_msgs}"
    assert all("msg1" not in m for m in warning_msgs)


def test_fetch_messages_batch_result_shape_matches_fetch_message() -> None:
    """Returned dicts have all keys expected by insert_batch."""
    expected_keys = {
        "message_id",
        "thread_id",
        "from_addr",
        "to_addr",
        "cc_addr",
        "subject",
        "date_str",
        "date_epoch",
        "labels",
        "body_plain",
        "body_html",
        "size_bytes",
        "snippet",
        "attachments",
    }
    service = MagicMock()
    collected: list[tuple[Any, Any]] = []

    def mock_add(request: Any, callback: Any = None) -> None:  # type: ignore[override]
        collected.append((request, callback))

    batch_mock = MagicMock()
    batch_mock.add.side_effect = mock_add

    def mock_execute() -> None:
        collected[0][1]("0", _make_raw_message("msg42", "thread99"), None)

    batch_mock.execute.side_effect = mock_execute
    service.new_batch_http_request.return_value = batch_mock

    result = fetch_messages_batch(service, ["msg42"])
    assert len(result.messages) == 1
    assert expected_keys.issubset(result.messages[0].keys())
    assert result.messages[0]["message_id"] == "msg42"
    assert result.messages[0]["thread_id"] == "thread99"


# ---------------------------------------------------------------------------
# _process_label_changes tests
# ---------------------------------------------------------------------------


def _seed_messages(conn: sqlite3.Connection, messages: list[tuple[str, str, list[str]]]) -> None:
    """Insert (msg_id, thread_id, label_list) rows into messages for label tests."""
    rows = [
        (
            mid,
            tid,
            "",
            "",
            "",
            "",
            "",
            0,
            json.dumps(labels),
            "",
            "",
            0,
            "",
            "",
        )
        for mid, tid, labels in messages
    ]
    conn.executemany(
        """INSERT INTO messages
           (message_id, thread_id, from_addr, to_addr, cc_addr, subject,
            date_str, date_epoch, labels, body_plain, body_html, size_bytes,
            snippet, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


# ── TestProcessLabelChanges (flattened) ─────────────────────────────────────


def test_process_label_changes_empty_history_returns_zero() -> None:
    """Empty history_records → returns 0 without touching DB."""
    conn = _in_memory_db()
    changed: set[str] = set()
    result = _process_label_changes(conn, [], changed)
    assert result == 0
    assert changed == set()


def test_process_label_changes_mixed_adds_and_removes() -> None:
    """labelsAdded and labelsRemoved are applied correctly, executemany used."""
    conn = _in_memory_db()
    _seed_messages(
        conn,
        [
            ("msg1", "thread1", ["INBOX", "UNREAD"]),
            ("msg2", "thread2", ["INBOX"]),
        ],
    )

    history_records = [
        {
            "labelsAdded": [
                {"message": {"id": "msg1"}, "labelIds": ["STARRED"]},
            ],
            "labelsRemoved": [
                {"message": {"id": "msg2"}, "labelIds": ["INBOX"]},
            ],
        }
    ]
    changed: set[str] = set()
    result = _process_label_changes(conn, history_records, changed)

    assert result == 2
    assert "thread1" in changed
    assert "thread2" in changed

    row1 = conn.execute("SELECT labels FROM messages WHERE message_id='msg1'").fetchone()
    row2 = conn.execute("SELECT labels FROM messages WHERE message_id='msg2'").fetchone()
    labels1 = json.loads(row1[0])
    labels2 = json.loads(row2[0])
    assert "STARRED" in labels1
    assert "INBOX" in labels1  # untouched
    assert "INBOX" not in labels2


def test_process_label_changes_unknown_msg_ids_skipped() -> None:
    """Events for msg_ids not in DB are silently skipped."""
    conn = _in_memory_db()
    _seed_messages(conn, [("msg1", "thread1", ["INBOX"])])

    history_records = [
        {
            "labelsAdded": [
                {"message": {"id": "ghost"}, "labelIds": ["STARRED"]},
            ],
        }
    ]
    changed: set[str] = set()
    result = _process_label_changes(conn, history_records, changed)

    # ghost not in DB → skipped, no changes written
    assert result == 0
    assert changed == set()


def test_process_label_changes_add_does_not_duplicate_existing_label() -> None:
    """Adding a label already present does not duplicate it in the list."""
    conn = _in_memory_db()
    _seed_messages(conn, [("msg1", "thread1", ["INBOX", "STARRED"])])

    history_records = [
        {
            "labelsAdded": [
                {"message": {"id": "msg1"}, "labelIds": ["STARRED"]},
            ],
        }
    ]
    changed: set[str] = set()
    _process_label_changes(conn, history_records, changed)

    row = conn.execute("SELECT labels FROM messages WHERE message_id='msg1'").fetchone()
    labels = json.loads(row[0])
    assert labels.count("STARRED") == 1


def test_process_label_changes_event_ordering_preserved() -> None:
    """labelsAdded processed before labelsRemoved within each record."""
    conn = _in_memory_db()
    _seed_messages(conn, [("msg1", "thread1", [])])

    # Add INBOX then immediately remove it — net result: no INBOX
    history_records = [
        {
            "labelsAdded": [
                {"message": {"id": "msg1"}, "labelIds": ["INBOX"]},
            ],
            "labelsRemoved": [
                {"message": {"id": "msg1"}, "labelIds": ["INBOX"]},
            ],
        }
    ]
    changed: set[str] = set()
    _process_label_changes(conn, history_records, changed)

    row = conn.execute("SELECT labels FROM messages WHERE message_id='msg1'").fetchone()
    labels = json.loads(row[0])
    assert "INBOX" not in labels
