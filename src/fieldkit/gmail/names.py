"""fieldkit.gmail.names — Ranked multi-strategy name resolver for the gmail people table.

Resolves a free-text query (name, email, or partial string) against the
``people`` table in gmail.db using a cascade of strategies, stopping at the
first strategy that returns results.

Strategy cascade (in order):
  1. Email exact match  (only when query contains '@')
  2. Email substring    (only when query contains '@' or '.')
  3. Full-name exact match  (display_name = query COLLATE NOCASE)
  4. Token-set AND match  — all query tokens must appear in display_name OR
     email local-part  (key fix for "first last" against single-token rows)
  5. Any-token LIKE  — union of per-token LIKE hits, ranked by token hit count
  6. difflib fuzzy  — SequenceMatcher ratio > threshold against display_name and
     email local-part  (catches typos, abbreviated names)

No external dependencies — uses only Python stdlib + the existing SQLite DB.

Public API:
  resolve_name(query, conn, limit=20, fuzzy_threshold=0.72) → list[Row]
"""

import difflib
import sqlite3
from collections import Counter
from typing import Any


def resolve_name(
    query: str,
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    fuzzy_threshold: float = 0.72,
) -> list[Any]:
    """Return up to *limit* people-table rows matching *query*.

    Applies the strategy cascade and returns an empty list when nothing matches.
    The caller is responsible for opening the connection and setting
    ``conn.row_factory = sqlite3.Row`` if dict-like access is needed.
    """
    q = query.strip()
    if not q:
        return []

    # ── Strategy 1 & 2: email-based ──────────────────────────────────────────
    if "@" in q:
        rows = _email_exact(q, conn, limit)
        if rows:
            return rows
        rows = _email_like(q, conn, limit)
        if rows:
            return rows

    # ── Strategy 3: exact full-name match ────────────────────────────────────
    rows = _name_exact(q, conn, limit)
    if rows:
        return rows

    # ── Strategy 4: token-set AND match ──────────────────────────────────────
    rows = _token_and(q, conn, limit)
    if rows:
        return rows

    # ── Strategy 5: any-token LIKE union (ranked by hit count) ───────────────
    rows = _token_any(q, conn, limit)
    if rows:
        return rows

    # ── Strategy 6: fuzzy fallback ───────────────────────────────────────────
    return _fuzzy(q, conn, limit, fuzzy_threshold)


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------


def _email_exact(query: str, conn: sqlite3.Connection, limit: int) -> list[Any]:
    return conn.execute(
        "SELECT email, display_name, message_count FROM people WHERE email = ? COLLATE NOCASE ORDER BY message_count DESC LIMIT ?",
        (query, limit),
    ).fetchall()


def _email_like(query: str, conn: sqlite3.Connection, limit: int) -> list[Any]:
    pattern = f"%{query}%"
    return conn.execute(
        "SELECT email, display_name, message_count FROM people WHERE email LIKE ? ORDER BY message_count DESC LIMIT ?",
        (pattern, limit),
    ).fetchall()


def _name_exact(query: str, conn: sqlite3.Connection, limit: int) -> list[Any]:
    return conn.execute(
        "SELECT email, display_name, message_count FROM people WHERE display_name = ? COLLATE NOCASE ORDER BY message_count DESC LIMIT ?",
        (query, limit),
    ).fetchall()


def _token_and(query: str, conn: sqlite3.Connection, limit: int) -> list[Any]:
    """Return rows where ALL query tokens appear in display_name OR email local-part.

    This is the key fix for a two-token query like "Alice Contact" against a row
    where display_name="Alice" and the email local-part contains "contact" —
    token 'alice' hits display_name, token 'contact' hits the email local-part,
    AND of both returns the correct row.
    """
    tokens = query.lower().split()
    if not tokens:
        return []

    # Each token must match: display_name LIKE '%token%' OR email-local LIKE '%token%'
    clauses = []
    params: list[str] = []
    for token in tokens:
        clauses.append("(LOWER(display_name) LIKE ? OR LOWER(SUBSTR(email, 1, INSTR(email,'@')-1)) LIKE ?)")
        params.extend([f"%{token}%", f"%{token}%"])
    params.append(str(limit))

    sql = f"SELECT email, display_name, message_count FROM people WHERE {' AND '.join(clauses)} ORDER BY message_count DESC LIMIT ?"
    return conn.execute(sql, params).fetchall()


