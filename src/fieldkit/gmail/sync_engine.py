#!/usr/bin/env python3
"""Gmail-to-SQLite synchronization orchestration."""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from googleapiclient.errors import HttpError

from fieldkit.errors import GmailAuthError, GmailSyncPartialError, GmailSyncRestartRequiredError, SQLiteSnapshotError
from fieldkit.gmail.batch import SyncSummary
from fieldkit.gmail.publication import (
    GMAIL_QUERY_READY_KEY,
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
    publication_root_for,
)
from fieldkit.gmail.retry import _api_call_with_retry
from fieldkit.gmail.sync_store import (
    BATCH_SIZE,
    fetch_messages_batch,
    insert_batch,
    list_messages,
)
from fieldkit.gmail.sync_store import (
    sync_get as _sync_get,
)
from fieldkit.gmail.sync_store import (
    sync_set as _sync_set,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection

EXCLUDED_LABELS = {
    "SPAM",
    "TRASH",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
}


log = logging.getLogger("gmail-sync")
_SQLConnection = sqlite3.Connection | SQLiteMutationConnection


def _process_deleted_messages(
    conn: _SQLConnection,
    history_records: list[dict[str, Any]],
    changed_thread_ids: set[str],
) -> int:
    """Delete removed messages from DB. Returns count deleted."""
    deleted = 0
    for record in history_records:
        for entry in record.get("messagesDeleted", []):
            msg_id = entry.get("message", {}).get("id")
            if not msg_id:
                continue
            row = conn.execute("SELECT thread_id FROM messages WHERE message_id=?", (msg_id,)).fetchone()
            if row:
                changed_thread_ids.add(row[0])
            conn.execute("DELETE FROM attachments WHERE message_id=?", (msg_id,))
            conn.execute("DELETE FROM messages WHERE message_id=?", (msg_id,))
            deleted += 1
    return deleted


def _collect_label_events(
    history_records: list[dict[str, Any]],
) -> list[tuple[str, str, list[str]]]:
    """Extract (msg_id, event_type, label_ids) tuples from history records in order."""
    events: list[tuple[str, str, list[str]]] = []
    for record in history_records:
        for event_type in ("labelsAdded", "labelsRemoved"):
            for entry in record.get(event_type, []):
                msg_id = entry.get("message", {}).get("id")
                label_ids = entry.get("labelIds", [])
                if msg_id and label_ids:
                    events.append((msg_id, event_type, label_ids))
    return events


def _fetch_label_state(
    conn: _SQLConnection,
    msg_ids: list[str],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Bulk-fetch label state and thread_id for the given message IDs."""
    placeholders = ",".join("?" * len(msg_ids))
    rows = conn.execute(
        f"SELECT message_id, labels, thread_id FROM messages WHERE message_id IN ({placeholders})",
        msg_ids,
    ).fetchall()
    label_state: dict[str, list[str]] = {}
    thread_map: dict[str, str] = {}
    for mid, labels_json, tid in rows:
        label_state[mid] = json.loads(labels_json or "[]")
        thread_map[mid] = tid
    return label_state, thread_map


def _apply_label_events(
    label_state: dict[str, list[str]],
    events: list[tuple[str, str, list[str]]],
) -> None:
    """Apply label add/remove events in order, mutating label_state in place."""
    for msg_id, event_type, label_ids in events:
        if msg_id not in label_state:
            continue
        current = label_state[msg_id]
        if event_type == "labelsAdded":
            for lbl in label_ids:
                if lbl not in current:
                    current.append(lbl)
        else:
            label_state[msg_id] = [lbl for lbl in current if lbl not in label_ids]


def _process_label_changes(
    conn: _SQLConnection,
    history_records: list[dict[str, Any]],
    changed_thread_ids: set[str],
) -> int:
    """Apply label add/remove events to the DB using bulk SELECT + executemany UPDATE.

    Collects all (msg_id, event_type, label_ids) tuples from history_records, fetches
    current label state for all affected messages in one SELECT IN query, applies adds
    then removes in record order, and flushes with a single executemany UPDATE.
    Returns the count of messages whose labels actually changed.
    """
    events = _collect_label_events(history_records)
    if not events:
        return 0

    affected_ids = list({e[0] for e in events})
    label_state, thread_map = _fetch_label_state(conn, affected_ids)
    _apply_label_events(label_state, events)

    updates: list[tuple[str, str]] = []
    for mid, tid in thread_map.items():
        updates.append((json.dumps(label_state[mid]), mid))
        changed_thread_ids.add(tid)

    if updates:
        conn.executemany("UPDATE messages SET labels=? WHERE message_id=?", updates)
        log.debug("Label sync: executemany updated %d messages", len(updates))

    return len(updates)


def _touch_changed_threads(conn: _SQLConnection, changed_thread_ids: set[str]) -> None:
    if not changed_thread_ids:
        return
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        "UPDATE threads SET updated_at=? WHERE thread_id=?",
        [(now, thread_id) for thread_id in changed_thread_ids],
    )


def _list_incremental_history(
    service: Any,
    kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        result = _api_call_with_retry(
            (lambda request_kwargs: lambda: service.users().history().list(**request_kwargs).execute())(kwargs),
            context="history.list",
        )
        if not isinstance(result, dict):
            raise RuntimeError("history.list returned a non-object response")
        return cast(dict[str, Any], result)
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
        log.warning("Gmail history checkpoint expired; a reconciled full refresh is required.")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ScanOutcome = Literal["exhausted", "paused"]
SyncCheckpointKey = Literal["last_page_token", "since_page_token", "last_history_id", "full_replay_page_token"]

_FULL_PAGE_TOKEN_KEY: Final = "last_page_token"
_FULL_COUNT_KEY = "messages_synced"
_FULL_SCAN_EXHAUSTED_KEY = "full_scan_exhausted"
_FORCED_MARKER_KEY = "full_sync_requested"
_FORCED_HISTORY_KEY = "full_sync_start_history_id"
_SINCE_EPOCH_KEY = "since_epoch"
_SINCE_PAGE_TOKEN_KEY: Final = "since_page_token"
_SINCE_COUNT_KEY = "since_messages_synced"
_SINCE_EXHAUSTED_TOKEN = "__fieldkit_exhausted__"


def _filter_excluded_labels(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only messages whose label set is not fully excluded."""
    return [
        msg
        for msg in messages
        if not ((labels := json.loads(msg["labels"])) and all(lbl in EXCLUDED_LABELS for lbl in labels))
    ]


def _capture_history_id(service: Any) -> str:
    profile = _api_call_with_retry(
        lambda: service.users().getProfile(userId="me").execute(),
        context="users.getProfile",
    )
    if not isinstance(profile, dict):
        raise GmailSyncPartialError("Gmail profile synchronization returned invalid data")
    history_id = profile.get("historyId")
    if not isinstance(history_id, str) or not history_id:
        raise GmailSyncPartialError("Gmail profile synchronization returned invalid data")
    return history_id


# ---------------------------------------------------------------------------
# Managed published sync
# ---------------------------------------------------------------------------

_INCREMENTAL_PAGE_TOKEN_KEY = "incremental_page_token"
_INCREMENTAL_LATEST_HISTORY_KEY = "incremental_latest_history_id"
_REPLAY_PAGE_TOKEN_KEY: Final = "full_replay_page_token"
_REPLAY_LATEST_HISTORY_KEY = "full_replay_latest_history_id"


@dataclass(frozen=True)
class PublishedSyncResult:
    """Selected sync mode, counts, and the persisted retry boundary."""

    mode: Literal["full", "incremental", "since"]
    summary: SyncSummary
    checkpoint_key: SyncCheckpointKey | None = None
    checkpoint: str | None = None


@dataclass(frozen=True)
class _FetchedMessagePage:
    messages: list[dict[str, Any]]
    summary: SyncSummary


def _read_published_state(db_path: Path, *keys: str) -> dict[str, str | None]:
    with open_gmail_publication(db_path) as connection:
        return {key: _sync_get(connection, key) for key in keys}


def get_published_sync_checkpoint(db_path: Path, key: str) -> str | None:
    """Read one checkpoint from the exact ready Gmail generation."""
    return _read_published_state(db_path, key)[key]


def _managed_cache_exists(db_path: Path) -> bool:
    publication_root = publication_root_for(db_path)
    if publication_root.exists() or publication_root.is_symlink():
        with open_gmail_publication(db_path):
            return True
    if db_path.exists() or db_path.is_symlink():
        raise SQLiteSnapshotError("Gmail cache requires explicit import", reason="unverified")
    return False


def _fetch_complete_message_page(service: Any, stubs: list[dict[str, Any]]) -> _FetchedMessagePage:
    messages: list[dict[str, Any]] = []
    not_found = 0
    for index in range(0, len(stubs), BATCH_SIZE):
        chunk = stubs[index : index + BATCH_SIZE]
        outcome = fetch_messages_batch(service, [str(stub["id"]) for stub in chunk])
        not_found += outcome.not_found
        if outcome.unresolved:
            return _FetchedMessagePage(
                messages=[],
                summary=SyncSummary(not_found=not_found, unresolved=outcome.unresolved),
            )
        messages.extend(_filter_excluded_labels(outcome.messages))
    return _FetchedMessagePage(
        messages=messages,
        summary=SyncSummary(added=len(messages), not_found=not_found),
    )


def _fetch_labels(service: Any) -> list[tuple[str, str]]:
    try:
        result = _api_call_with_retry(
            lambda: service.users().labels().list(userId="me").execute(),
            context="labels.list",
        )
    except GmailAuthError:
        raise
    except Exception as exc:
        log.warning("Gmail label synchronization failed; no label changes were published.")
        raise GmailSyncPartialError("Gmail label synchronization failed; no label changes were published") from exc
    if not isinstance(result, dict) or not isinstance(result.get("labels", []), list):
        raise GmailSyncPartialError("Gmail label synchronization returned invalid data")
    labels: list[tuple[str, str]] = []
    for raw in result.get("labels", []):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not isinstance(raw.get("name"), str):
            raise GmailSyncPartialError("Gmail label synchronization returned invalid data")
        labels.append((raw["id"], raw["name"]))
    return labels


def _publish_labels(db_path: Path, labels: list[tuple[str, str]]) -> None:
    def mutation(connection: SQLiteMutationConnection) -> None:
        connection.executemany(
            """
            INSERT OR REPLACE INTO labels(label_id, label_name, synced_at)
            VALUES (?, ?, datetime('now'))
            """,
            labels,
        )

    apply_gmail_page(db_path, mutation)


def _apply_history_page(
    connection: SQLiteMutationConnection,
    history_records: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> tuple[int, int]:
    changed_thread_ids = {str(message["thread_id"]) for message in messages}
    insert_batch(connection, messages)
    deleted = _process_deleted_messages(connection, history_records, changed_thread_ids)
    label_changes = _process_label_changes(connection, history_records, changed_thread_ids)
    _touch_changed_threads(connection, changed_thread_ids)
    return deleted, label_changes


def _history_page(
    service: Any,
    *,
    start_history_id: str,
    page_token: str | None,
) -> dict[str, Any] | None:
    kwargs: dict[str, Any] = {
        "userId": "me",
        "startHistoryId": start_history_id,
        "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
        "maxResults": 500,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    return _list_incremental_history(service, kwargs)


def _validated_history_records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise GmailSyncPartialError("Gmail history synchronization returned invalid data")
    records: list[dict[str, Any]] = []
    for raw_record in value:
        if not isinstance(raw_record, dict):
            raise GmailSyncPartialError("Gmail history synchronization returned invalid data")
        record = cast(dict[str, Any], raw_record)
        for event_type in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
            entries = record.get(event_type, [])
            if not isinstance(entries, list):
                raise GmailSyncPartialError("Gmail history synchronization returned invalid data")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("message"), dict):
                    raise GmailSyncPartialError("Gmail history synchronization returned invalid data")
                message = entry["message"]
                if not isinstance(message.get("id"), str):
                    raise GmailSyncPartialError("Gmail history synchronization returned invalid data")
                labels = message.get("labelIds", []) if event_type == "messagesAdded" else entry.get("labelIds", [])
                if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
                    raise GmailSyncPartialError("Gmail history synchronization returned invalid data")
        records.append(record)
    return records


def _sync_history_pages(
    service: Any,
    db_path: Path,
    *,
    start_history_id: str,
    page_token_key: str,
    latest_history_key: str,
    publish_global_checkpoint: bool,
) -> tuple[SyncSummary, str | None, bool]:
    state = _read_published_state(db_path, page_token_key, latest_history_key)
    page_token = state[page_token_key]
    latest_history_id = state[latest_history_key]
    summary = SyncSummary()
    while True:
        result = _history_page(service, start_history_id=start_history_id, page_token=page_token)
        if result is None:
            return summary, None, True
        history_records = _validated_history_records(result.get("history", []))
        message_ids: list[str] = []
        for record in history_records:
            for entry in record.get("messagesAdded", []):
                message = entry["message"]
                message_id = message.get("id")
                labels = message.get("labelIds", [])
                if labels and all(label in EXCLUDED_LABELS for label in labels):
                    continue
                message_ids.append(cast(str, message_id))
        fetched = _fetch_complete_message_page(service, [{"id": message_id} for message_id in message_ids])
        summary = summary.plus(fetched.summary)
        if fetched.summary.unresolved:
            return summary, None, False
        next_token = result.get("nextPageToken")
        current_history = result.get("historyId") or latest_history_id or start_history_id
        if next_token is not None and not isinstance(next_token, str):
            raise GmailSyncPartialError("Gmail history synchronization returned invalid data")
        if not isinstance(current_history, str):
            raise GmailSyncPartialError("Gmail history synchronization returned invalid data")

        def mutation(
            connection: SQLiteMutationConnection,
            records: list[dict[str, Any]] = history_records,
            page_messages: list[dict[str, Any]] = fetched.messages,
            saved_token: str | None = next_token,
            saved_history: str = current_history,
        ) -> None:
            _apply_history_page(connection, records, page_messages)
            _sync_set(connection, page_token_key, saved_token)
            _sync_set(connection, latest_history_key, saved_history)
            _sync_set(connection, GMAIL_QUERY_READY_KEY, "true")
            if saved_token is None and publish_global_checkpoint:
                _sync_set(connection, "last_history_id", saved_history)
                _sync_set(connection, page_token_key, None)
                _sync_set(connection, latest_history_key, None)

        apply_gmail_page(db_path, mutation)
        latest_history_id = current_history
        if next_token is None:
            return summary, current_history, False
        page_token = next_token


def _publish_scan_start(db_path: Path, history_id: str, *, forced: bool) -> None:
    def mutation(connection: SQLiteMutationConnection) -> None:
        _sync_set(connection, _FULL_PAGE_TOKEN_KEY, "")
        _sync_set(connection, _FULL_COUNT_KEY, "0")
        _sync_set(connection, _FULL_SCAN_EXHAUSTED_KEY, None)
        _sync_set(connection, "initial_sync_complete", None)
        _sync_set(connection, "last_history_id", None)
        _sync_set(connection, _FORCED_HISTORY_KEY, history_id)
        _sync_set(connection, _FORCED_MARKER_KEY, "true" if forced else "initial")
        _sync_set(connection, _REPLAY_PAGE_TOKEN_KEY, None)
        _sync_set(connection, _REPLAY_LATEST_HISTORY_KEY, None)
        _sync_set(connection, "sync_started_at", datetime.now(UTC).isoformat())

    apply_gmail_page(db_path, mutation)


def _scan_published_messages(
    service: Any,
    db_path: Path,
    *,
    query: str | None,
    page_token_key: str,
    count_key: str,
    max_messages: int,
    exhausted_key: str | None = None,
    exhausted_page_token: str | None = None,
) -> tuple[ScanOutcome, SyncSummary]:
    state = _read_published_state(db_path, page_token_key, count_key)
    page_token = state[page_token_key]
    total_synced = int(state[count_key] or "0")
    summary = SyncSummary()
    while True:
        remaining = max_messages - total_synced if max_messages else 500
        if max_messages and remaining <= 0:
            return "paused", summary
        stubs, next_token = list_messages(
            service,
            page_token,
            query=query,
            max_results=min(500, remaining),
        )
        if not stubs:

            def exhausted(connection: SQLiteMutationConnection) -> None:
                _sync_set(connection, GMAIL_QUERY_READY_KEY, "true")
                if exhausted_key:
                    _sync_set(connection, exhausted_key, "true")
                if exhausted_page_token:
                    _sync_set(connection, page_token_key, exhausted_page_token)

            if exhausted_key or exhausted_page_token:
                apply_gmail_page(db_path, exhausted)
            return "exhausted", summary
        fetched = _fetch_complete_message_page(service, stubs)
        summary = summary.plus(fetched.summary)
        if fetched.summary.unresolved:
            return "paused", summary
        updated_total = total_synced + len(stubs)

        def mutation(
            connection: SQLiteMutationConnection,
            page_messages: list[dict[str, Any]] = fetched.messages,
            saved_token: str | None = next_token,
            saved_total: int = updated_total,
        ) -> None:
            insert_batch(connection, page_messages)
            _sync_set(connection, GMAIL_QUERY_READY_KEY, "true")
            _sync_set(connection, page_token_key, saved_token or exhausted_page_token or "")
            _sync_set(connection, count_key, str(saved_total))
            if exhausted_key and saved_token is None:
                _sync_set(connection, exhausted_key, "true")

        apply_gmail_page(db_path, mutation)
        total_synced = updated_total
        if next_token is None:
            return "exhausted", summary
        if max_messages and total_synced >= max_messages:
            return "paused", summary
        page_token = next_token


def _finalize_published_full_sync(service: Any, db_path: Path) -> SyncSummary:
    state = _read_published_state(db_path, _FORCED_HISTORY_KEY, _FULL_COUNT_KEY)
    start_history_id = state[_FORCED_HISTORY_KEY]
    if not start_history_id:
        raise GmailSyncPartialError("Gmail full synchronization has no reconciliation checkpoint")
    summary, latest_history_id, expired = _sync_history_pages(
        service,
        db_path,
        start_history_id=start_history_id,
        page_token_key=_REPLAY_PAGE_TOKEN_KEY,
        latest_history_key=_REPLAY_LATEST_HISTORY_KEY,
        publish_global_checkpoint=False,
    )
    if expired:
        new_history_id = _capture_history_id(service)
        _publish_scan_start(db_path, new_history_id, forced=True)
        raise GmailSyncRestartRequiredError(
            "Gmail changed too far back to finish the refresh; retry to restart the full scan."
        )
    if latest_history_id is None:
        return summary

    def mutation(connection: SQLiteMutationConnection) -> None:
        _sync_set(connection, "last_history_id", latest_history_id)
        _sync_set(connection, "initial_sync_complete", "true")
        for key in (
            _FULL_PAGE_TOKEN_KEY,
            _FULL_COUNT_KEY,
            _FULL_SCAN_EXHAUSTED_KEY,
            _FORCED_MARKER_KEY,
            _FORCED_HISTORY_KEY,
            _REPLAY_PAGE_TOKEN_KEY,
            _REPLAY_LATEST_HISTORY_KEY,
        ):
            _sync_set(connection, key, None)

    apply_gmail_page(db_path, mutation)
    return summary


def _run_published_full_sync(service: Any, db_path: Path, max_messages: int, *, forced: bool) -> SyncSummary:
    state = _read_published_state(db_path, _FORCED_MARKER_KEY, _FULL_SCAN_EXHAUSTED_KEY)
    if forced and state[_FORCED_MARKER_KEY] not in {"true", "initial"}:
        _publish_scan_start(db_path, _capture_history_id(service), forced=True)
        state = {_FORCED_MARKER_KEY: "true", _FULL_SCAN_EXHAUSTED_KEY: None}
    elif not state[_FORCED_MARKER_KEY]:
        _publish_scan_start(db_path, _capture_history_id(service), forced=False)
        state = {_FORCED_MARKER_KEY: "initial", _FULL_SCAN_EXHAUSTED_KEY: None}
    summary = SyncSummary()
    if state[_FULL_SCAN_EXHAUSTED_KEY] != "true":
        outcome, summary = _scan_published_messages(
            service,
            db_path,
            query=None,
            page_token_key=_FULL_PAGE_TOKEN_KEY,
            count_key=_FULL_COUNT_KEY,
            max_messages=max_messages,
            exhausted_key=_FULL_SCAN_EXHAUSTED_KEY,
        )
        if outcome == "paused":
            return summary
    return summary.plus(_finalize_published_full_sync(service, db_path))


def _run_published_since_sync(
    service: Any,
    db_path: Path,
    since: datetime,
    max_messages: int,
) -> SyncSummary:
    since_epoch = int(since.replace(tzinfo=UTC).timestamp())
    state = _read_published_state(db_path, _SINCE_EPOCH_KEY, _SINCE_PAGE_TOKEN_KEY)
    page_token: str | None
    if state[_SINCE_EPOCH_KEY] != str(since_epoch):

        def start(connection: SQLiteMutationConnection) -> None:
            _sync_set(connection, _SINCE_EPOCH_KEY, str(since_epoch))
            _sync_set(connection, _SINCE_PAGE_TOKEN_KEY, "")
            _sync_set(connection, _SINCE_COUNT_KEY, "0")

        apply_gmail_page(db_path, start)
        page_token = ""
    else:
        page_token = state[_SINCE_PAGE_TOKEN_KEY]
    if page_token == _SINCE_EXHAUSTED_TOKEN:
        outcome, summary = "exhausted", SyncSummary()
    else:
        outcome, summary = _scan_published_messages(
            service,
            db_path,
            query=f"after:{since_epoch - 1}",
            page_token_key=_SINCE_PAGE_TOKEN_KEY,
            count_key=_SINCE_COUNT_KEY,
            max_messages=max_messages,
            exhausted_page_token=_SINCE_EXHAUSTED_TOKEN,
        )
    if outcome == "exhausted":

        def finish(connection: SQLiteMutationConnection) -> None:
            _sync_set(connection, _SINCE_EPOCH_KEY, None)
            _sync_set(connection, _SINCE_PAGE_TOKEN_KEY, None)
            _sync_set(connection, _SINCE_COUNT_KEY, None)

        apply_gmail_page(db_path, finish)
    return summary


def _run_published_incremental_sync(service: Any, db_path: Path, history_id: str) -> SyncSummary:
    summary, _latest_history_id, expired = _sync_history_pages(
        service,
        db_path,
        start_history_id=history_id,
        page_token_key=_INCREMENTAL_PAGE_TOKEN_KEY,
        latest_history_key=_INCREMENTAL_LATEST_HISTORY_KEY,
        publish_global_checkpoint=True,
    )
    if expired:

        def reset(connection: SQLiteMutationConnection) -> None:
            _sync_set(connection, "last_history_id", None)
            _sync_set(connection, "initial_sync_complete", None)
            _sync_set(connection, _INCREMENTAL_PAGE_TOKEN_KEY, None)
            _sync_set(connection, _INCREMENTAL_LATEST_HISTORY_KEY, None)

        apply_gmail_page(db_path, reset)
        raise GmailSyncRestartRequiredError("Gmail history expired; retry to start a reconciled full refresh")
    return summary


def run_published_sync(
    *,
    service_factory: Any,
    db_path: Path,
    full: bool,
    since: datetime | None,
    max_messages: int,
) -> PublishedSyncResult:
    """Fetch outside publication locks and publish each complete provider page."""
    service = service_factory()
    log.info("Authenticated.")
    managed_cache_exists = _managed_cache_exists(db_path)
    labels = _fetch_labels(service)
    initial_history_id = None
    if not managed_cache_exists and since is None:
        initial_history_id = _capture_history_id(service)
    if not managed_cache_exists:
        initialize_gmail_publication(db_path)
        if initial_history_id is not None:
            _publish_scan_start(db_path, initial_history_id, forced=full)
    _publish_labels(db_path, labels)
    if since is not None:
        mode: Literal["full", "incremental", "since"] = "since"
        summary = _run_published_since_sync(service, db_path, since, max_messages)
        checkpoint_key: SyncCheckpointKey = _SINCE_PAGE_TOKEN_KEY
    else:
        state = _read_published_state(db_path, "initial_sync_complete", "last_history_id")
        history_id = state["last_history_id"]
        if not full and state["initial_sync_complete"] == "true" and history_id:
            mode = "incremental"
            summary = _run_published_incremental_sync(service, db_path, history_id)
            checkpoint_key = "last_history_id"
        else:
            mode = "full"
            summary = _run_published_full_sync(service, db_path, max_messages, forced=full)
            checkpoint_key = _FULL_PAGE_TOKEN_KEY
            if summary.unresolved and get_published_sync_checkpoint(db_path, _FULL_SCAN_EXHAUSTED_KEY) == "true":
                checkpoint_key = _REPLAY_PAGE_TOKEN_KEY
    return PublishedSyncResult(
        mode=mode,
        summary=summary,
        checkpoint_key=checkpoint_key if summary.unresolved else None,
        checkpoint=get_published_sync_checkpoint(db_path, checkpoint_key) if summary.unresolved else None,
    )
