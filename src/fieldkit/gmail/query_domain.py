"""Gmail query domain logic — connect, champion signals, and supporting helpers.

Extracted from commands/gmail/query.py (historic regression) so that collect.py and other
callers can import from the domain layer instead of from commands/.

historic regression: connect() raises GmailDbNotFoundError instead of calling sys.exit(1).
"""

import calendar
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fieldkit.gmail.exceptions import GmailDbNotFoundError
from fieldkit.gmail.names import resolve_name

_BATCH_SIZE = 50

_ENSURE_INDEX_STMTS = [
    "CREATE INDEX IF NOT EXISTS idx_messages_from_addr ON messages(from_addr)",
    "CREATE INDEX IF NOT EXISTS idx_messages_to_addr   ON messages(to_addr)",
    "CREATE INDEX IF NOT EXISTS idx_messages_cc_addr   ON messages(cc_addr)",
    "CREATE INDEX IF NOT EXISTS idx_people_display_name ON people(display_name)",
]


def _chunk_list(lst: list[str], size: int) -> list[list[str]]:
    """Split a list into chunks of at most *size* items.

    An empty list produces one empty chunk so callers get a zero result
    without special-casing the empty-input path.
    """
    return [lst[i : i + size] for i in range(0, max(len(lst), 1), size)]


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """Apply any indexes that may be missing from existing databases."""
    for stmt in _ENSURE_INDEX_STMTS:
        conn.execute(stmt)
    conn.commit()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations for columns added after initial release.

    Each migration uses ALTER TABLE … ADD COLUMN and silently ignores the
    "duplicate column name" error that SQLite raises when the column already
    exists.  Any other OperationalError is re-raised so genuine problems are
    not swallowed.

    Migrations applied here:
    - body_plain TEXT: added to store plain-text email bodies for dig/context
      subcommands.  Legacy databases created before this column was introduced
      crash with ``sqlite3.OperationalError: no such column: body_plain``
      without this guard (historic regression).
    """
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN body_plain TEXT")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def prepare_database(db_path: Path) -> None:
    """Apply additive schema and index updates before opening query connections.

    Args:
        db_path: Path to the gmail.db SQLite file.

    Raises:
        GmailDbNotFoundError: When the database file does not exist.
    """
    if not db_path.exists():
        raise GmailDbNotFoundError(f"Gmail database not found: {db_path}")
    rw_conn = sqlite3.connect(str(db_path))
    # historic regression: 5s busy_timeout prevents "database is locked" errors when a
    # concurrent gmail sync holds the write lock.
    rw_conn.execute("PRAGMA busy_timeout = 5000")
    try:
        _ensure_indexes(rw_conn)
        _ensure_schema(rw_conn)
    finally:
        rw_conn.close()


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    """Open the Gmail cache without performing schema or index writes."""
    if not db_path.exists():
        raise GmailDbNotFoundError(f"Gmail database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    # historic regression: 5s busy_timeout prevents "database is locked" errors when a
    # concurrent gmail sync holds the write lock.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def connect(db_path: Path) -> sqlite3.Connection:
    """Prepare the Gmail cache and return a read-only query connection.

    This compatibility entry point performs setup on its calling thread. Code
    that opens connections concurrently must call :func:`prepare_database`
    once before starting workers, then use :func:`connect_read_only` in them.
    """
    prepare_database(db_path)
    return connect_read_only(db_path)


_RFC2822_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M:%S %Z",
)


def _normalize_date(raw: str) -> str:
    """Return YYYY-MM-DD from an RFC 2822 header string or ISO prefix.

    Returns an empty string when *raw* cannot be parsed — callers display
    blank rather than garbage (historic regression: raw[:10] on RFC 2822 strings like
    "Wed, 6 May" produced corrupt output).
    """
    if not raw:
        return ""
    raw = raw.strip()
    # Fast path: already ISO-formatted (YYYY-MM-DD…)
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    # Strip trailing parenthesised timezone annotation like "(GMT)" or "(UTC)"
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    for fmt in _RFC2822_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Return empty string rather than raw[:10] — RFC 2822 strings like
    # "Wed, 6 May 2025 …" produce garbage when sliced to 10 chars.
    return ""


def query_by_email(conn: sqlite3.Connection, email: str, limit: int = 1) -> tuple[int, str | None]:
    """Return (thread_count, last_contact_date_iso) for threads involving *email*.

    Searches from_addr, to_addr, and cc_addr using the address indexes.
    ``last_contact_date_iso`` is None when no matching messages are found.
    The *limit* parameter is accepted for API symmetry but the aggregate result
    always spans all matching threads.
    """
    pattern = f"%{email}%"
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT m.thread_id) AS thread_count,
               (SELECT m2.date_str FROM messages m2
                WHERE  m2.from_addr LIKE ?
                   OR  m2.to_addr   LIKE ?
                   OR  m2.cc_addr   LIKE ?
                ORDER BY m2.date_epoch DESC LIMIT 1) AS last_date
        FROM   messages m
        WHERE  m.from_addr LIKE ?
           OR  m.to_addr   LIKE ?
           OR  m.cc_addr   LIKE ?
        """,
        (pattern, pattern, pattern, pattern, pattern, pattern),
    ).fetchone()
    if row is None or not row["thread_count"]:
        return (0, None)
    last_date: str | None = row["last_date"]
    return (row["thread_count"], _normalize_date(last_date) if last_date else None)


