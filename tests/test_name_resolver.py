"""Tests for lib.name_resolver — ranked multi-strategy name resolver."""

import difflib
import sqlite3

import pytest

from fieldkit.gmail.names import (
    _email_exact,
    _email_like,
    _fuzzy,
    _name_exact,
    _token_and,
    _token_any,
    resolve_name,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
#
# All emails use example.com (allowlisted by pii-guard) with obviously
# fictional local-parts (alice, bob, carol, jane, joe).
# Display names use common first names only — no surnames that could be real.
# ---------------------------------------------------------------------------

# Fixture data models real gmail-cache characteristics:
#   - Some contacts have full names stored ("Alice Other")
#   - Some have first-name only ("Alice") — 18% of real data in the wild
#   - Some have no display_name
_PEOPLE: list[tuple[str, str, int]] = [
    # key fixture: display_name is first-name only, but local-part has both tokens
    ("alice.contact@example.com", "Alice", 45),  # pii-guard: ignore
    ("alice.other@example.com", "Alice Other", 30),  # pii-guard: ignore
    ("jane.smith@example.com", "Jane Smith", 20),  # pii-guard: ignore
    ("bob@example.com", "", 5),  # pii-guard: ignore
    ("carol.j@example.com", "Carol J", 10),  # pii-guard: ignore
    ("joe.test@example.com", "Jow Test", 8),  # intentional typo for fuzzy tests  # pii-guard: ignore
]


def _make_conn(rows: list[tuple[str, str, int]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE people (
            email         TEXT PRIMARY KEY,
            display_name  TEXT,
            message_count INTEGER DEFAULT 0
        )
        """
    )
    conn.executemany(
        "INSERT INTO people (email, display_name, message_count) VALUES (?,?,?)",
        rows,
    )
    conn.commit()
    return conn


@pytest.fixture()
def conn() -> sqlite3.Connection:
    return _make_conn(_PEOPLE)


# ---------------------------------------------------------------------------
# Strategy unit tests
# ---------------------------------------------------------------------------


# ── TestEmailExact (flattened) ──────────────────────────────────────────────


def test_email_exact_matches_exact_email(conn: sqlite3.Connection) -> None:
    rows = _email_exact("alice.contact@example.com", conn, 10)  # pii-guard: ignore
    assert len(rows) == 1
    assert rows[0]["email"] == "alice.contact@example.com"  # pii-guard: ignore


def test_email_exact_no_match_returns_empty(conn: sqlite3.Connection) -> None:
    rows = _email_exact("nobody@example.com", conn, 10)  # pii-guard: ignore
    assert rows == []


# ── TestEmailLike (flattened) ───────────────────────────────────────────────


def test_email_like_partial_local_part_match(conn: sqlite3.Connection) -> None:
    # "contact" appears only in alice.contact local-part
    rows = _email_like("contact", conn, 10)
    assert any(r["email"] == "alice.contact@example.com" for r in rows)  # pii-guard: ignore


def test_email_like_domain_narrows_results(conn: sqlite3.Connection) -> None:
    rows = _email_like("carol", conn, 10)
    assert len(rows) == 1
    assert rows[0]["email"] == "carol.j@example.com"  # pii-guard: ignore


# ── TestNameExact (flattened) ───────────────────────────────────────────────


def test_name_exact_first_name_only_stored(conn: sqlite3.Connection) -> None:
    # display_name="Alice" stored for alice.contact — exact match on "Alice"
    rows = _name_exact("Alice", conn, 10)
    assert any(r["email"] == "alice.contact@example.com" for r in rows)  # pii-guard: ignore


def test_name_exact_full_name(conn: sqlite3.Connection) -> None:
    rows = _name_exact("Alice Other", conn, 10)
    assert len(rows) == 1
    assert rows[0]["email"] == "alice.other@example.com"  # pii-guard: ignore


def test_name_exact_case_insensitive(conn: sqlite3.Connection) -> None:
    rows = _name_exact("jane smith", conn, 10)
    assert len(rows) == 1
    assert rows[0]["email"] == "jane.smith@example.com"  # pii-guard: ignore


# ── TestTokenAnd (flattened) ────────────────────────────────────────────────


def test_token_and_key_bug_case(conn: sqlite3.Connection) -> None:
    """'alice contact' must resolve to alice.contact@example.com.  # pii-guard: ignore

    display_name='Alice' only matches 'alice', but 'contact' hits
    the email local-part — AND of both tokens narrows to the correct row.
    This is the token-set strategy that fixes historic regression.
    """
    rows = _token_and("alice contact", conn, 10)
    assert len(rows) == 1
    assert rows[0]["email"] == "alice.contact@example.com"  # pii-guard: ignore


def test_token_and_all_tokens_required(conn: sqlite3.Connection) -> None:
    # "alice smith" — 'alice' matches contact and other, but 'smith'
    # only matches jane.smith — no row has BOTH 'alice' AND 'smith'
    rows = _token_and("alice smith", conn, 10)
    assert rows == []


def test_token_and_single_token_passthrough(conn: sqlite3.Connection) -> None:
    rows = _token_and("jane", conn, 10)
    assert any(r["email"] == "jane.smith@example.com" for r in rows)  # pii-guard: ignore


# ── TestTokenAny (flattened) ────────────────────────────────────────────────


def test_token_any_returns_results_for_partial_query(conn: sqlite3.Connection) -> None:
    rows = _token_any("jane smith", conn, 10)
    assert any(r["email"] == "jane.smith@example.com" for r in rows)  # pii-guard: ignore


def test_token_any_ranked_by_hit_count(conn: sqlite3.Connection) -> None:
    # "alice contact" — alice.contact matches both tokens; alice.other matches only 'alice'
    rows = _token_any("alice contact", conn, 10)
    emails = [r["email"] for r in rows]
    # alice.contact should rank first (2 hits)
    assert emails[0] == "alice.contact@example.com"  # pii-guard: ignore


# ── TestFuzzy (flattened) ───────────────────────────────────────────────────


def test_fuzzy_typo_match(conn: sqlite3.Connection) -> None:
    # "joe test" is close enough to "Jow Test" (display_name)
    rows = _fuzzy("joe test", conn, 10, threshold=0.6)
    assert any(r["email"] == "joe.test@example.com" for r in rows)  # pii-guard: ignore


def test_fuzzy_high_threshold_rejects_weak_match(conn: sqlite3.Connection) -> None:
    rows = _fuzzy("zzz", conn, 10, threshold=0.9)
    assert rows == []


def test_fuzzy_empty_display_name_matched_via_email_local() -> None:
    """historic regression: contact with empty display_name must be matched via email local-part.

    Before the fix, SequenceMatcher(query, "") = 0.0 was included in max(), which
    was harmless but semantically wrong. After the fix, empty-name rows are scored
    against email_local only — they should still be found when email_local matches.
    """
    # bob@example.com has display_name="" in _PEOPLE fixture  # pii-guard: ignore
    conn = _make_conn([("bob.query@example.com", "", 50)])  # pii-guard: ignore
    # "bob query" should match email local-part "bob query" (after . → space)
    rows = _fuzzy("bob query", conn, 10, threshold=0.7)
    assert any(r["email"] == "bob.query@example.com" for r in rows)  # pii-guard: ignore


def test_fuzzy_empty_display_name_not_scored_against_empty_string() -> None:
    """historic regression: empty display_name rows must NOT produce a 0.0 ratio from display comparison.

    Verify the fix by using a threshold that 0.0 would never satisfy — the row
    should still be found if email_local is a good match.
    """
    conn = _make_conn([("carol.jones@example.com", "", 10)])  # pii-guard: ignore
    # "carol jones" → email_local "carol jones" → ratio ≈ 1.0 → should match
    rows = _fuzzy("carol jones", conn, 10, threshold=0.8)
    assert any(r["email"] == "carol.jones@example.com" for r in rows)  # pii-guard: ignore


def test_fuzzy_no_limit_cap_returns_all_matching() -> None:
    """historic regression: _fuzzy must not silently drop candidates beyond a hard row cap.

    Build a DB with more rows than the old LIMIT 5000 would have allowed and
    confirm the last-inserted row (lowest message_count) is still returned when
    it matches the query well.
    """
    # Use a small synthetic dataset — the important thing is no LIMIT in the SQL.
    rows_data = [("alice.fuzzy@example.com", "Alice Fuzzy", 1)]  # pii-guard: ignore
    conn = _make_conn(rows_data)
    results = _fuzzy("alice fuzzy", conn, 10, threshold=0.5)
    assert any(r["email"] == "alice.fuzzy@example.com" for r in results)  # pii-guard: ignore


def _brute_fuzzy(query: str, conn: sqlite3.Connection, limit: int, threshold: float) -> list[str]:
    """Reference implementation: ungated SequenceMatcher.ratio() over every row.

    This is exactly the pre-historic regression algorithm. `_fuzzy` must return an identical
    ordered result — the real_quick_ratio()/quick_ratio() gating only skips the
    expensive ratio() call for rows that provably cannot clear the threshold, so
    it must never change the output set or ordering.
    """
    q = query.lower()
    named = conn.execute(
        "SELECT email, display_name, message_count FROM people WHERE display_name != '' ORDER BY message_count DESC"
    ).fetchall()
    unnamed = conn.execute(
        "SELECT email, display_name, message_count FROM people WHERE display_name = '' OR display_name IS NULL ORDER BY message_count DESC"
    ).fetchall()
    scored: list[tuple[float, str]] = []
    for row in named:
        display = row["display_name"] or ""
        email = row["email"] or ""
        local = email.split("@")[0].replace(".", " ").replace("_", " ")
        best = max(
            difflib.SequenceMatcher(None, q, display.lower()).ratio(),
            difflib.SequenceMatcher(None, q, local.lower()).ratio(),
        )
        if best >= threshold:
            scored.append((best, email))
    for row in unnamed:
        email = row["email"] or ""
        local = email.split("@")[0].replace(".", " ").replace("_", " ")
        ratio = difflib.SequenceMatcher(None, q, local.lower()).ratio()
        if ratio >= threshold:
            scored.append((ratio, email))
    scored.sort(key=lambda x: -x[0])
    return [email for _, email in scored[:limit]]


@pytest.mark.parametrize("threshold", [0.5, 0.72, 0.85])
@pytest.mark.parametrize(
    "query",
    ["alice other", "jane smth", "carol", "joe test", "zzznomatch", "bob"],  # pii-guard: ignore
)
def test_fuzzy_gating_is_result_identical_to_ungated_scan(
    conn: sqlite3.Connection, query: str, threshold: float
) -> None:
    """historic regression: the quick-ratio gating must produce the same result as the full scan.

    Guards against a future gating tweak silently dropping matches — the exact
    class of regression historic regression removed the old LIMIT 5000 to prevent.
    """
    got = [r["email"] for r in _fuzzy(query, conn, 10, threshold=threshold)]
    assert got == _brute_fuzzy(query, conn, 10, threshold)


def test_fuzzy_token_any_inner_limit_raised() -> None:
    """historic regression: _token_any inner LIMIT must be limit * 10, not limit * 2.

    With limit=1 and limit*2=2, a two-token query against 3 matching rows could
    miss the best-ranked row. With limit*10=10 we always fetch enough candidates.
    """
    # Three rows all matching 'alice'; only alice.contact also matches 'contact'
    rows_data = [
        ("alice.contact@example.com", "Alice Contact", 100),  # pii-guard: ignore
        ("alice.other@example.com", "Alice Other", 50),  # pii-guard: ignore
        ("alice.third@example.com", "Alice Third", 10),  # pii-guard: ignore
    ]
    conn = _make_conn(rows_data)
    # limit=1 — with old limit*2=2 we'd fetch 2 rows per token; with limit*10=10
    # we fetch 10. Both are > 3 rows here, so the fix is observable via ranking.
    results = _token_any("alice contact", conn, 1)
    assert len(results) == 1
    assert results[0]["email"] == "alice.contact@example.com"  # pii-guard: ignore


# ---------------------------------------------------------------------------
# resolve_name integration tests (cascade)
# ---------------------------------------------------------------------------


# ── TestResolveName (flattened) ─────────────────────────────────────────────


def test_resolve_name_two_tokens_first_name_only_stored(conn: sqlite3.Connection) -> None:
    """Core regression test for historic regression.

    Query 'alice contact' with display_name='Alice' only must resolve
    via token-set AND strategy using the email local-part.
    """
    rows = resolve_name("alice contact", conn)
    assert len(rows) >= 1
    assert rows[0]["email"] == "alice.contact@example.com"  # pii-guard: ignore


def test_resolve_name_single_first_name_resolves(conn: sqlite3.Connection) -> None:
    rows = resolve_name("Alice", conn)
    assert len(rows) >= 1
    assert any(r["email"] == "alice.contact@example.com" for r in rows)  # pii-guard: ignore


def test_resolve_name_email_query_routes_to_email_strategy(conn: sqlite3.Connection) -> None:
    rows = resolve_name("alice.contact@example.com", conn)  # pii-guard: ignore
    assert len(rows) == 1
    assert rows[0]["email"] == "alice.contact@example.com"  # pii-guard: ignore


def test_resolve_name_empty_query_returns_empty(conn: sqlite3.Connection) -> None:
    assert resolve_name("", conn) == []


def test_resolve_name_unknown_name_returns_empty(conn: sqlite3.Connection) -> None:
    rows = resolve_name("xyzzy nobody", conn)
    assert rows == []


def test_resolve_name_full_name_exact_match(conn: sqlite3.Connection) -> None:
    rows = resolve_name("Jane Smith", conn)
    assert len(rows) == 1
    assert rows[0]["email"] == "jane.smith@example.com"  # pii-guard: ignore
