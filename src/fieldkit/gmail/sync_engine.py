#!/usr/bin/env python3
"""Gmail-to-SQLite synchronization orchestration."""

import json
import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from googleapiclient.errors import HttpError

from fieldkit.errors import GmailSyncRestartRequiredError
from fieldkit.gmail.batch import BatchFetchResult, SyncSummary
from fieldkit.gmail.retry import _api_call_with_retry, warn_recoverable_sync_error
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

EXCLUDED_LABELS = {
    "SPAM",
    "TRASH",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
}


log = logging.getLogger("gmail-sync")


def get_sync_checkpoint(conn: sqlite3.Connection, key: str) -> str | None:
    """Return a persisted synchronization checkpoint for CLI result rendering."""
    return _sync_get(conn, key)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def db_init(db_path: Path) -> sqlite3.Connection:
    """Initialize the Gmail cache database with schema and WAL mode."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Label sync
# ---------------------------------------------------------------------------


def sync_labels(service: Any, conn: sqlite3.Connection) -> None:
    """Fetch all Gmail labels and upsert into the labels table."""
    try:
        result = _api_call_with_retry(
            lambda: service.users().labels().list(userId="me").execute(),
            context="labels.list",
        )
    except Exception as exc:  # noqa: BLE001
        warn_recoverable_sync_error(exc, "labels.list() failed — skipping label sync: %s")
        return

    labels = result.get("labels", [])
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO labels(label_id, label_name, synced_at)
            VALUES (?, ?, datetime('now'))
            """,
            [(lbl["id"], lbl["name"]) for lbl in labels],
        )
    log.info("Synced %d labels", len(labels))


# ---------------------------------------------------------------------------
# Incremental sync
# ---------------------------------------------------------------------------


def _fetch_added_outcomes(service: Any, message_ids: list[str]) -> list[BatchFetchResult]:
    outcomes: list[BatchFetchResult] = []
    for i in range(0, len(message_ids), BATCH_SIZE):
        outcome = fetch_messages_batch(service, message_ids[i : i + BATCH_SIZE])
        outcomes.append(outcome)
        if outcome.unresolved:
            break
    return outcomes


def _process_added_messages(
    service: Any,
    conn: sqlite3.Connection,
    history_records: list[dict[str, Any]],
    changed_thread_ids: set[str],
) -> SyncSummary:
    """Fetch and insert newly added messages, stopping at the first unresolved chunk."""
    to_fetch: list[str] = []
    for record in history_records:
        for entry in record.get("messagesAdded", []):
            msg_stub = entry.get("message", {})
            label_ids = msg_stub.get("labelIds", [])
            if label_ids and all(lbl in EXCLUDED_LABELS for lbl in label_ids):
                continue
            to_fetch.append(msg_stub["id"])

    if not to_fetch:
        return SyncSummary()

    # Chunk into BATCH_SIZE groups — Gmail API limits batch requests to 100 items.
    summary = SyncSummary()
    for outcome in _fetch_added_outcomes(service, to_fetch):
        batch: list[dict[str, Any]] = []
        for msg in outcome.messages:
            labels = json.loads(msg["labels"])
            if labels and all(lbl in EXCLUDED_LABELS for lbl in labels):
                continue
            batch.append(msg)
            changed_thread_ids.add(msg["thread_id"])

        if batch:
            insert_batch(conn, batch)
            log.info("Added %d messages this page", len(batch))
        summary = summary.plus(
            SyncSummary(added=len(batch), not_found=outcome.not_found, unresolved=outcome.unresolved)
        )
    return summary


