"""Message decoding, batch fetching, and SQLite upserts for Gmail sync."""

import base64
import contextlib
import html
import json
import logging
import sqlite3
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from typing import Any

from fieldkit.errors import GmailSyncPartialError
from fieldkit.gmail.batch import BatchAccumulator, BatchFetchResult, SyncSummary, warn_if_batch_incomplete
from fieldkit.gmail.retry import _api_call_with_retry

BATCH_SIZE = 100

log = logging.getLogger("gmail-sync")


def sync_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def sync_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, ?)", (key, value))


def raise_partial_sync(summary: SyncSummary) -> None:
    if summary.failed:
        raise GmailSyncPartialError(
            f"{summary.added} added, {summary.failed} failed "
            f"({summary.not_found} not found, {summary.unresolved} unresolved)"
        )


def _decode_b64url(data: str) -> str:
    """Decode base64url with proper padding."""
    if not data:
        return ""
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_attachment_filename(payload: dict[str, Any]) -> str:
    """Extract filename from Content-Disposition header of a MIME part."""
    for hdr in payload.get("headers", []):
        if hdr["name"].lower() != "content-disposition":
            continue
        for raw_token in hdr["value"].split(";"):
            token = raw_token.strip()
            if token.startswith("filename="):
                return str(token[9:].strip('"'))
    return ""


def _extract_parts(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """Recursively walk MIME tree. Return (plain, html, attachments)."""
    plain, html = "", ""
    attachments: list[dict[str, Any]] = []

    mime = payload.get("mimeType", "")
    body: dict[str, Any] = payload.get("body", {})
    parts = payload.get("parts", [])

    if mime == "text/plain" and not parts:
        plain = _decode_b64url(body.get("data", ""))
    elif mime == "text/html" and not parts:
        html = _decode_b64url(body.get("data", ""))
    elif body.get("attachmentId"):
        filename = _extract_attachment_filename(payload)
        attachments.append(
            {
                "attachment_id_suffix": body["attachmentId"],
                "filename": filename or payload.get("filename", ""),
                "mime_type": mime,
                "size_bytes": body.get("size", 0),
                "part_id": payload.get("partId", ""),
            }
        )

    for part in parts:
        p, h, a = _extract_parts(part)
        if p and not plain:
            plain = p
        if h and not html:
            html = h
        attachments.extend(a)

    return plain, html, attachments


def _make_message_dict(raw: dict[str, Any], msg_id: str) -> dict[str, Any]:
    """Convert a raw Gmail API message response into the structured dict used by insert_batch."""
    headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}

    date_str = headers.get("date", "")
    date_epoch: int | None = None
    with contextlib.suppress(ValueError, TypeError):
        dt = parsedate_to_datetime(date_str)
        date_epoch = int(dt.timestamp())

    payload = raw.get("payload", {})
    body_plain, body_html, attach_list = _extract_parts(payload)
    labels = raw.get("labelIds", [])

    attachments = []
    for a in attach_list:
        attachments.append(
            {
                "attachment_id": f"{msg_id}:{a['attachment_id_suffix']}",
                "message_id": msg_id,
                "filename": a["filename"],
                "mime_type": a["mime_type"],
                "size_bytes": a["size_bytes"],
                "part_id": a["part_id"],
            }
        )

    return {
        "message_id": msg_id,
        "thread_id": raw.get("threadId", ""),
        "from_addr": headers.get("from", ""),
        "to_addr": headers.get("to", ""),
        "cc_addr": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date_str": date_str,
        "date_epoch": date_epoch,
        "labels": json.dumps(labels),
        "body_plain": body_plain,
        "body_html": body_html,
        "size_bytes": raw.get("sizeEstimate", 0),
        "snippet": html.unescape(raw.get("snippet", "")),
        "attachments": attachments,
    }


def fetch_messages_batch(service: Any, msg_ids: list[str]) -> BatchFetchResult:
    """Fetch up to BATCH_SIZE messages in a single HTTP batch request.

    Returns a list of structured message dicts (same shape as fetch_message output).
    Per-message errors are logged as warnings; successful messages are collected and returned.
    """
    if not msg_ids:
        return BatchFetchResult(messages=[])

    accumulator = BatchAccumulator(_make_message_dict)
    batch = service.new_batch_http_request()
    for mid in msg_ids:
        batch.add(
            service.users().messages().get(userId="me", id=mid, format="full"),
            callback=partial(accumulator.record, mid),
        )

    _api_call_with_retry(batch.execute, context=f"batch.execute({len(msg_ids)} messages)")
    result = accumulator.result()
    warn_if_batch_incomplete(result, log)
    return result


# ---------------------------------------------------------------------------
# SQLite insertion
# ---------------------------------------------------------------------------


def insert_batch(conn: sqlite3.Connection, messages: list[dict[str, Any]]) -> None:
    """Upsert threads + messages + attachments in a single transaction."""
    # Collect thread stubs: use first message per thread for subject
    threads_seen: dict[str, dict[str, Any]] = {}
    for m in messages:
        tid = m["thread_id"]
        if tid not in threads_seen:
            threads_seen[tid] = {
                "thread_id": tid,
                "subject": m["subject"],
                "snippet": m["snippet"],
                "updated_at": datetime.now(UTC).isoformat(),
            }

    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO threads(thread_id, subject, snippet, updated_at)
            VALUES (:thread_id, :subject, :snippet, :updated_at)
            """,
            list(threads_seen.values()),
        )

        conn.executemany(
            """
            INSERT OR REPLACE INTO messages(
                message_id, thread_id, from_addr, to_addr, cc_addr,
                subject, date_str, date_epoch, labels,
                body_plain, body_html, size_bytes, snippet, synced_at
            ) VALUES (
                :message_id, :thread_id, :from_addr, :to_addr, :cc_addr,
                :subject, :date_str, :date_epoch, :labels,
                :body_plain, :body_html, :size_bytes, :snippet,
                datetime('now')
            )
            """,
            [{k: v for k, v in m.items() if k != "attachments"} for m in messages],
        )

        # Recompute message_count for affected threads from actual message rows.
        thread_ids = list(threads_seen)
        if thread_ids:
            conn.execute(
                f"""
                UPDATE threads
                SET message_count = (
                    SELECT COUNT(*) FROM messages WHERE messages.thread_id = threads.thread_id
                )
                WHERE thread_id IN ({",".join("?" * len(thread_ids))})
                """,
                thread_ids,
            )

        attachments = [a for m in messages for a in m.get("attachments", [])]
        if attachments:
            conn.executemany(
                """
                INSERT OR REPLACE INTO attachments(
                    attachment_id, message_id, filename, mime_type, size_bytes, part_id
                ) VALUES (
                    :attachment_id, :message_id, :filename, :mime_type, :size_bytes, :part_id
                )
                """,
                attachments,
            )


def _add_message_list_filters(kwargs: dict[str, Any], page_token: str | None, query: str | None) -> None:
    if page_token:
        kwargs["pageToken"] = page_token
    if query:
        kwargs["q"] = query


def list_messages(
    service: Any,
    page_token: str | None = None,
    *,
    query: str | None = None,
    max_results: int = 500,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ([(id, threadId), …], next_page_token)."""
    kwargs: dict[str, Any] = {
        "userId": "me",
        "maxResults": max_results,
        "fields": "messages(id,threadId),nextPageToken",
    }
    _add_message_list_filters(kwargs, page_token, query)

    result = _api_call_with_retry(
        lambda: service.users().messages().list(**kwargs).execute(),
        context="messages.list",
    )
    msgs = result.get("messages", [])
    next_token = result.get("nextPageToken")
    return msgs, next_token