def _token_any(query: str, conn: sqlite3.Connection, limit: int) -> list[Any]:
    """Union of per-token LIKE hits, de-duplicated and ranked by number of token hits."""
    tokens = query.lower().split()
    if not tokens:
        return []

    # historic regression: single OR'd query across all tokens instead of one LIKE scan
    # per token. Inner LIMIT stays limit * 10 (historic regression) — same cap as before,
    # now applied once instead of once per token.
    clauses = []
    params: list[str] = []
    for token in tokens:
        pattern = f"%{token}%"
        clauses.append("(LOWER(display_name) LIKE ? OR LOWER(email) LIKE ?)")
        params.extend([pattern, pattern])
    params.append(str(limit * 10))

    sql = (
        "SELECT email, display_name, message_count FROM people WHERE "
        + " OR ".join(clauses)
        + " ORDER BY message_count DESC LIMIT ?"
    )
    rows = conn.execute(sql, params).fetchall()

    # Rank by number of tokens each row hits — computed in Python from the
    # already-fetched columns instead of a second query per token.
    # implementation note: Counter simplifies the manual hit_count dict accumulation.
    hit_count: Counter[str] = Counter()
    seen: dict[str, Any] = {}
    for row in rows:
        email = row["email"] if hasattr(row, "__getitem__") else row[0]
        display_name = (row["display_name"] if hasattr(row, "__getitem__") else row[1]) or ""
        haystack = f"{display_name} {email}".lower()
        hit_count[email] = sum(1 for token in tokens if token in haystack)
        seen.setdefault(email, row)

    # Sort by token hits DESC via most_common(), then take top *limit*
    ranked = hit_count.most_common()
    return [seen[e] for e, _ in ranked[:limit]]


def _fuzzy(query: str, conn: sqlite3.Connection, limit: int, threshold: float) -> list[Any]:
    """difflib.SequenceMatcher fuzzy match against display_name and email local-part.

    historic regression: Removed LIMIT 5000 cap; pre-filters WHERE display_name != '' to reduce
    work on rows that will only be scored against email_local anyway.
    historic regression: When display_name is empty, skip the display comparison entirely and score
    against email_local only — avoids SequenceMatcher(query, "") = 0.0 polluting max().
    """
    # historic regression: no LIMIT; filter empty display_name rows separately below so we still
    # process them via email_local, but we avoid loading them in the primary scan.
    # We fetch all rows — the WHERE display_name != '' pre-filters the majority of
    # low-quality rows, then we union with the empty-name rows scored by email_local.
    named_candidates = conn.execute(
        "SELECT email, display_name, message_count FROM people WHERE display_name != '' ORDER BY message_count DESC"
    ).fetchall()
    unnamed_candidates = conn.execute(
        "SELECT email, display_name, message_count FROM people WHERE display_name = '' OR display_name IS NULL ORDER BY message_count DESC"
    ).fetchall()

    scored: list[tuple[float, Any]] = []
    q_lower = query.lower()

    # historic regression: skip the O(n*m) SequenceMatcher.ratio() on rows that cannot possibly
    # clear the threshold. real_quick_ratio() (length-only, O(1)) and quick_ratio()
    # (character-multiset, O(n)) are guaranteed upper bounds on ratio(), so a candidate
    # failing either can never reach threshold and is skipped without the full match.
    # This is the same short-circuit difflib.get_close_matches() uses internally.
    # Result-identical — no row cap — so it does NOT reintroduce the silent-drop
    # regression historic regression removed the old LIMIT 5000 to fix.
    #
    # Seq assignment mirrors the old positional SequenceMatcher(None, query, candidate)
    # exactly: a=query (fixed), b=candidate (varies). ratio() is NOT symmetric in a/b,
    # so query must stay the first arg or the scores — and thus the ranking — shift.
    matcher = difflib.SequenceMatcher()
    matcher.set_seq1(q_lower)

    def _gated_ratio(candidate: str) -> float:
        matcher.set_seq2(candidate)
        if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
            return 0.0
        return matcher.ratio()

    for row in named_candidates:
        display = (row["display_name"] or "") if hasattr(row, "__getitem__") else ""
        email = (row["email"] or "") if hasattr(row, "__getitem__") else ""
        email_local = email.split("@")[0].replace(".", " ").replace("_", " ")

        best = max(_gated_ratio(display.lower()), _gated_ratio(email_local.lower()))
        if best >= threshold:
            scored.append((best, row))

    # historic regression: For rows with no display_name, score against email_local only.
    # Comparing against "" always yields 0.0 and would never exceed threshold anyway,
    # but explicitly skipping it avoids the misleading max(ratio, 0.0) pattern.
    for row in unnamed_candidates:
        email = (row["email"] or "") if hasattr(row, "__getitem__") else ""
        email_local = email.split("@")[0].replace(".", " ").replace("_", " ")

        ratio = _gated_ratio(email_local.lower())
        if ratio >= threshold:
            scored.append((ratio, row))

    scored.sort(key=lambda x: -x[0])
    return [row for _, row in scored[:limit]]
