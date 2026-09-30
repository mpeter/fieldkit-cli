"""Sanitized candidate-cache observations, not independent controller approval."""

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, open_gmail_publication
from fieldkit.gmail.query_support import sqlite_query_budget
from fieldkit.sqlite_publication import SQLitePublicationError

MAX_OBSERVED_COUNT = (1 << 63) - 1
MAX_CHECKPOINT_BYTES = 4096
_COUNT_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z", re.ASCII)
_STATE_KEYS = (
    "since_epoch",
    "since_page_token",
    "since_messages_synced",
    "last_history_id",
    GMAIL_QUERY_READY_KEY,
)
_STATE_SQL = (
    "SELECT key, typeof(value) AS value_type, "
    "coalesce(length(CAST(value AS BLOB)), 0) AS byte_length, "
    "CASE WHEN key IN ('since_epoch', 'since_messages_synced') "
    "AND typeof(value) = 'text' AND length(CAST(value AS BLOB)) BETWEEN 1 AND 19 "
    "THEN CASE WHEN length(value) = length(CAST(value AS BLOB)) "
    "AND value NOT GLOB '*[^0-9]*' THEN substr(value, 1, 19) END END AS numeric_text, "
    "CASE WHEN key = ? AND value = 'true' THEN 1 ELSE 0 END AS query_ready "
    "FROM sync_state WHERE key IN (?, ?, ?, ?, ?)"
)


@dataclass(frozen=True)
class _CheckpointMetadata:
    value_type: str = "null"
    byte_length: int = 0
    numeric_text: str | None = None
    query_ready: bool = False

    @property
    def present(self) -> bool:
        return self.value_type != "null"


class GmailCacheObservationError(ValueError):
    """A cache cannot produce a validated sanitized observation."""


@dataclass(frozen=True)
class GmailCacheObservation:
    """Aggregate state only; no mailbox content or checkpoint values."""

    message_count: int
    since_checkpoint_present: bool
    since_count: int | None
    history_checkpoint_present: bool
    query_ready: bool


def _count(value: object) -> int:
    if not isinstance(value, str) or _COUNT_PATTERN.fullmatch(value) is None:
        raise GmailCacheObservationError("Gmail cache has an invalid aggregate count")
    parsed = int(value)
    if parsed > MAX_OBSERVED_COUNT:
        raise GmailCacheObservationError("Gmail cache aggregate count exceeds the observation bound")
    return parsed


def observe_gmail_cache(db_path: Path) -> GmailCacheObservation:
    """Read one verified committed generation and return sanitized aggregates.

    Completed since scans clear their checkpoint and count. A paused scan's
    counter measures scanned messages, including filtered or duplicate rows,
    so it need not equal or be bounded by the cached message count. The opener
    is candidate code; this API does not authenticate a rehearsal controller.
    """
    try:
        with closing(open_gmail_publication(db_path)) as connection, sqlite_query_budget(connection, row_budget=100):
            count_row = connection.execute("SELECT COUNT(*) FROM messages").fetchone()
            state = {
                row["key"]: _CheckpointMetadata(
                    row["value_type"], row["byte_length"], row["numeric_text"], row["query_ready"] == 1
                )
                for row in connection.execute(_STATE_SQL, (GMAIL_QUERY_READY_KEY, *_STATE_KEYS)).fetchall()
            }
    except (SQLitePublicationError, sqlite3.Error, OSError):
        raise GmailCacheObservationError("Gmail cache committed snapshot is unavailable or invalid") from None

    if count_row is None or type(count_row[0]) is not int or not 0 <= count_row[0] <= MAX_OBSERVED_COUNT:
        raise GmailCacheObservationError("Gmail cache has an invalid message count")
    if not state.get(GMAIL_QUERY_READY_KEY, _CheckpointMetadata()).query_ready:
        raise GmailCacheObservationError("Gmail cache is not query ready")
    epoch = state.get("since_epoch", _CheckpointMetadata())
    token = state.get("since_page_token", _CheckpointMetadata())
    count = state.get("since_messages_synced", _CheckpointMetadata())
    since_count = None
    if not epoch.present:
        if token.present or count.present:
            raise GmailCacheObservationError("Gmail cache has an inconsistent since checkpoint")
    else:
        _count(epoch.numeric_text)
        if token.value_type != "text" or token.byte_length > MAX_CHECKPOINT_BYTES:
            raise GmailCacheObservationError("Gmail cache has an invalid since checkpoint")
        since_count = _count(count.numeric_text)
    history = state.get("last_history_id", _CheckpointMetadata())
    if history.present and (history.value_type != "text" or not 1 <= history.byte_length <= MAX_CHECKPOINT_BYTES):
        raise GmailCacheObservationError("Gmail cache has an invalid history checkpoint")
    return GmailCacheObservation(
        message_count=count_row[0],
        since_checkpoint_present=epoch.present,
        since_count=since_count,
        history_checkpoint_present=history.present,
        query_ready=True,
    )
