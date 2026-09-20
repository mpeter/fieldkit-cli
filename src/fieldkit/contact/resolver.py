"""contact_resolver — library module for resolving email/name to a people-table profile.

Public API:
    resolve(email_or_name, db_path=None) -> dict
    resolve_by_email(email, conn)         -> dict
    resolve_by_name(name, conn)           -> dict
    scan_pursuit_affiliations(email_or_name, accounts_root=None) -> list[dict]

No CLI, no try/except around DB ops (errors fail loudly per R058).
"""

import re
import sqlite3
from functools import cache
from pathlib import Path
from re import compile as _compile
from typing import Any

from fieldkit.config import get_accounts_root as _get_accounts_root
from fieldkit.config import get_user_email, get_user_name
from fieldkit.gmail.discover import get_gmail_db_path


@cache
def _default_db() -> Path:
    return get_gmail_db_path()


_STRIP_RE = _compile(r"^(Re:\s*|Fwd?:\s*|AW:\s*|FWD:\s*|Subject:\s*)+", flags=re.IGNORECASE)
_OOO_RE = _compile(r"^(out of office|automatic reply|autoreply)", flags=re.IGNORECASE)


# Fields returned in every resolved profile (excludes slack_* by default for external)
_BASE_FIELDS = (
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
_SLACK_FIELDS = ("slack_user_id", "slack_message_count")


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Safely read a key from a sqlite3.Row, returning default if absent."""
    try:
        return row[key]
    except IndexError:
        return default


def _compute_champion_signal(message_count: int, initiated_count: int) -> str | None:
    """Classify contact initiation ratio as INITIATOR/MIXED/REACTIVE/None."""
    if not message_count:
        return None
    ratio = initiated_count / message_count
    if ratio >= 0.15:
        return "INITIATOR"
    if ratio >= 0.05:
        return "MIXED"
    return "REACTIVE"


def _compute_decay_signal(decay_pct: float | None) -> str | None:
    """Classify engagement trend as GONE/DECAY/ACTIVE/None."""
    if decay_pct is None:
        return None
    if decay_pct <= -100:
        return "GONE"
    if decay_pct <= -50:
        return "DECAY"
    return "ACTIVE"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Return a sqlite3 connection with row_factory and foreign_keys enabled."""
    path = db_path or _default_db()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_recent_threads(email: str, conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    """Return recent email thread subjects and snippets involving this contact.

    Queries messages where the contact appears in from_addr, to_addr, or cc_addr,
    then deduplicates by subject stem (strips Re:/Fwd: prefixes) to return distinct
    conversation topics. Returns [] if messages table is absent.
    """
    try:
        rows = conn.execute(
            """
            SELECT subject, snippet, date_str, date_epoch, from_addr
            FROM messages
            WHERE (from_addr LIKE ? OR to_addr LIKE ? OR cc_addr LIKE ?)
              AND subject != ''
            ORDER BY date_epoch DESC
            LIMIT 100
            """,
            (f"%{email}%", f"%{email}%", f"%{email}%"),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    seen_stems = set()
    result = []
    for r in rows:
        subj = (r["subject"] or "").strip()
        # Skip OOO/auto-reply noise
        if _OOO_RE.match(subj):
            continue
        stem = _STRIP_RE.sub("", subj).strip().lower()
        if stem in seen_stems:
            continue
        seen_stems.add(stem)
        result.append(
            {
                "subject": subj,
                "snippet": (r["snippet"] or "")[:200].strip(),
                "date_str": r["date_str"],
                "from_addr": r["from_addr"],
            }
        )
        if len(result) >= limit:
            break
    return result


def get_recent_meetings(email: str, conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Return recent calendar events where email is organizer or attendee.

    Queries calendar_events using organizer_email match UNION json_each(attendees)
    match, ordered by start_time DESC. Returns [] if calendar_events table is absent.
    """
    try:
        rows = conn.execute(
            """
            SELECT event_id, summary, start_time, end_time, organizer_email, attendees
            FROM calendar_events
            WHERE event_id IN (
                SELECT event_id FROM calendar_events WHERE organizer_email = ? COLLATE NOCASE
                UNION
                SELECT ce.event_id FROM calendar_events ce, json_each(ce.attendees) att
                WHERE json_extract(att.value, '$.email') = ? COLLATE NOCASE
            )
            ORDER BY start_time DESC
            LIMIT ?
            """,
            (email, email, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "event_id": r["event_id"],
            "summary": r["summary"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "organizer_email": r["organizer_email"],
            "attendees": r["attendees"],
        }
        for r in rows
    ]


_INT_FIELDS = frozenset(
    {
        "message_count",
        "thread_count",
        "initiated_count",
        "is_internal",
        "meeting_count",
        "slack_message_count",
    }
)


def build_profile(row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Convert a sqlite3.Row to a dict.

    Excludes slack_user_id and slack_message_count for external contacts (is_internal=0).
    Includes champion_signal and decay_signal computed at query time.
    When conn is provided, attaches recent_meetings from calendar_events.

    Guards all _BASE_FIELDS lookups against None so downstream string formatting
    cannot crash with TypeError.
    """
    profile = {
        f: (_row_get(row, f) if _row_get(row, f) is not None else (0 if f in _INT_FIELDS else "")) for f in _BASE_FIELDS
    }
    if _row_get(row, "is_internal"):
        for f in _SLACK_FIELDS:
            v = _row_get(row, f)
            profile[f] = v if v is not None else (0 if f in _INT_FIELDS else "")
    profile["champion_signal"] = _compute_champion_signal(
        int(profile["message_count"]), int(profile["initiated_count"])
    )
    profile["decay_signal"] = _compute_decay_signal(_row_get(row, "decay_pct"))
    if conn is not None:
        profile["recent_meetings"] = get_recent_meetings(row["email"], conn)
        profile["recent_threads"] = get_recent_threads(row["email"], conn)
    return profile


def _apply_self_name_fallback(profile: dict[str, Any]) -> None:
    """implementation change: the people-table display_name may be empty for the authenticated
    user's own address (self-lookup). Fall back to the configured identity name.
    """
    if profile.get("display_name") or not profile.get("email"):
        return
    configured_email = get_user_email()
    if configured_email and configured_email.strip().lower() == str(profile["email"]).strip().lower():
        configured_name = get_user_name()
        if configured_name:
            profile["display_name"] = configured_name


def resolve_by_email(email: str, conn: sqlite3.Connection) -> dict[str, Any]:
    """Exact match on people.email (PK). Returns resolved or not_found dict."""
    if not email:
        return {"type": "not_found", "email": email, "candidates": []}
    row = conn.execute(
        "SELECT * FROM people WHERE email = ? COLLATE NOCASE",
        (email,),
    ).fetchone()
    if row is None:
        return {"type": "not_found", "email": email, "candidates": []}
    profile = build_profile(row, conn)
    profile["type"] = "resolved"
    _apply_self_name_fallback(profile)
    return profile


def resolve_by_name(name: str, conn: sqlite3.Connection) -> dict[str, Any]:
    """Ranked multi-strategy match against display_name and email.

    Uses lib.name_resolver.resolve_name cascade (exact → token-set → fuzzy)
    so "first last" queries work even when only one token is stored in display_name.

    Returns:
      resolved  — exactly one match
      ambiguous — more than one match (candidates sorted by message_count DESC)
      not_found — zero matches
    """
    if not name:
        return {"type": "not_found", "query": name, "email": None, "candidates": []}
    from fieldkit.gmail.names import resolve_name

    rows = resolve_name(name, conn)
    if len(rows) == 0:
        return {"type": "not_found", "query": name, "email": None, "candidates": []}
    if len(rows) == 1:
        profile = build_profile(rows[0], conn)
        profile["type"] = "resolved"
        _apply_self_name_fallback(profile)
        return profile
    candidates = [
        {
            "email": r["email"],
            "display_name": r["display_name"],
            # implementation note: names.py now returns email/display_name/message_count only;
            # account is not in the result set — callers receive None here.
            "account": None,
            "message_count": r["message_count"],
        }
        for r in rows
    ]
    return {"type": "ambiguous", "query": name, "candidates": candidates}


def resolve(email_or_name: str | None, db_path: Path | None = None) -> dict[str, Any]:
    """Route to email or name resolver based on presence of '@'.

    Handles None and empty string gracefully (returns not_found).
    """
    if not email_or_name or not email_or_name.strip():
        return {"type": "not_found", "email": email_or_name, "candidates": []}
    conn = connect(db_path)
    try:
        if "@" in email_or_name:
            return resolve_by_email(email_or_name.strip().lower(), conn)
        return resolve_by_name(email_or_name.strip(), conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pursuit affiliation scanner — filesystem-only, no DB access
# ---------------------------------------------------------------------------

# Column header labels recognized for each affiliation field (case-insensitive)
_COL_ALIASES = {
    "name": {"name"},
    "title": {"title"},
    "support": {"support"},
    "meddpicc_role": {"meddpicc role", "meddpicc_role"},
    "notes": {"notes", "next step", "next steps"},
}

_EMAIL_RE = _compile(r"[\w.+\-]+@[\w.\-]+")


def _strip_bold(value: str | None) -> str | None:
    """Remove ** bold markers from a markdown table cell value."""
    return value.replace("**", "").strip() if value else None


def _find_header_idx(lines: list[str]) -> int | None:
    """Return the line index of the header row (separator row index - 1), or None."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and re.match(r"^[\|\s\-:]+$", stripped) and i > 0:
            return i - 1
    return None


def _build_col_map(header_line: str) -> dict[str, int]:
    """Build a field-name → column-index map from a pipe-table header line."""
    col_map: dict[str, int] = {}
    header_cells = [c.strip().lower() for c in header_line.split("|") if c.strip()]
    for col_idx, cell in enumerate(header_cells):
        for field, aliases in _COL_ALIASES.items():
            if cell in aliases and field not in col_map:
                col_map[field] = col_idx
    return col_map


def _split_pipe_row(line: str) -> list[str]:
    """Split a pipe-table row into trimmed cell values, dropping boundary pipes."""
    parts = line.strip().split("|")
    inner = parts[1:-1] if len(parts) > 2 else parts
    return [p.strip() for p in inner]


def _get_cell(parts: list[str], col_map: dict[str, int], field: str) -> str | None:
    """Return the stripped, de-bolded cell value for field, or None."""
    idx = col_map.get(field)
    if idx is None or idx >= len(parts):
        return None
    return _strip_bold(parts[idx]) or None


def _parse_pipe_table(lines: list[str]) -> list[dict[str, str | None]]:
    """Parse a markdown pipe table, returning list of row dicts.

    Detects header row by looking for a separator row (---|---).
    Builds a column index map using _COL_ALIASES. Skips separator rows.
    Returns [] when no valid header found.
    """
    header_idx = _find_header_idx(lines)
    if header_idx is None:
        return []

    col_map = _build_col_map(lines[header_idx])
    if "name" not in col_map:
        return []

    rows: list[dict[str, str | None]] = []
    for line in lines[header_idx + 2 :]:  # skip header + separator
        if not line.strip().startswith("|"):
            break
        parts = _split_pipe_row(line)
        name = _get_cell(parts, col_map, "name")
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "title": _get_cell(parts, col_map, "title"),
                "support": _get_cell(parts, col_map, "support"),
                "meddpicc_role": _get_cell(parts, col_map, "meddpicc_role"),
                "notes": _get_cell(parts, col_map, "notes"),
            }
        )
    return rows


def _extract_emails_from_gmail_signals(text: str) -> set[str]:
    """Return all email addresses found in Gmail Signals bullet lines."""
    emails = set()
    in_signals = False
    for line in text.splitlines():
        if re.match(r"^#{1,4}\s+Gmail Signals", line.strip()):
            in_signals = True
            continue
        if in_signals:
            # Stop at next section header of equal or higher level
            if re.match(r"^#{1,4}\s+", line.strip()) and "Gmail Signals" not in line:
                break
            for m in _EMAIL_RE.finditer(line):
                emails.add(m.group().lower())
    return emails


def _make_affiliation(rel_path: str, row: dict[str, str | None]) -> dict[str, str | None]:
    """Build a single affiliation result dict from a parsed table row."""
    return {
        "pursuit_file": rel_path,
        "name": row["name"],
        "title": row["title"],
        "support": row["support"],
        "meddpicc_role": row["meddpicc_role"],
        "notes": row["notes"],
    }


def _match_email_in_file(text: str, rel_path: str, email_lower: str) -> list[dict[str, str | None]]:
    """Return affiliations found by email address within a single file."""
    results: list[dict[str, str | None]] = []
    emails_in_signals = _extract_emails_from_gmail_signals(text)
    if email_lower in emails_in_signals:
        results.append(
            {
                "pursuit_file": rel_path,
                "name": None,
                "title": None,
                "support": None,
                "meddpicc_role": None,
                "notes": None,
            }
        )
    for row in _parse_pipe_table(text.splitlines()):
        for cell_val in (row.get("notes") or "", row.get("title") or ""):
            if email_lower in (cell_val or "").lower():
                results.append(_make_affiliation(rel_path, row))
    return results


def _match_name_in_file(text: str, rel_path: str, name_lower: str) -> list[dict[str, str | None]]:
    """Return affiliations found by display-name within a single file."""
    results: list[dict[str, str | None]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].strip().startswith("|"):
            j += 1
        for row in _parse_pipe_table(lines[i:j]):
            if row["name"] is not None and row["name"].lower() == name_lower:
                results.append(_make_affiliation(rel_path, row))
        i = j
    return results


def scan_pursuit_affiliations(
    email_or_name: str | None, accounts_root: Path | str | None = None
) -> list[dict[str, str | None]]:
    """Scan pursuit/*.md and account.md files for name/email matches.

    Args:
        email_or_name: display name (case-insensitive) or email address.
        accounts_root: Path to accounts/ dir. Defaults to fieldkit root / accounts/.

    Returns:
        List of affiliation dicts:
            {pursuit_file, name, title, support, meddpicc_role, notes}
        pursuit_file is a relative path string (relative to accounts_root).
        Never accesses the DB (R052/D018).
    """
    if not email_or_name or not email_or_name.strip():
        return []

    root = Path(accounts_root) if accounts_root else _get_accounts_root()
    if not root.exists():
        return []

    query = email_or_name.strip()
    is_email = "@" in query
    query_lower = query.lower()

    candidate_files = [
        md_file
        for md_file in root.rglob("*.md")
        if ".template" not in md_file.relative_to(root).parts and "projects" not in md_file.relative_to(root).parts
    ]

    results: list[dict[str, str | None]] = []
    for md_file in candidate_files:
        text = md_file.read_text(encoding="utf-8")
        rel_path = str(md_file.relative_to(root))
        if is_email:
            results.extend(_match_email_in_file(text, rel_path, query_lower))
        else:
            results.extend(_match_name_in_file(text, rel_path, query_lower))

    return results
