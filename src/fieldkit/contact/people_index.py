"""Build people index: extract and deduplicate email addresses from all messages.

Enriched fields computed via SQL aggregation:
- thread_count    -- distinct threads where person appears in from/to/cc
- initiated_count -- distinct threads where person sent the first message
- domain          -- extracted from email address
- is_internal     -- 1 if domain is in the configured internal_domains list
- account         -- most frequent account from thread_accounts, or NULL
"""

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import click

from fieldkit.config import get_internal_domains

_ADDR_RE = re.compile(r'"?([^"<,]*?)"?\s*<([^>]+)>')

logger = logging.getLogger(__name__)


def _internal_domain_tuple() -> tuple[str, ...]:
    """Return internal domains at call time so config changes take effect without restart."""
    return tuple(get_internal_domains())


def parse_addresses(field: str | None) -> list[tuple[str, str]]:
    """Parse a From/To/Cc header into [(display_name, email)] tuples.

    Handles 'Name <email>', bare emails, and comma-separated multi-recipient fields.
    Returns an empty list for None or blank input.
    """
    if not field or not field.strip():
        return []

    results = []
    matched_spans = []

    for m in _ADDR_RE.finditer(field):
        name = m.group(1).strip().strip('"')
        email = m.group(2).strip().lower()
        if email:
            results.append((name, email))
        matched_spans.append((m.start(), m.end()))

    # Pick up bare emails not matched by the angle-bracket pattern
    consumed: set[int] = set()
    for start, end in matched_spans:
        consumed.update(range(start, end))

    remaining = "".join(ch if i not in consumed else " " for i, ch in enumerate(field))
    for raw_part in remaining.split(","):
        part = raw_part.strip()
        if not part:
            continue
        # Must look like a bare email (contains @ but no angle brackets)
        if "@" in part and "<" not in part:
            email = part.lower().strip()
            results.append(("", email))

    return results


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Add enriched columns to people table if they don't exist yet."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(people)")}
    additions = [
        ("thread_count", "INTEGER DEFAULT 0"),
        ("initiated_count", "INTEGER DEFAULT 0"),
        ("domain", "TEXT"),
        ("is_internal", "INTEGER DEFAULT 0"),
        ("account", "TEXT"),
    ]
    for col, col_def in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE people ADD COLUMN {col} {col_def}")
    conn.commit()


def _accumulate_addresses(aggregated: dict[str, list[Any]], field: str | None, date_epoch: int) -> None:
    """Merge one address header field into the aggregated email→stats dict."""
    for name, email in parse_addresses(field or ""):
        if email not in aggregated:
            aggregated[email] = [name, date_epoch, date_epoch, 1]
        else:
            rec = aggregated[email]
            if date_epoch > rec[1]:
                rec[0] = name
                rec[1] = date_epoch
            rec[2] = min(rec[2], date_epoch)
            rec[3] += 1


def _write_base_fields(conn: sqlite3.Connection, aggregated: dict[str, list[Any]]) -> None:
    """Upsert display_name, first/last seen, and message_count into people table."""
    upsert_cur = conn.cursor()
    rows = [(email, rec[0], rec[2], rec[1], rec[3]) for email, rec in aggregated.items()]
    upsert_cur.executemany(
        """INSERT OR IGNORE INTO people
               (email, display_name, first_seen, last_seen, message_count)
           VALUES (?, ?, datetime(?, 'unixepoch'), datetime(?, 'unixepoch'), ?)""",
        rows,
    )
    upsert_cur.executemany(
        """UPDATE people
              SET display_name = ?,
                  first_seen   = MIN(first_seen, datetime(?, 'unixepoch')),
                  last_seen    = MAX(last_seen,  datetime(?, 'unixepoch')),
                  message_count = ?
            WHERE email = ?""",
        [(rec[0], rec[2], rec[1], rec[3], email) for email, rec in aggregated.items()],
    )
    conn.commit()
    logger.info("Base fields written: %d rows upserted.", len(aggregated))