def _process_deleted_messages(
    conn: sqlite3.Connection,
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
    conn: sqlite3.Connection,
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
    conn: sqlite3.Connection,
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


def _persist_incremental_checkpoint(
    conn: sqlite3.Connection,
    *,
    latest_history_id: str | None,
    start_history_id: str,
    summary: SyncSummary,
) -> None:
    if summary.unresolved:
        return
    if latest_history_id:
        _sync_set(conn, "last_history_id", latest_history_id)
        conn.commit()
        log.info("Updated last_history_id to %s", latest_history_id)
        return
    log.warning(
        "historyId was absent from all history.list responses "
        "(start_history_id=%s) — last_history_id not updated. "
        "Next run will retry; may trigger full resync via HTTP 404.",
        start_history_id,
    )


def _require_incremental_checkpoint(conn: sqlite3.Connection) -> str:
    history_id = _sync_get(conn, "last_history_id")
    if not history_id:
        raise RuntimeError("incremental_sync called without last_history_id")
    return history_id


def _reset_incremental_checkpoint(conn: sqlite3.Connection) -> None:
    _sync_set(conn, "last_history_id", None)
    _sync_set(conn, "initial_sync_complete", None)
    conn.commit()


def _process_incremental_page(
    service: Any,
    conn: sqlite3.Connection,
    history_records: list[dict[str, Any]],
    changed_thread_ids: set[str],
) -> tuple[SyncSummary, int, int]:
    summary = _process_added_messages(service, conn, history_records, changed_thread_ids)
    deleted = _process_deleted_messages(conn, history_records, changed_thread_ids)
    label_changes = _process_label_changes(conn, history_records, changed_thread_ids)
    conn.commit()
    return summary, deleted, label_changes


def _touch_changed_threads(conn: sqlite3.Connection, changed_thread_ids: set[str]) -> None:
    if not changed_thread_ids:
        return
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        "UPDATE threads SET updated_at=? WHERE thread_id=?",
        [(now, thread_id) for thread_id in changed_thread_ids],
    )
    conn.commit()


