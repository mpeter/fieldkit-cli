"""Build people index: extract and deduplicate email addresses from all messages.

Enriched fields computed via SQL aggregation:
- thread_count    -- distinct threads where person appears in from/to/cc
- initiated_count -- distinct threads where person sent the first message
- domain          -- extracted from email address
- is_internal     -- 1 if domain is in the configured internal_domains list
- account         -- most frequent account from thread_accounts, or NULL
"""

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from fieldkit.config import get_internal_domains
from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.gmail.addresses import parse_address_header
from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page
from fieldkit.gmail.query_domain import connect as connect_gmail_cache
from fieldkit.sqlite_publication import SQLiteMutationConnection, SQLiteMutationCursor, SQLitePublicationError

logger = logging.getLogger(__name__)


def _internal_domain_tuple() -> tuple[str, ...]:
    """Return internal domains at call time so config changes take effect without restart."""
    return tuple(get_internal_domains())


def _accumulate_addresses(aggregated: dict[str, list[Any]], field: str | None, date_epoch: int) -> None:
    """Merge one address header field into the aggregated email→stats dict."""
    for name, email in parse_address_header(field, field="message address"):
        if email not in aggregated:
            aggregated[email] = [name, date_epoch, date_epoch, 1]
        else:
            rec = aggregated[email]
            if date_epoch > rec[1]:
                rec[0] = name
                rec[1] = date_epoch
            rec[2] = min(rec[2], date_epoch)
            rec[3] += 1


def _write_base_fields(conn: SQLiteMutationConnection, aggregated: dict[str, list[Any]]) -> None:
    """Reconcile current people and write base fields inside the publication transaction."""
    rows = [(email, rec[0], rec[2], rec[1], rec[3]) for email, rec in aggregated.items()]
    conn.executemany(
        """
        INSERT INTO people(email, display_name, first_seen, last_seen, message_count)
        VALUES (?, ?, datetime(?, 'unixepoch'), datetime(?, 'unixepoch'), ?)
        ON CONFLICT(email) DO UPDATE SET
            display_name = excluded.display_name,
            first_seen = excluded.first_seen,
            last_seen = excluded.last_seen,
            message_count = excluded.message_count
        """,
        rows,
    )
    logger.info("Base fields written: %d rows upserted.", len(aggregated))


def _update_domain_and_internal(conn: SQLiteMutationConnection, emails: tuple[str, ...]) -> None:
    """Update domain and internal status for the current reconciled people."""
    if not emails:
        return
    like_params: list[str] = []
    for dom in _internal_domain_tuple():
        like_params.extend([dom, f"%.{dom}"])

    if not like_params:
        # No internal domains configured — set domain only, mark all external.
        conn.executemany(
            """
            UPDATE people
            SET domain = LOWER(SUBSTR(email, INSTR(email, '@') + 1)), is_internal = 0
            WHERE email = ? AND INSTR(email, '@') > 0
            """,
            ((email,) for email in emails),
        )
        logger.info("domain / is_internal updated (no internal domains configured).")
        return

    like_clauses = " OR ".join("LOWER(SUBSTR(email, INSTR(email, '@') + 1)) LIKE ?" for _ in like_params)
    conn.executemany(
        f"""
        UPDATE people
            SET domain      = LOWER(SUBSTR(email, INSTR(email, '@') + 1)),
                is_internal = CASE WHEN {like_clauses} THEN 1 ELSE 0 END
        WHERE email = ? AND INSTR(email, '@') > 0
        """,
        ((*like_params, email) for email in emails),
    )
    logger.info("domain / is_internal updated.")


