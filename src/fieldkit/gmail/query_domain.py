"""Canonical Gmail query, connection, and contact-analysis behavior."""

import calendar
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fieldkit.config import get_internal_domains
from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.gmail.address_query import exact_message_address_filter, register_exact_address_matcher
from fieldkit.gmail.addresses import parse_address_header
from fieldkit.gmail.constants import AUTOMATION_NOISE_DOMAINS
from fieldkit.gmail.exceptions import GmailDbNotFoundError, GmailSchemaError
from fieldkit.gmail.names import resolve_name
from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, open_gmail_publication, publication_root_for
from fieldkit.gmail.query_support import NOISE_REGEX, scan_row_budget, sqlite_query_budget
from fieldkit.sqlite_publication import SQLitePublicationError

_BATCH_SIZE = 50


def _chunk_list(lst: list[str], size: int) -> list[list[str]]:
    """Split a list into chunks of at most *size* items.

    An empty list produces one empty chunk so callers get a zero result
    without special-casing the empty-input path.
    """
    return [lst[i : i + size] for i in range(0, max(len(lst), 1), size)]


def prepare_database(db_path: Path) -> None:
    """Validate the ready published cache before concurrent query connections.

    Args:
        db_path: Path to the gmail.db SQLite file.

    Raises:
        GmailDbNotFoundError: When the database file does not exist.
    """
    with connect(db_path):
        pass


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    """Open only the ready committed Gmail cache generation."""
    if not publication_root_for(db_path).is_dir():
        if db_path.exists() or db_path.is_symlink():
            raise SQLiteSnapshotError("Gmail cache requires explicit import", reason="unverified")
        raise GmailDbNotFoundError("Gmail cache has not been published; run 'fieldkit gmail sync' first")
    try:
        return open_gmail_publication(db_path)
    except SQLitePublicationError as exc:
        if exc.reason in {"active", "resource"}:
            raise GmailSyncPartialError("Gmail cache publication is not ready; retry may help") from None
        raise SQLiteSnapshotError("Gmail cache publication is unverified", reason="unverified") from None


def _validate_query_schema(conn: sqlite3.Connection) -> None:
    """Reject caches that require an explicit sync-owned schema migration."""
    required = {
        "threads": {"thread_id", "subject", "message_count", "updated_at"},
        "messages": {
            "message_id",
            "thread_id",
            "from_addr",
            "to_addr",
            "cc_addr",
            "subject",
            "date_str",
            "date_epoch",
            "body_plain",
        },
        "people": {
            "email",
            "display_name",
            "message_count",
            "thread_count",
            "meeting_count",
            "slack_message_count",
            "last_seen",
            "is_internal",
            "account",
        },
        "thread_accounts": {"thread_id", "account"},
        "sync_state": {"key", "value"},
    }
    missing: list[str] = []
    for table, columns in required.items():
        present = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if absent := sorted(columns - present):
            missing.append(f"{table} ({', '.join(absent)})")
    if missing:
        details = "; ".join(missing)
        raise GmailSchemaError(
            f"Gmail cache schema is incomplete: {details}. Run 'fieldkit gmail sync' to migrate the cache."
        )


def connect(db_path: Path) -> sqlite3.Connection:
    """Open and validate an existing Gmail cache without modifying it."""
    conn = connect_read_only(db_path)
    try:
        _validate_query_schema(conn)
        ready = conn.execute("SELECT value FROM sync_state WHERE key = ?", (GMAIL_QUERY_READY_KEY,)).fetchone()
        if ready is None or ready[0] != "true":
            raise GmailSyncPartialError("Gmail cache publication is not ready; retry may help")
    except Exception:
        conn.close()
        raise
    return conn


_RFC2822_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M:%S %Z",
)


