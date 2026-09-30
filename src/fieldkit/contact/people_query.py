"""Read-only queries against the people-index cache built by ``contact.people_index``.

Backs ``contact list`` — a pure read of the existing cache. Rebuilding the cache is a
separate concern owned by the ``sync`` pipeline (see ``fieldkit.datasync``), never
triggered implicitly by a read.
"""

from pathlib import Path
from typing import Any

from fieldkit.gmail.query_domain import connect as connect_gmail_cache

_COLUMNS = (
    "email",
    "display_name",
    "first_seen",
    "last_seen",
    "message_count",
    "thread_count",
    "initiated_count",
    "domain",
    "account",
    "is_internal",
    "meeting_count",
)


def list_people(
    db_path: str | Path,
    *,
    account: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return people-index rows, most recently seen first.

    Args:
        db_path: Path to the gmail.db SQLite database.
        account: When set, restrict to people whose most-frequent account matches this slug.
        limit: When set, return at most this many rows.

    Returns:
        A list of dicts (one per person), each with the columns in ``_COLUMNS``.
    """
    conn = connect_gmail_cache(Path(db_path))
    try:
        cols = ", ".join(_COLUMNS)
        query = f"SELECT {cols} FROM people"
        params: list[str] = []
        if account:
            query += " WHERE account = ?"
            params.append(account)
        query += " ORDER BY last_seen DESC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