def date_to_epoch(date_str: str, is_before: bool = False) -> int:
    """Convert YYYY-MM-DD to Unix epoch (seconds). --before uses start of next day.

    For CLI option validation, prefer ``_DateEpoch`` as the Click ``type=``
    argument — it produces clean Click UsageError messages without conflicting
    with fieldkit's ``cli_main()`` exit-code taxonomy.

    This function is retained for non-Click callers (e.g. internal helpers that
    pass a pre-validated date string).  It raises ``ValueError`` on bad input;
    callers are responsible for handling it.

    Args:
        date_str: Date string in YYYY-MM-DD format.
        is_before: If True, advance by one day for exclusive upper-bound semantics.

    Raises:
        ValueError: When *date_str* does not match ``YYYY-MM-DD``.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    epoch = calendar.timegm(dt.timetuple())
    if is_before:
        epoch += 86400
    return epoch


def build_date_clause(since: int | None, before: int | None) -> tuple[str, list[Any]]:
    """Return (WHERE fragment, bind params) for date filtering via messages.

    Accepts pre-converted epoch integers (as produced by ``_DateEpoch``).
    Uses bare column name ``date_epoch`` (no table alias) so the fragment is
    valid in both aliased contexts (``messages m``) and unaliased subqueries
    (``SELECT ... FROM messages WHERE ...``).
    """
    clauses = []
    params: list[Any] = []
    if since is not None:
        clauses.append("date_epoch >= ?")
        params.append(since)
    if before is not None:
        clauses.append("date_epoch < ?")
        params.append(before)
    fragment = (" AND " + " AND ".join(clauses)) if clauses else ""
    return fragment, params


def _champion_thread_stats(
    conn: sqlite3.Connection, emails: list[str], since: int | None = None
) -> tuple[int, int, int, Any]:
    """Return (initiated, total, sent, last_row) for champion signal computation.

    Chunks the email list into batches of _BATCH_SIZE to avoid SQLite's
    1000-node expression-tree limit on large accounts (historic regression).

    Args:
        since: Optional lower-bound epoch integer.  When provided, only messages
            on or after this epoch are counted (historic regression).
    """
    if not emails:
        return 0, 0, 0, None

    # historic regression: build the date filter fragment once; applied to all per-batch queries.
    date_clause, date_params = build_date_clause(since, None)

    initiated = 0
    total = 0
    sent = 0
    last: Any = None

    for batch in _chunk_list(emails, _BATCH_SIZE):
        like_params = [f"%{e}%" for e in batch]
        from_clause = " OR ".join(["m.from_addr LIKE ?" for _ in batch])
        sent_from_clause = " OR ".join(["from_addr LIKE ?" for _ in batch])
        total_like_params = like_params * 3

        # historic regression: apply date filter inside the subquery where date_epoch is in scope.
        # The outer query only has m.min_epoch (aliased), so the bare date_epoch column
        # is not visible there.  Filtering inside the subquery is semantically equivalent
        # and avoids "no such column: date_epoch" errors.
        initiated_inner_clause = f"WHERE 1=1{date_clause}" if date_clause else ""
        initiated_sql = f"""
            SELECT COUNT(DISTINCT t.thread_id) as cnt
            FROM   threads t
            JOIN   (
                       SELECT thread_id, from_addr, MIN(date_epoch) AS min_epoch
                       FROM   messages
                       {initiated_inner_clause}
                       GROUP  BY thread_id
                   ) m ON m.thread_id = t.thread_id
            WHERE  ({from_clause})
        """
        initiated += conn.execute(initiated_sql, date_params + like_params).fetchone()["cnt"]

        total_sql = f"""
            SELECT COUNT(DISTINCT m.thread_id) as cnt
            FROM   messages m
            WHERE  ({
            " OR ".join(
                ["m.from_addr LIKE ?" for _ in batch]
                + ["m.to_addr LIKE ?" for _ in batch]
                + ["m.cc_addr LIKE ?" for _ in batch]
            )
        })
            {date_clause}
        """
        total += conn.execute(total_sql, total_like_params + date_params).fetchone()["cnt"]

        sent_sql = f"SELECT COUNT(*) as cnt FROM messages WHERE ({sent_from_clause}){date_clause}"
        sent += conn.execute(sent_sql, like_params + date_params).fetchone()["cnt"]

        last_sql = f"""
            SELECT date_str, date_epoch, subject
            FROM   messages
            WHERE  ({sent_from_clause})
            {date_clause}
            ORDER  BY date_epoch DESC
            LIMIT  1
        """
        batch_last = conn.execute(last_sql, like_params + date_params).fetchone()
        if batch_last is not None and (last is None or (batch_last["date_epoch"] or 0) > (last["date_epoch"] or 0)):
            last = batch_last

    return initiated, total, sent, last


def _champion_signal_label(pct: int) -> str:
    """Return a human-readable label for the champion initiation percentage."""
    if pct >= 30:
        return "INITIATOR — starts conversations, not just responds."
    if pct >= 15:
        return "MIXED — sometimes leads, often follows."
    return "REACTIVE — primarily responds, rarely initiates."


def query_champion_signals(conn: sqlite3.Connection, name: str, since: int | None = None) -> str:
    """Return a formatted champion-signal report for *name* using *conn*.

    Accepts an open sqlite3.Connection and a name/email fragment.  Returns
    the same text that ``cmd_champion_click`` previously printed to stdout so
    that callers (e.g. morning_brief) can filter lines without spawning a
    subprocess.

    Uses ``resolve_name()`` from ``fieldkit.gmail.names`` for name resolution so
    that display-name matching is consistent with ``query person`` (historic regression).

    Args:
        conn: Open SQLite connection to the Gmail cache database.
        name: Name or email fragment to look up.
        since: Optional lower-bound epoch integer.  When provided, only messages
            on or after this epoch are counted (historic regression).

    Returns:
        Formatted champion signal report as a string.

    Keywords preserved for downstream filtering: 'Threads initiated',
    'Last outbound', 'Signal:'.
    """
    # historic regression: use the shared ranked resolver (same as cmd_person_click) so that
    # display-name matching works correctly instead of a raw LIKE query.
    matched_rows = resolve_name(name, conn)

    if not matched_rows:
        return f"No people matched '{name}'."

    emails = [r["email"] for r in matched_rows]
    display = (matched_rows[0]["display_name"] or name) if matched_rows else name

    # historic regression: pass the parsed since epoch so date filtering is applied.
    initiated, total, sent, last = _champion_thread_stats(conn, emails, since=since)

    days_silent: int | None = None
    if last and last["date_epoch"]:
        now_epoch = int(datetime.now(UTC).timestamp())
        days_silent = (now_epoch - last["date_epoch"]) // 86400

    pct = round(100 * initiated / total) if total else 0

    lines: list[str] = [
        f"\nChampion signal: {display}\n",
        f"  Threads involved in : {total}",
        f"  Threads initiated   : {initiated}  ({pct}% initiation rate)",
        f"  Messages sent       : {sent}",
    ]
    if last:
        silence_str = f"{days_silent}d ago" if days_silent is not None else ""
        lines.append(f"  Last outbound       : {(last['date_str'] or '')[:16]}  {silence_str}")
        lines.append(f"  Last subject        : {(last['subject'] or '')[:70]}")

    lines.append("")
    lines.append(f"  Signal: {_champion_signal_label(pct)}")

    # Recent threads they started (up to 5) — chunked to avoid expression-tree limit (historic regression)
    # historic regression: use MIN(date_epoch) for actual first-message date instead of updated_at
    # (updated_at is the sync timestamp, not the email date; epochs are authoritative).
    all_recents: list[Any] = []
    for batch in _chunk_list(emails, _BATCH_SIZE):
        batch_like = [f"%{e}%" for e in batch]
        batch_from = " OR ".join(["m.from_addr LIKE ?" for _ in batch])
        recent_sql = f"""
            SELECT t.subject,
                   datetime(MIN(m2.date_epoch), 'unixepoch') AS first_sent_at,
                   MIN(m2.date_epoch) AS first_epoch
            FROM   threads t
            JOIN   (
                       SELECT thread_id, from_addr, MIN(date_epoch) AS min_epoch
                       FROM   messages
                       GROUP  BY thread_id
                   ) m ON m.thread_id = t.thread_id
            JOIN   messages m2 ON m2.thread_id = t.thread_id
            WHERE  ({batch_from})
            GROUP  BY t.thread_id, t.subject
            ORDER  BY MIN(m2.date_epoch) DESC
            LIMIT  5
        """
        all_recents.extend(conn.execute(recent_sql, batch_like).fetchall())
    # Sort all collected rows by epoch and keep top 5
    recents = sorted(all_recents, key=lambda r: r["first_epoch"] or 0, reverse=True)[:5]
    if recents:
        lines.append("\n  Threads they started (most recent):")
        for r in recents:
            date_str = (r["first_sent_at"] or "")[:10]  # YYYY-MM-DD from datetime()
            lines.append(f"    [{date_str}] {(r['subject'] or '')[:70]}")

    return "\n".join(lines)


__all__ = [
    "build_date_clause",
    "connect",
    "date_to_epoch",
    "query_champion_signals",
]