def _normalize_date(raw: str) -> str:
    """Return YYYY-MM-DD from an RFC 2822 header string or ISO prefix.

    Returns an empty string when *raw* cannot be parsed — callers display
    blank rather than slicing an unparsed RFC 2822 value.
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


def query_by_email(conn: sqlite3.Connection, email: str) -> tuple[int, str | None]:
    """Return (thread_count, last_contact_date_iso) for threads involving *email*.

    Searches from_addr, to_addr, and cc_addr by exact parsed mailbox address.
    ``last_contact_date_iso`` is None when no matching messages are found.
    """
    register_exact_address_matcher(conn)
    match_m, params_m = exact_message_address_filter(email, alias="m.")
    match_m2, params_m2 = exact_message_address_filter(email, alias="m2.")
    with sqlite_query_budget(conn, row_budget=scan_row_budget(1)) as budget:
        try:
            row = conn.execute(
                f"""
            SELECT COUNT(DISTINCT m.thread_id) AS thread_count,
                   (SELECT m2.date_str FROM messages m2
                    WHERE {match_m2}
                    ORDER BY m2.date_epoch DESC LIMIT 1) AS last_date
            FROM   messages m
            WHERE {match_m}
            """,
                (*params_m2, *params_m),
            ).fetchone()
        except sqlite3.DatabaseError:
            if budget.exhausted:
                raise GmailSyncPartialError("Gmail contact query exceeded its SQLite work bound") from None
            raise SQLiteSnapshotError("Gmail cache contact data is unverified", reason="unverified") from None
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


def _internal_blind_domains() -> set[str]:
    """Return internal blind domains from the current configuration."""
    return set(get_internal_domains()) | set(AUTOMATION_NOISE_DOMAINS)


def _is_noise(email: str) -> bool:
    domain = email.split("@")[-1] if "@" in email else ""
    if domain in _internal_blind_domains():
        return True
    return bool(NOISE_REGEX.search(email))


def _fetch_blindspot_messages(
    conn: sqlite3.Connection,
    thread_ids: list[str],
    since: int | None,
) -> list[Any]:
    """Fetch address and activity rows for account threads."""
    placeholders = ",".join("?" * len(thread_ids))
    if since is not None:
        return conn.execute(
            f"SELECT from_addr, to_addr, date_epoch FROM messages "
            f"WHERE thread_id IN ({placeholders}) AND date_epoch >= ?",
            [*thread_ids, since],
        ).fetchall()
    return conn.execute(
        f"SELECT from_addr, to_addr, date_epoch FROM messages WHERE thread_id IN ({placeholders})",
        thread_ids,
    ).fetchall()


def _build_addr_stats(msg_rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Aggregate per-address message count and last-seen epoch."""
    addr_stats: dict[str, dict[str, Any]] = {}
    for row in msg_rows:
        for header, field in ((row[0], "sender address"), (row[1], "recipient address")):
            for _name, email in parse_address_header(header, field=field):
                if _is_noise(email):
                    continue
                if email not in addr_stats:
                    addr_stats[email] = {"msgs": 0, "last_epoch": 0}
                addr_stats[email]["msgs"] += 1
                epoch: int = row[2] or 0
                if epoch > addr_stats[email]["last_epoch"]:
                    addr_stats[email]["last_epoch"] = epoch
    return addr_stats


def query_blindspots(
    conn: sqlite3.Connection,
    account: str,
    since: int | None = None,
    limit: int | None = 50,
) -> list[tuple[str, str, int, int]]:
    """Return external contacts active in account threads."""
    thread_id_rows = conn.execute(
        "SELECT thread_id FROM thread_accounts WHERE LOWER(account) = ?",
        [account.lower()],
    ).fetchall()
    if not thread_id_rows:
        return []

    thread_ids = [row[0] for row in thread_id_rows]
    addr_stats = _build_addr_stats(_fetch_blindspot_messages(conn, thread_ids, since))
    if not addr_stats:
        return []

    placeholders = ",".join("?" * len(addr_stats))
    name_rows = conn.execute(
        f"SELECT email, display_name FROM people WHERE email IN ({placeholders})",
        list(addr_stats),
    ).fetchall()
    name_map: dict[str, str] = {row[0]: row[1] or "" for row in name_rows}
    results = [
        (email, name_map.get(email, ""), stats["msgs"], stats["last_epoch"]) for email, stats in addr_stats.items()
    ]
    results.sort(key=lambda item: item[3], reverse=True)
    return results[:limit] if limit is not None else results