def _list_incremental_history(
    service: Any,
    kwargs: dict[str, Any],
    *,
    start_history_id: str,
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
        log.warning(
            "historyId %s expired (HTTP 404) — clearing state for full resync",
            start_history_id,
            exc_info=True,
        )
        return None


def incremental_sync(service: Any, conn: sqlite3.Connection) -> SyncSummary:
    """Sync only new/changed/deleted messages since last_history_id."""
    start_history_id = _require_incremental_checkpoint(conn)

    log.info("Incremental sync starting from historyId %s", start_history_id)

    summary = SyncSummary()
    deleted = 0
    label_changes = 0
    page_token: str | None = None
    latest_history_id: str | None = None
    changed_thread_ids: set[str] = set()

    while True:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            "maxResults": 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        result = _list_incremental_history(service, kwargs, start_history_id=start_history_id)
        if result is None:
            _reset_incremental_checkpoint(conn)
            return summary

        latest_history_id = result.get("historyId") or latest_history_id

        history_records = result.get("history", [])
        log.info(
            "Processing %d history records (page_token=%s)",
            len(history_records),
            page_token or "start",
        )

        added_result, page_deleted, page_label_changes = _process_incremental_page(
            service,
            conn,
            history_records,
            changed_thread_ids,
        )
        summary = summary.plus(added_result)
        deleted += page_deleted
        label_changes += page_label_changes

        if added_result.unresolved:
            break

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    _touch_changed_threads(conn, changed_thread_ids)

    _persist_incremental_checkpoint(
        conn,
        latest_history_id=latest_history_id,
        start_history_id=start_history_id,
        summary=summary,
    )
    log.info(
        "Incremental sync complete: %d added, %d deleted, %d label updates",
        summary.added,
        deleted,
        label_changes,
    )
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ScanOutcome = Literal["exhausted", "paused"]

_FULL_PAGE_TOKEN_KEY = "last_page_token"
_FULL_COUNT_KEY = "messages_synced"
_FULL_SCAN_EXHAUSTED_KEY = "full_scan_exhausted"
_FORCED_MARKER_KEY = "full_sync_requested"
_FORCED_HISTORY_KEY = "full_sync_start_history_id"
_SINCE_EPOCH_KEY = "since_epoch"
_SINCE_PAGE_TOKEN_KEY = "since_page_token"
_SINCE_COUNT_KEY = "since_messages_synced"
_SINCE_EXHAUSTED_TOKEN = "__fieldkit_exhausted__"


def _filter_excluded_labels(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only messages whose label set is not fully excluded."""
    return [
        msg
        for msg in messages
        if not ((labels := json.loads(msg["labels"])) and all(lbl in EXCLUDED_LABELS for lbl in labels))
    ]


def _process_page_chunk(
    service: Any,
    conn: sqlite3.Connection,
    stubs: list[dict[str, Any]],
    *,
    total_synced: int,
    start_time: float,
    page_num: int,
) -> tuple[int, SyncSummary]:
    """Fetch and insert one BATCH_SIZE chunk from *stubs*.

    Returns the updated all-run count and this page's outcome summary.
    """
    summary = SyncSummary()
    for i in range(0, len(stubs), BATCH_SIZE):
        chunk = stubs[i : i + BATCH_SIZE]
        outcome = fetch_messages_batch(service, [s["id"] for s in chunk])
        to_insert = _filter_excluded_labels(outcome.messages)

        if to_insert:
            insert_batch(conn, to_insert)
        total_synced += len(chunk)
        elapsed = time.monotonic() - start_time
        rate = total_synced / elapsed if elapsed > 0 else 0
        log.info("Processed %d messages total (%.1f msg/s) — page %d", total_synced, rate, page_num)

        summary = summary.plus(
            SyncSummary(added=len(to_insert), not_found=outcome.not_found, unresolved=outcome.unresolved)
        )
        if outcome.unresolved:
            break

    return total_synced, summary


def _capture_history_id(service: Any) -> str:
    profile = _api_call_with_retry(
        lambda: service.users().getProfile(userId="me").execute(),
        context="users.getProfile",
    )
    history_id = profile.get("historyId")
    if not history_id:
        raise RuntimeError("users.getProfile returned no historyId")
    return str(history_id)


def _prepare_forced_full_sync(service: Any, conn: sqlite3.Connection) -> None:
    """Start a forced operation once; subsequent flagged calls resume it."""
    if _sync_get(conn, _FORCED_MARKER_KEY) == "true":
        return
    history_id = _capture_history_id(service)
    with conn:
        _sync_set(conn, _FULL_PAGE_TOKEN_KEY, "")
        _sync_set(conn, _FULL_COUNT_KEY, "0")
        _sync_set(conn, _FULL_SCAN_EXHAUSTED_KEY, None)
        _sync_set(conn, "initial_sync_complete", None)
        _sync_set(conn, "last_history_id", None)
        _sync_set(conn, _FORCED_HISTORY_KEY, history_id)
        _sync_set(conn, _FORCED_MARKER_KEY, "true")
        _sync_set(conn, "sync_started_at", datetime.now(UTC).isoformat())


def _restart_expired_forced_sync(service: Any, conn: sqlite3.Connection) -> None:
    history_id = _capture_history_id(service)
    with conn:
        _sync_set(conn, _FULL_PAGE_TOKEN_KEY, "")
        _sync_set(conn, _FULL_COUNT_KEY, "0")
        _sync_set(conn, _FULL_SCAN_EXHAUSTED_KEY, None)
        _sync_set(conn, "initial_sync_complete", None)
        _sync_set(conn, "last_history_id", None)
        _sync_set(conn, _FORCED_HISTORY_KEY, history_id)
        _sync_set(conn, _FORCED_MARKER_KEY, "true")
    raise GmailSyncRestartRequiredError(
        "Gmail changed too far back to finish the refresh; retry to restart the full scan."
    )


def _replay_forced_history(
    service: Any,
    conn: sqlite3.Connection,
    start_history_id: str,
) -> tuple[SyncSummary, str | None, bool]:
    """Replay scan-window history, returning summary, latest ID, and expiry."""
    summary = SyncSummary()
    latest_history_id: str | None = None
    page_token: str | None = None
    changed_thread_ids: set[str] = set()
    while True:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            "maxResults": 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = _list_incremental_history(service, kwargs, start_history_id=start_history_id)
        if result is None:
            return summary, None, True
        latest_history_id = result.get("historyId") or latest_history_id
        page_summary, _, _ = _process_incremental_page(service, conn, result.get("history", []), changed_thread_ids)
        summary = summary.plus(page_summary)
        if page_summary.unresolved:
            _touch_changed_threads(conn, changed_thread_ids)
            return summary, None, False
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    _touch_changed_threads(conn, changed_thread_ids)
    return summary, latest_history_id or start_history_id, False


def _reconcile_and_publish_full_sync(
    service: Any,
    conn: sqlite3.Connection,
    total_synced: int,
) -> SyncSummary:
    replay_summary = SyncSummary()
    captured_history_id = _sync_get(conn, _FORCED_HISTORY_KEY)
    if captured_history_id:
        replay_summary, latest_history_id, expired = _replay_forced_history(service, conn, captured_history_id)
        if expired:
            _restart_expired_forced_sync(service, conn)
        if latest_history_id is None:
            return replay_summary
    else:
        latest_history_id = _capture_history_id(service)

    with conn:
        _sync_set(conn, "last_history_id", latest_history_id)
        _sync_set(conn, "initial_sync_complete", "true")
        _sync_set(conn, _FULL_PAGE_TOKEN_KEY, None)
        _sync_set(conn, _FULL_COUNT_KEY, None)
        _sync_set(conn, _FULL_SCAN_EXHAUSTED_KEY, None)
        _sync_set(conn, _FORCED_MARKER_KEY, None)
        _sync_set(conn, _FORCED_HISTORY_KEY, None)
    log.info("Sync complete. Total messages processed: %d", total_synced)
    return replay_summary


def _finalize_full_sync(service: Any, conn: sqlite3.Connection, total_synced: int) -> SyncSummary:
    """Reconcile the scan window, then publish completion atomically."""
    return _reconcile_and_publish_full_sync(service, conn, total_synced)


def _process_full_page(
    service: Any,
    conn: sqlite3.Connection,
    stubs: list[dict[str, Any]],
    *,
    next_token: str | None,
    page_token_key: str,
    count_key: str,
    exhausted_key: str | None,
    exhausted_page_token: str | None,
    total_synced: int,
    start_time: float,
    page_num: int,
) -> tuple[int, SyncSummary]:
    updated_total, summary = _process_page_chunk(
        service,
        conn,
        stubs,
        total_synced=total_synced,
        start_time=start_time,
        page_num=page_num,
    )
    if not summary.unresolved:
        with conn:
            saved_token = next_token or exhausted_page_token or ""
            _sync_set(conn, page_token_key, saved_token)
            _sync_set(conn, count_key, str(updated_total))
            if exhausted_key and not next_token:
                _sync_set(conn, exhausted_key, "true")
    return updated_total, summary


def _start_message_scan(
    conn: sqlite3.Connection,
    *,
    resume_token: str | None,
    total_synced: int,
    started_at_key: str | None,
) -> None:
    if resume_token:
        log.info("Resuming from checkpoint (messages synced so far: %d)", total_synced)
        return
    log.info("Starting full sync…")
    if started_at_key:
        _sync_set(conn, started_at_key, datetime.now(UTC).isoformat())
        conn.commit()


def _checkpoint_scan_exhaustion(
    conn: sqlite3.Connection,
    *,
    page_token_key: str,
    exhausted_key: str | None,
    exhausted_page_token: str | None,
) -> None:
    if not exhausted_key and not exhausted_page_token:
        return
    with conn:
        if exhausted_key:
            _sync_set(conn, exhausted_key, "true")
        if exhausted_page_token:
            _sync_set(conn, page_token_key, exhausted_page_token)


def _scan_messages(
    service: Any,
    conn: sqlite3.Connection,
    *,
    query: str | None,
    page_token_key: str,
    count_key: str,
    max_messages: int,
    exhausted_key: str | None = None,
    exhausted_page_token: str | None = None,
    started_at_key: str | None = "sync_started_at",
) -> tuple[ScanOutcome, SyncSummary]:
    """Scan complete API pages and checkpoint only after each page is processed."""
    resume_token = _sync_get(conn, page_token_key)
    total_synced = int(_sync_get(conn, count_key) or "0")

    _start_message_scan(
        conn,
        resume_token=resume_token,
        total_synced=total_synced,
        started_at_key=started_at_key,
    )

    page_token: str | None = resume_token
    page_num = 0
    start_time = time.monotonic()
    summary = SyncSummary()

    while True:
        page_num += 1
        log.info("Fetching page %d (token: %s)…", page_num, page_token or "start")

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
            log.info("No messages on page %d — done.", page_num)
            _checkpoint_scan_exhaustion(
                conn,
                page_token_key=page_token_key,
                exhausted_key=exhausted_key,
                exhausted_page_token=exhausted_page_token,
            )
            return "exhausted", summary

        total_synced, page_summary = _process_full_page(
            service,
            conn,
            stubs,
            next_token=next_token,
            page_token_key=page_token_key,
            count_key=count_key,
            exhausted_key=exhausted_key,
            exhausted_page_token=exhausted_page_token,
            total_synced=total_synced,
            start_time=start_time,
            page_num=page_num,
        )
        summary = summary.plus(page_summary)
        if page_summary.unresolved:
            return "paused", summary

        if not next_token:
            return "exhausted", summary
        if max_messages and total_synced >= max_messages:
            log.info("Reached --max-messages %d at a page boundary, stopping.", max_messages)
            return "paused", summary

        page_token = next_token


def _full_sync(service: Any, conn: sqlite3.Connection, max_messages: int) -> SyncSummary:
    """Execute the full (initial or resumed) sync loop."""
    summary = SyncSummary()
    scan_exhausted = _sync_get(conn, _FULL_SCAN_EXHAUSTED_KEY) == "true"
    if not scan_exhausted:
        outcome, summary = _scan_messages(
            service,
            conn,
            query=None,
            page_token_key=_FULL_PAGE_TOKEN_KEY,
            count_key=_FULL_COUNT_KEY,
            max_messages=max_messages,
            exhausted_key=_FULL_SCAN_EXHAUSTED_KEY,
        )
        if outcome == "paused":
            return summary
    total_synced = int(_sync_get(conn, _FULL_COUNT_KEY) or "0")
    summary = summary.plus(_finalize_full_sync(service, conn, total_synced))
    return summary


def _since_sync(
    service: Any,
    conn: sqlite3.Connection,
    since: datetime,
    max_messages: int,
) -> SyncSummary:
    """Run a resumable date-bounded upsert without changing global sync continuity."""
    since_epoch = int(since.replace(tzinfo=UTC).timestamp())
    stored_epoch = _sync_get(conn, _SINCE_EPOCH_KEY)
    if stored_epoch != str(since_epoch):
        with conn:
            _sync_set(conn, _SINCE_EPOCH_KEY, str(since_epoch))
            _sync_set(conn, _SINCE_PAGE_TOKEN_KEY, "")
            _sync_set(conn, _SINCE_COUNT_KEY, "0")

    if _sync_get(conn, _SINCE_PAGE_TOKEN_KEY) == _SINCE_EXHAUSTED_TOKEN:
        outcome, summary = "exhausted", SyncSummary()
    else:
        outcome, summary = _scan_messages(
            service,
            conn,
            query=f"after:{since_epoch - 1}",
            page_token_key=_SINCE_PAGE_TOKEN_KEY,
            count_key=_SINCE_COUNT_KEY,
            max_messages=max_messages,
            exhausted_page_token=_SINCE_EXHAUSTED_TOKEN,
            started_at_key=None,
        )
    if outcome == "exhausted":
        with conn:
            _sync_set(conn, _SINCE_EPOCH_KEY, None)
            _sync_set(conn, _SINCE_PAGE_TOKEN_KEY, None)
            _sync_set(conn, _SINCE_COUNT_KEY, None)
    return summary


def _run_sync(
    *,
    service_factory: Any,
    conn: sqlite3.Connection,
    full: bool,
    since: datetime | None,
    max_messages: int,
) -> SyncSummary:
    service = service_factory()
    log.info("Authenticated.")
    if since is not None:
        return _since_sync(service, conn, since, max_messages)
    if full:
        _prepare_forced_full_sync(service, conn)
    sync_labels(service, conn)
    if _sync_get(conn, "initial_sync_complete") == "true" and _sync_get(conn, "last_history_id"):
        return incremental_sync(service, conn)
    return _full_sync(service, conn, max_messages)