def _update_domain_and_internal(conn: sqlite3.Connection) -> None:
    """Update the domain and is_internal columns for all people rows."""
    like_params: list[str] = []
    for dom in _internal_domain_tuple():
        like_params.extend([dom, f"%.{dom}"])

    if not like_params:
        # No internal domains configured — set domain only, mark all external.
        conn.execute(
            "UPDATE people SET domain = LOWER(SUBSTR(email, INSTR(email, '@') + 1)), is_internal = 0"
            " WHERE INSTR(email, '@') > 0"
        )
        conn.commit()
        logger.info("domain / is_internal updated (no internal domains configured).")
        return

    like_clauses = " OR ".join("LOWER(SUBSTR(email, INSTR(email, '@') + 1)) LIKE ?" for _ in like_params)
    conn.execute(
        f"""UPDATE people
            SET domain      = LOWER(SUBSTR(email, INSTR(email, '@') + 1)),
                is_internal = CASE WHEN {like_clauses} THEN 1 ELSE 0 END
            WHERE INSTR(email, '@') > 0""",
        like_params,
    )
    conn.commit()
    logger.info("domain / is_internal updated.")


def _build_person_thread_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Parse all messages and return (person_threads, person_initiated) dicts."""
    thread_min_epoch: dict[str, int] = {}
    for row in conn.execute("SELECT thread_id, MIN(date_epoch) AS min_ep FROM messages GROUP BY thread_id"):
        thread_min_epoch[row["thread_id"]] = row["min_ep"] or 0

    person_threads: dict[str, set[str]] = {}
    person_initiated: dict[str, set[str]] = {}

    for row in conn.execute("SELECT thread_id, from_addr, to_addr, cc_addr, date_epoch FROM messages"):
        tid = row["thread_id"]
        epoch = row["date_epoch"] or 0

        sender_emails: set[str] = set()
        for _name, email in parse_addresses(row["from_addr"] or ""):
            sender_emails.add(email)
            person_threads.setdefault(email, set()).add(tid)

        for addr_field in (row["to_addr"], row["cc_addr"]):
            for _name, email in parse_addresses(addr_field or ""):
                person_threads.setdefault(email, set()).add(tid)

        if epoch == thread_min_epoch.get(tid, -1):
            for email in sender_emails:
                person_initiated.setdefault(email, set()).add(tid)

    logger.info("Person-thread index built: %d distinct emails seen in messages.", len(person_threads))
    return person_threads, person_initiated


def _update_thread_and_account_fields(
    conn: sqlite3.Connection,
    aggregated: dict[str, list[Any]],
    person_threads: dict[str, set[str]],
    person_initiated: dict[str, set[str]],
) -> None:
    """Write thread_count, initiated_count, and account to the people table."""
    conn.executemany(
        "UPDATE people SET thread_count = ?, initiated_count = ? WHERE email = ?",
        [(len(person_threads.get(email, ())), len(person_initiated.get(email, ())), email) for email in aggregated],
    )
    conn.commit()
    logger.info("thread_count / initiated_count updated.")

    # account: most frequent account for threads this person participated in
    conn.execute("DROP TABLE IF EXISTS _tmp_person_threads")
    conn.execute("CREATE TEMP TABLE _tmp_person_threads (email TEXT, thread_id TEXT)")
    rows_to_insert = [(email, tid) for email, tids in person_threads.items() for tid in tids if email in aggregated]
    conn.executemany("INSERT INTO _tmp_person_threads VALUES (?, ?)", rows_to_insert)
    conn.execute("CREATE INDEX IF NOT EXISTS _idx_pt_email ON _tmp_person_threads(email)")
    conn.commit()
    logger.info("Temp person-thread table: %d rows.", len(rows_to_insert))

    conn.execute(
        """UPDATE people
           SET account = (
               SELECT ta.account
               FROM   _tmp_person_threads t
               JOIN   thread_accounts ta ON ta.thread_id = t.thread_id
               WHERE  t.email = people.email
               GROUP  BY ta.account
               ORDER  BY COUNT(*) DESC
               LIMIT  1
           )"""
    )
    conn.commit()
    logger.info("account updated.")
    conn.execute("DROP TABLE IF EXISTS _tmp_person_threads")


def _log_summary_stats(conn: sqlite3.Connection) -> None:
    """Log total, per-account, internal, and external contact counts."""
    total = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    per_account = conn.execute(
        "SELECT account, COUNT(*) FROM people WHERE account IS NOT NULL GROUP BY account ORDER BY COUNT(*) DESC"
    ).fetchall()

    internal_count = conn.execute("SELECT COUNT(*) FROM people WHERE is_internal = 1").fetchone()[0]
    external_count = conn.execute(
        "SELECT COUNT(*) FROM people WHERE is_internal = 0 OR is_internal IS NULL"
    ).fetchone()[0]

    # Summary to stdout so it's capturable by scripts and agents.
    per_account_str = ", ".join(f"{r[0]}: {r[1]}" for r in per_account)
    click.echo(f"Total contacts: {total}")
    click.echo(f"Per-account contacts: {per_account_str}")
    click.echo(f"Internal: {internal_count}  External: {external_count}")
    logger.debug("People index summary: total=%d internal=%d external=%d", total, internal_count, external_count)


def _execute_people_query(
    conn: sqlite3.Connection,
    *,
    account_filter: str | None,
    limit: int | None,
    show_progress: bool,
) -> tuple[sqlite3.Cursor, int]:
    """Build and execute the messages query for people index population.

    Extracted from ``build_people_index`` to reduce its cyclomatic complexity (CRAP gate).
    Returns (cursor, total_messages_for_progress).

    Args:
        conn: Open SQLite connection.
        account_filter: When set, restrict to threads tagged with this account slug.
        limit: When set, process at most this many messages.
        show_progress: When True, also query COUNT(*) for progress display.

    Returns:
        Tuple of (cursor for message rows, total_messages estimate for progress).
    """
    if account_filter:
        query = """
            SELECT m.from_addr, m.to_addr, m.cc_addr, m.date_epoch
            FROM messages m
            JOIN thread_accounts ta ON ta.thread_id = m.thread_id
            WHERE ta.account = ?
            ORDER BY m.date_epoch DESC
        """
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        cursor = conn.execute(query, (account_filter,))
        logger.info("Account filter active: restricting to account=%r", account_filter)
    else:
        query = "SELECT from_addr, to_addr, cc_addr, date_epoch FROM messages ORDER BY date_epoch DESC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        cursor = conn.execute(query)

    # Estimate total for progress display
    if show_progress and limit is None:
        try:
            total_messages: int = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        except Exception:  # noqa: BLE001
            total_messages = 0
    else:
        total_messages = limit or 0

    return cursor, total_messages


def build_people_index(
    db_path: str | Path,
    account_filter: str | None = None,
    limit: int | None = None,
    *,
    show_progress: bool = True,
) -> None:
    """Query all messages and populate the people table with deduplicated email→name mappings.

    Args:
        db_path: Path to the gmail.db SQLite database.
        account_filter: When set, restrict message processing to threads whose account tag
            matches this slug (looked up via the thread_accounts table). Useful for building
            a per-account people index without processing the entire mailbox.
        limit: When set, process at most this many messages (most-recent first via ORDER BY
            date_epoch DESC). Useful for testing on large mailboxes.
        show_progress: When True (default), emit a progress counter to stderr every 10,000
            messages and a completion summary line. Set False in tests.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        ensure_columns(conn)

        # Phase 1: base fields
        aggregated: dict[str, list[Any]] = {}
        row_count = 0

        cursor, total_messages = _execute_people_query(
            conn, account_filter=account_filter, limit=limit, show_progress=show_progress
        )

        _PROGRESS_INTERVAL = 10_000
        for row in cursor:
            date_epoch = row["date_epoch"] or 0
            for addr_field in (row["from_addr"], row["to_addr"], row["cc_addr"]):
                _accumulate_addresses(aggregated, addr_field, date_epoch)
            row_count += 1
            if show_progress and row_count % _PROGRESS_INTERVAL == 0:
                if total_messages:
                    click.echo(f"\rProcessing... {row_count:,}/{total_messages:,}", nl=False, err=True)
                else:
                    click.echo(f"\rProcessing... {row_count:,}", nl=False, err=True)

        if show_progress and row_count >= _PROGRESS_INTERVAL:
            click.echo("", err=True)  # newline after progress line

        logger.info("Processed %d messages, found %d unique email addresses.", row_count, len(aggregated))
        _write_base_fields(conn, aggregated)

        # Phase 2: enriched fields
        _update_domain_and_internal(conn)

        logger.info("Building person-thread index (parsing addresses)...")
        person_threads, person_initiated = _build_person_thread_index(conn)
        _update_thread_and_account_fields(conn, aggregated, person_threads, person_initiated)

        # Phase 3: summary stats
        _log_summary_stats(conn)
    finally:
        conn.close()