def query_dig(
    conn: sqlite3.Connection,
    account: str,
    keyword: str,
    since: int | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Return account threads whose subject or body matches a keyword."""
    pattern = f"%{keyword}%"
    if since is not None:
        sql = """
            SELECT t.thread_id, t.subject, t.message_count,
                   first_msg.from_addr, first_msg.date_str AS first_date,
                   (SELECT mx.date_str FROM messages mx
                    WHERE mx.thread_id = t.thread_id
                    ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
            FROM   threads t
            JOIN   thread_accounts ta ON ta.thread_id = t.thread_id
            JOIN   (
                       SELECT thread_id, from_addr, date_str
                       FROM   messages
                       WHERE  (subject LIKE ? OR body_plain LIKE ?)
                       GROUP  BY thread_id
                       HAVING MIN(date_epoch) > 0
                   ) first_msg ON first_msg.thread_id = t.thread_id
            WHERE  LOWER(ta.account) = ?
              AND  t.thread_id IN (
                       SELECT DISTINCT thread_id FROM messages WHERE date_epoch >= ?
                   )
            ORDER  BY last_date DESC
            LIMIT  ?
        """
        params: list[Any] = [pattern, pattern, account.lower(), since, limit]
    else:
        sql = """
            SELECT t.thread_id, t.subject, t.message_count,
                   first_msg.from_addr, first_msg.date_str AS first_date,
                   (SELECT mx.date_str FROM messages mx
                    WHERE mx.thread_id = t.thread_id
                    ORDER BY mx.date_epoch DESC LIMIT 1) AS last_date
            FROM   threads t
            JOIN   thread_accounts ta ON ta.thread_id = t.thread_id
            JOIN   (
                       SELECT thread_id, from_addr, date_str
                       FROM   messages
                       WHERE  (subject LIKE ? OR body_plain LIKE ?)
                       GROUP  BY thread_id
                       HAVING MIN(date_epoch) > 0
                   ) first_msg ON first_msg.thread_id = t.thread_id
            WHERE  LOWER(ta.account) = ?
            ORDER  BY last_date DESC
            LIMIT  ?
        """
        params = [pattern, pattern, account.lower(), limit]

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "thread_id": row["thread_id"],
            "subject": row["subject"],
            "message_count": row["message_count"],
            "from_addr": row["from_addr"],
            "first_date": row["first_date"],
            "last_date": row["last_date"],
        }
        for row in rows
    ]


def _champion_thread_stats(
    conn: sqlite3.Connection, emails: list[str], since: int | None = None
) -> tuple[int, int, int, Any]:
    """Return (initiated, total, sent, last_row) for champion signal computation.

    Chunks the email list into batches of _BATCH_SIZE to avoid SQLite's
    1000-node expression-tree limit on large accounts.

    Args:
        since: Optional lower-bound epoch integer.  When provided, only messages
            on or after this epoch are counted.
    """
    if not emails:
        return 0, 0, 0, None

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

        # Apply the date filter inside the subquery where date_epoch is in scope.
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
    the text consumed by the CLI and other domain callers without spawning a
    subprocess.

    Uses ``resolve_name()`` from ``fieldkit.gmail.names`` for name resolution so
    that display-name matching is consistent with ``query person``.

    Args:
        conn: Open SQLite connection to the Gmail cache database.
        name: Name or email fragment to look up.
        since: Optional lower-bound epoch integer.  When provided, only messages
            on or after this epoch are counted.

    Returns:
        Formatted champion signal report as a string.

    Keywords preserved for downstream filtering: 'Threads initiated',
    'Last outbound', 'Signal:'.
    """
    matched_rows = resolve_name(name, conn)

    if not matched_rows:
        return f"No people matched '{name}'."

    emails = [r["email"] for r in matched_rows]
    display = (matched_rows[0]["display_name"] or name) if matched_rows else name

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

    # Recent threads they started, using message epochs rather than sync timestamps.
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