def _build_person_thread_index(
    conn: SQLiteMutationConnection,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Parse all messages and return (person_threads, person_initiated) dicts."""
    thread_min_epoch: dict[str, int] = {}
    for row in conn.execute(
        "SELECT thread_id, MIN(date_epoch) AS min_ep FROM messages GROUP BY thread_id",
    ):
        thread_min_epoch[str(row[0])] = int(row[1] or 0)

    person_threads: dict[str, set[str]] = {}
    person_initiated: dict[str, set[str]] = {}

    for row in conn.execute(
        "SELECT thread_id, from_addr, to_addr, cc_addr, date_epoch FROM messages",
    ):
        tid = str(row[0])
        epoch = int(row[4] or 0)

        sender_emails: set[str] = set()
        for _name, email in parse_address_header(row[1], field="sender address"):
            sender_emails.add(email)
            person_threads.setdefault(email, set()).add(tid)

        for addr_field in (row[2], row[3]):
            for _name, email in parse_address_header(addr_field, field="recipient address"):
                person_threads.setdefault(email, set()).add(tid)

        if epoch == thread_min_epoch.get(tid, -1):
            for email in sender_emails:
                person_initiated.setdefault(email, set()).add(tid)

    logger.info("Person-thread index built: %d distinct emails seen in messages.", len(person_threads))
    return person_threads, person_initiated


def _update_thread_and_account_fields(
    conn: SQLiteMutationConnection,
    aggregated: dict[str, list[Any]],
    person_threads: dict[str, set[str]],
    person_initiated: dict[str, set[str]],
) -> None:
    """Write thread_count, initiated_count, and account to the people table."""
    account_by_thread: dict[str, list[str]] = {}
    for row in conn.execute("SELECT thread_id, account FROM thread_accounts ORDER BY account"):
        account_by_thread.setdefault(str(row[0]), []).append(str(row[1]))
    updates: list[tuple[int, int, str | None, str]] = []
    for email in aggregated:
        accounts = Counter(
            account for thread_id in person_threads.get(email, ()) for account in account_by_thread.get(thread_id, ())
        )
        selected_account = sorted(accounts, key=lambda account: (-accounts[account], account))[0] if accounts else None
        updates.append(
            (
                len(person_threads.get(email, ())),
                len(person_initiated.get(email, ())),
                selected_account,
                email,
            )
        )
    conn.executemany(
        "UPDATE people SET thread_count = ?, initiated_count = ?, account = ? WHERE email = ?",
        updates,
    )
    logger.info("thread_count / initiated_count / account updated.")


@dataclass(frozen=True)
class _IndexSummary:
    total: int
    per_account: tuple[tuple[str, int], ...]
    internal: int
    external: int


def _summary_stats(conn: SQLiteMutationConnection) -> _IndexSummary:
    """Return summary counts from the candidate publication generation."""
    total = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    per_account = conn.execute(
        "SELECT account, COUNT(*) FROM people WHERE account IS NOT NULL GROUP BY account ORDER BY COUNT(*) DESC"
    ).fetchall()

    internal_count = conn.execute("SELECT COUNT(*) FROM people WHERE is_internal = 1").fetchone()[0]
    external_count = conn.execute(
        "SELECT COUNT(*) FROM people WHERE is_internal = 0 OR is_internal IS NULL"
    ).fetchone()[0]

    return _IndexSummary(
        total=int(total),
        per_account=tuple((str(row[0]), int(row[1])) for row in per_account),
        internal=int(internal_count),
        external=int(external_count),
    )


def _execute_people_query(
    conn: SQLiteMutationConnection,
    *,
    show_progress: bool,
) -> tuple[SQLiteMutationCursor, int]:
    """Build and execute the messages query for people index population.

    Extracted from ``build_people_index`` to reduce its cyclomatic complexity (CRAP gate).
    Returns (cursor, total_messages_for_progress).

    Args:
        conn: Open SQLite connection.
        show_progress: When True, also query COUNT(*) for progress display.

    Returns:
        Tuple of (cursor for message rows, total_messages estimate for progress).
    """
    query = "SELECT from_addr, to_addr, cc_addr, date_epoch FROM messages ORDER BY date_epoch DESC"
    cursor = conn.execute(query)
    total_messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]) if show_progress else 0

    return cursor, total_messages


def build_people_index(
    db_path: str | Path,
    account_filter: str | None = None,
    *,
    show_progress: bool = True,
) -> None:
    """Query all messages and populate the people table with deduplicated email→name mappings.

    Args:
        db_path: Path to the gmail.db SQLite database.
        account_filter: Optional account that triggered the rebuild. The published index is
            always reconciled from the complete current message set so contacts shared by
            multiple accounts retain their complete history.
        show_progress: When True (default), emit a progress counter to stderr every 10,000
            messages and a completion summary line. Set False in tests.
    """
    path = Path(db_path)
    summary: _IndexSummary | None = None
    with connect_gmail_cache(path):
        pass

    def rebuild(conn: SQLiteMutationConnection) -> None:
        nonlocal summary
        ready = conn.execute("SELECT value FROM sync_state WHERE key = ?", (GMAIL_QUERY_READY_KEY,)).fetchone()
        if ready is None or ready[0] != "true":
            raise GmailSyncPartialError("Gmail cache publication is not ready; complete sync before rebuilding people")
        # Phase 1: base fields
        aggregated: dict[str, list[Any]] = {}
        row_count = 0

        if account_filter is not None:
            logger.info("Account-scoped refresh triggered a complete people-index reconciliation.")
        cursor, total_messages = _execute_people_query(conn, show_progress=show_progress)

        _PROGRESS_INTERVAL = 10_000
        for row in cursor:
            date_epoch = int(row[3] or 0)
            for addr_field in row[:3]:
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
        conn.execute("CREATE TEMP TABLE _current_people(email TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO _current_people(email) VALUES (?)", ((email,) for email in sorted(aggregated)))
        conn.execute("DELETE FROM people WHERE email NOT IN (SELECT email FROM _current_people)")
        _write_base_fields(conn, aggregated)

        # Phase 2: enriched fields
        _update_domain_and_internal(conn, tuple(aggregated))

        logger.info("Building person-thread index (parsing addresses)...")
        person_threads, person_initiated = _build_person_thread_index(conn)
        _update_thread_and_account_fields(conn, aggregated, person_threads, person_initiated)

        conn.execute("DROP TABLE _current_people")
        summary = _summary_stats(conn)

    try:
        apply_gmail_page(path, rebuild)
    except SQLitePublicationError as exc:
        if exc.reason == "active":
            raise SQLiteSnapshotError("Gmail cache publication is unverified", reason="active") from None
        raise SQLiteSnapshotError("Gmail cache publication is unverified", reason="unverified") from None
    if summary is None:
        raise RuntimeError("People index rebuild did not produce a summary")
    per_account = ", ".join(f"{account}: {count}" for account, count in summary.per_account)
    click.echo(f"Total contacts: {summary.total}")
    click.echo(f"Per-account contacts: {per_account}")
    click.echo(f"Internal: {summary.internal}  External: {summary.external}")
    logger.debug(
        "People index summary: total=%d internal=%d external=%d",
        summary.total,
        summary.internal,
        summary.external,
    )
