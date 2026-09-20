"""Gmail discovery — scans gmail.db and returns candidate documents
for the ingest pipeline.

Public API
----------
get_gmail_db_path()       — canonical path: <data-root>/data/gmail.db
scan_gemini_candidates()  — scan gmail.db and return GmailCandidate objects
GmailCandidate            — frozen dataclass for a discovered Gmail candidate
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from fieldkit.config import ConfigError, get_fieldkit_data

logger = logging.getLogger(__name__)

# ── regex patterns ──────────────────────────────────────────────────────────────────

_DOC_ID_RE = re.compile(r"https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)")

# Matches: Notes: "Meeting Name" May 4, 2026
# Matches both ASCII quotes and Unicode curly quotes used by Gemini in subject lines
_SUBJECT_RE = re.compile(r'^Notes:\s+[\u201c"]([^\u201d"]+)[\u201d"]\s+(\w+ \d{1,2},\s+\d{4})$')

_DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y"]

NOISE_REGEX = re.compile(
    r"noreply|no-reply|notifications|donotreply|do-not-reply|bounce|mailer-daemon|postmaster|support|alerts|automated",
    re.IGNORECASE,
)


# ── data model ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GmailCandidate:
    """Frozen dataclass for a Gemini meeting-notes candidate discovered from gmail.db.

    Produced by ``scan_gemini_candidates``; consumed by ``fieldkit.ingest.sources``.
    """

    source_id: str
    doc_url: str
    subject: str
    meeting_title: str
    meeting_date: datetime | None
    email_message_id: str
    recipient_addresses: tuple[str, ...] = ()


# ── path helpers ───────────────────────────────────────────────────────────────────


@cache
def get_gmail_db_path() -> Path:
    """Return the canonical path for gmail.db."""
    import fieldkit.config._loader as _cfg_impl

    data = _cfg_impl._read_config_dict(_cfg_impl.CONFIG_PATH)
    if data is not None and "gmail_db" in data:
        raw = str(data["gmail_db"]).strip()
        if not raw:
            raise ValueError("Config key 'gmail_db' must not be empty or whitespace")
        resolved = Path(raw).expanduser().resolve()
        if resolved.suffix.lower() != ".db":
            raise ConfigError(f"Config key 'gmail_db' must point to a .db file, got: {resolved}")
        return resolved
    return get_fieldkit_data() / "gmail.db"


def clear_gmail_caches() -> None:
    """Clear all cached gmail path/config results for test isolation."""
    get_gmail_db_path.cache_clear()


# ── DB utility helpers ───────────────────────────────────────────────────────────────────


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Return True if *table_name* exists in *conn*'s schema."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        [table_name],
    ).fetchone()
    return row is not None


# ── internal helpers ──────────────────────────────────────────────────────────────────────


def _extract_doc_id(body_html: str, body_plain: str) -> str | None:
    """Extract the first Google Doc ID from email body."""
    body = body_html if body_html and body_html.strip() else body_plain
    if not body:
        return None
    match = _DOC_ID_RE.search(body)
    return match.group(1) if match else None


def _extract_doc_url(body_html: str, body_plain: str) -> str | None:
    """Extract the first Google Docs URL from email body."""
    body = body_html if body_html and body_html.strip() else body_plain
    if not body:
        return None
    match = _DOC_ID_RE.search(body)
    return match.group(0) if match else None


def _recipient_addresses(to_addr: str, cc_addr: str) -> tuple[str, ...]:
    """Return the non-empty To and CC addresses from a Gmail row."""
    return tuple(address.strip() for field in (to_addr, cc_addr) for address in field.split(",") if address.strip())


def _parse_subject(subject: str) -> tuple[str, datetime | None]:
    """Parse meeting title and date from a Gemini notes subject line."""
    m = _SUBJECT_RE.match(subject.strip())
    if not m:
        return subject, None

    title = m.group(1).strip()
    date_str = m.group(2).strip()

    for fmt in _DATE_FORMATS:
        try:
            return title, datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    return title, None


# ── public API ────────────────────────────────────────────────────────────────────────

_DEFAULT_LIMIT = 1000


def _validate_gmail_db_path(gmail_db_path: Path) -> None:
    if gmail_db_path.suffix.lower() != ".db":
        raise ConfigError(f"gmail_db_path must point to a .db file, got: {gmail_db_path}")
    if not gmail_db_path.exists():
        raise ConfigError(f"gmail.db not found at {gmail_db_path}. Run 'fieldkit gmail sync' to populate it first.")


def _effective_scan_limit(limit: int | None, *, default_limit: int | None, require_positive_limit: bool) -> int | None:
    if limit is None:
        return default_limit
    if not isinstance(limit, int) or (require_positive_limit and limit <= 0):
        raise ValueError(f"limit must be a positive integer, got: {limit!r}")
    return int(limit)


def _load_gemini_rows(
    gmail_db_path: Path, *, effective_limit: int | None, max_age_days: int | None
) -> list[sqlite3.Row]:
    gmail_conn = sqlite3.connect(f"file:{gmail_db_path}?mode=ro", uri=True)
    gmail_conn.row_factory = sqlite3.Row
    try:
        if not table_exists(gmail_conn, "messages"):
            raise ConfigError(
                "gmail.db exists but has no 'messages' table. Run 'fieldkit gmail sync' to populate it, then retry."
            )
        message_columns = {row["name"] for row in gmail_conn.execute("PRAGMA table_info(messages)")}
        to_addr_column = "to_addr" if "to_addr" in message_columns else "''"
        cc_addr_column = "cc_addr" if "cc_addr" in message_columns else "''"
        conditions = ["from_addr LIKE '%gemini-notes@google.com%'"]
        parameters: list[int] = []
        if max_age_days is not None:
            conditions.append("date_epoch >= ?")
            parameters.append(int((datetime.now(UTC) - timedelta(days=max_age_days)).timestamp()))
        query = f"""
            SELECT message_id, from_addr, {to_addr_column} AS to_addr, {cc_addr_column} AS cc_addr,
                   subject, body_html, body_plain, date_epoch
            FROM messages
            WHERE {" AND ".join(conditions)}
            ORDER BY date_epoch DESC
        """
        if effective_limit is not None:
            query += " LIMIT ?"
            parameters.append(effective_limit)
        return gmail_conn.execute(query, parameters).fetchall()
    finally:
        gmail_conn.close()


def _candidate_from_row(row: sqlite3.Row) -> GmailCandidate | None:
    body_html: str = row["body_html"] or ""
    body_plain: str = row["body_plain"] or ""
    doc_id = _extract_doc_id(body_html, body_plain)
    if not doc_id:
        return None
    subject: str = row["subject"] or ""
    meeting_title, meeting_date = _parse_subject(subject)
    return GmailCandidate(
        source_id=doc_id,
        doc_url=_extract_doc_url(body_html, body_plain) or f"https://docs.google.com/document/d/{doc_id}",
        subject=subject,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        email_message_id=row["message_id"],
        recipient_addresses=_recipient_addresses(row["to_addr"] or "", row["cc_addr"] or ""),
    )


def scan_gemini_candidates(
    gmail_db_path: Path,
    limit: int | None,
    *,
    max_age_days: int | None = 90,
    default_limit: int | None = _DEFAULT_LIMIT,
    require_positive_limit: bool = True,
) -> list[GmailCandidate]:
    """Scan gmail.db for Gemini meeting-notes emails and return candidate objects.

    This function is responsible for gmail.db access only — it does NOT open pipeline.db.

    Raises:
        ConfigError: If ``gmail_db_path`` does not have a ``.db`` suffix (historic regression guard).
        ConfigError: If ``gmail_db_path`` does not exist.
        ValueError: If ``limit`` is not an allowed integer for the requested scan policy.
    """
    _validate_gmail_db_path(gmail_db_path)
    effective_limit = _effective_scan_limit(
        limit, default_limit=default_limit, require_positive_limit=require_positive_limit
    )
    rows = _load_gemini_rows(gmail_db_path, effective_limit=effective_limit, max_age_days=max_age_days)

    if max_age_days is not None and effective_limit is not None and len(rows) == effective_limit:
        logger.warning(
            "scan_gemini_candidates returned %d notes (limit reached). "
            "Notes older than 90 days or beyond the first %d are excluded.",
            effective_limit,
            effective_limit,
        )

    candidates = [candidate for row in rows if (candidate := _candidate_from_row(row)) is not None]

    return candidates
