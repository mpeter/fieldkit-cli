"""Unit tests for tools/gmail-cache/contact_resolver.py.

All tests use an in-memory SQLite DB — no dependency on live data.
"""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.contact import resolver as cr

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CREATE_PEOPLE = """
CREATE TABLE people (
    email               TEXT PRIMARY KEY,
    display_name        TEXT,
    first_seen          TEXT,
    last_seen           TEXT,
    message_count       INTEGER DEFAULT 0,
    thread_count        INTEGER DEFAULT 0,
    initiated_count     INTEGER DEFAULT 0,
    domain              TEXT,
    account             TEXT,
    is_internal         INTEGER DEFAULT 0,
    meeting_count       INTEGER DEFAULT 0,
    slack_user_id       TEXT,
    slack_message_count INTEGER DEFAULT 0
);
"""


def _make_conn(rows):
    """Return an in-memory connection pre-loaded with *rows*."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_PEOPLE)
    conn.executemany(
        """INSERT INTO people VALUES (
            :email, :display_name, :first_seen, :last_seen,
            :message_count, :thread_count, :initiated_count,
            :domain, :account, :is_internal, :meeting_count,
            :slack_user_id, :slack_message_count
        )""",
        rows,
    )
    conn.commit()
    return conn


_EXTERNAL = {
    "email": "alice@globalpay.example.com",
    "display_name": "Alice",
    "first_seen": "2024-01-01",
    "last_seen": "2024-06-01",
    "message_count": 10,
    "thread_count": 5,
    "initiated_count": 2,
    "domain": "globalpay.example.com",
    "account": "Global Pay",
    "is_internal": 0,
    "meeting_count": 3,
    "slack_user_id": None,
    "slack_message_count": 0,
}

_INTERNAL = {
    "email": "bob@internal.example.com",  # pii-guard: ignore
    "display_name": "Bob",
    "first_seen": "2023-01-01",
    "last_seen": "2024-06-15",
    "message_count": 50,
    "thread_count": 20,
    "initiated_count": 8,
    "domain": "internal.example.com",  # pii-guard: ignore
    "account": None,
    "is_internal": 1,
    "meeting_count": 10,
    "slack_user_id": "U123456",
    "slack_message_count": 200,
}

_DUPLICATE_A = {
    **_EXTERNAL,
    "email": "charlie-a@globalpay.example.com",
    "display_name": "Charlie",
    "message_count": 30,
}
_DUPLICATE_B = {
    **_EXTERNAL,
    "email": "charlie-b@globalpay.example.com",
    "display_name": "Charlie",
    "message_count": 10,
}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


# ── TestResolveByEmail (flattened) ─────────────────────────────────────────────


def test_known_email_returns_resolved():
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("alice@globalpay.example.com", conn)
    assert result["type"] == "resolved"
    assert result["email"] == "alice@globalpay.example.com"
    assert result["display_name"] == "Alice"
    assert result["account"] == "Global Pay"


def test_unknown_email_returns_not_found():  # pii-guard: ignore
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("nobody@nowhere.example.com", conn)  # pii-guard: ignore
    assert result["type"] == "not_found"
    assert result["candidates"] == []


def test_empty_string_returns_not_found():
    conn = _make_conn([])
    result = cr.resolve_by_email("", conn)
    assert result["type"] == "not_found"


def test_case_insensitive_match():
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("ALICE@GLOBALPAY.EXAMPLE.COM", conn)
    assert result["type"] == "resolved"
    assert result["email"] == "alice@globalpay.example.com"


def test_self_email_falls_back_to_configured_name(monkeypatch):
    self_row = {**_EXTERNAL, "email": "self@example.com", "display_name": ""}
    conn = _make_conn([self_row])
    monkeypatch.setattr(cr, "get_user_email", lambda: "self@example.com")
    monkeypatch.setattr(cr, "get_user_name", lambda: "Alex Morgan")
    result = cr.resolve_by_email("self@example.com", conn)
    assert result["display_name"] == "Alex Morgan"


def test_self_email_case_insensitive_match(monkeypatch):
    self_row = {**_EXTERNAL, "email": "self@example.com", "display_name": ""}
    conn = _make_conn([self_row])
    monkeypatch.setattr(cr, "get_user_email", lambda: "self@example.com")
    monkeypatch.setattr(cr, "get_user_name", lambda: "Alex Morgan")
    result = cr.resolve_by_email("SELF@EXAMPLE.COM", conn)
    assert result["display_name"] == "Alex Morgan"


def test_non_self_empty_display_name_stays_empty(monkeypatch):
    other_row = {**_EXTERNAL, "email": "other@example.com", "display_name": ""}
    conn = _make_conn([other_row])
    monkeypatch.setattr(cr, "get_user_email", lambda: "self@example.com")
    monkeypatch.setattr(cr, "get_user_name", lambda: "Alex Morgan")
    result = cr.resolve_by_email("other@example.com", conn)
    assert result["display_name"] == ""


def test_existing_display_name_not_overwritten(monkeypatch):
    self_row = {**_EXTERNAL, "email": "self@example.com", "display_name": "Existing Name"}
    conn = _make_conn([self_row])
    monkeypatch.setattr(cr, "get_user_email", lambda: "self@example.com")
    monkeypatch.setattr(cr, "get_user_name", lambda: "Alex Morgan")
    result = cr.resolve_by_email("self@example.com", conn)
    assert result["display_name"] == "Existing Name"


# ── TestResolveByName (flattened) ─────────────────────────────────────────────


def test_unique_name_returns_resolved():
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_name("Alice", conn)
    assert result["type"] == "resolved"
    assert result["email"] == "alice@globalpay.example.com"


def test_ambiguous_name_returns_candidates_sorted():
    conn = _make_conn([_DUPLICATE_A, _DUPLICATE_B])
    result = cr.resolve_by_name("Charlie", conn)
    assert result["type"] == "ambiguous"
    candidates = result["candidates"]
    assert len(candidates) == 2
    # Sorted by message_count DESC
    assert candidates[0]["email"] == "charlie-a@globalpay.example.com"
    assert candidates[1]["email"] == "charlie-b@globalpay.example.com"


def test_no_name_match_returns_not_found():
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_name("Zorg", conn)
    assert result["type"] == "not_found"


def test_empty_name_returns_not_found():
    conn = _make_conn([])
    result = cr.resolve_by_name("", conn)
    assert result["type"] == "not_found"


def test_self_name_lookup_falls_back_to_configured_name(monkeypatch):
    self_row = {**_EXTERNAL, "email": "self@example.com", "display_name": ""}
    conn = _make_conn([self_row])
    monkeypatch.setattr(cr, "get_user_email", lambda: "self@example.com")
    monkeypatch.setattr(cr, "get_user_name", lambda: "Alex Morgan")
    result = cr.resolve_by_name("self", conn)
    assert result["type"] == "resolved"
    assert result["display_name"] == "Alex Morgan"


# ── TestSlackFieldExclusion (flattened) ─────────────────────────────────────────────


def test_external_contact_has_no_slack_fields():
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("alice@globalpay.example.com", conn)
    assert "slack_user_id" not in result
    assert "slack_message_count" not in result


def test_internal_contact_has_slack_fields():
    conn = _make_conn([_INTERNAL])
    result = cr.resolve_by_email("bob@internal.example.com", conn)  # pii-guard: ignore
    assert result["slack_user_id"] == "U123456"
    assert result["slack_message_count"] == 200


# ── TestBaseFields (flattened) ─────────────────────────────────────────────

_BASE_base_fields = (
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


def test_all_base_fields_present():
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("alice@globalpay.example.com", conn)
    for field in _BASE_base_fields:
        assert field in result, f"Missing base field: {field}"


# ---------------------------------------------------------------------------
# Signal helper unit tests (pure functions, no DB)
# ---------------------------------------------------------------------------


# ── TestComputeChampionSignal (flattened) ─────────────────────────────────────────────


def test_initiator_at_exact_threshold():
    # 3/20 = 15% — boundary INITIATOR
    assert cr._compute_champion_signal(20, 3) == "INITIATOR"


def test_initiator_above_threshold():
    assert cr._compute_champion_signal(10, 5) == "INITIATOR"


def test_mixed_at_lower_boundary():
    # 1/20 = 5% — boundary MIXED
    assert cr._compute_champion_signal(20, 1) == "MIXED"


def test_mixed_between_boundaries():
    # 2/20 = 10%
    assert cr._compute_champion_signal(20, 2) == "MIXED"


def test_reactive_below_mixed():
    # 0/20 = 0%
    assert cr._compute_champion_signal(20, 0) == "REACTIVE"


def test_none_when_message_count_zero():
    assert cr._compute_champion_signal(0, 0) is None


def test_none_when_message_count_none():
    assert cr._compute_champion_signal(None, 0) is None


# ── TestComputeDecaySignal (flattened) ─────────────────────────────────────────────


def test_gone_at_minus_100():
    assert cr._compute_decay_signal(-100) == "GONE"


def test_gone_below_minus_100():
    assert cr._compute_decay_signal(-150) == "GONE"


def test_decay_at_minus_50():
    assert cr._compute_decay_signal(-50) == "DECAY"


def test_decay_between_boundaries():
    assert cr._compute_decay_signal(-75) == "DECAY"


def test_active_above_minus_50():
    assert cr._compute_decay_signal(-49) == "ACTIVE"


def test_active_at_zero():
    assert cr._compute_decay_signal(0) == "ACTIVE"


def test_active_positive():
    assert cr._compute_decay_signal(20) == "ACTIVE"


def test_none_when_null():
    assert cr._compute_decay_signal(None) is None


# ---------------------------------------------------------------------------
# Signal fields in resolved profile (DB round-trip, no decay_pct column)
# ---------------------------------------------------------------------------


# ── TestSignalFieldsInProfile (flattened) ─────────────────────────────────────────────


def test_champion_signal_present_in_resolved_profile():
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("alice@globalpay.example.com", conn)
    assert "champion_signal" in result


def test_decay_signal_present_in_resolved_profile():
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("alice@globalpay.example.com", conn)
    assert "decay_signal" in result


def test_decay_signal_none_when_column_absent():
    # Schema has no decay_pct — _row_get must return None, decay_signal=None
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("alice@globalpay.example.com", conn)
    assert result["decay_signal"] is None


def test_champion_signal_value_matches_ratio():
    # _EXTERNAL: initiated_count=2, message_count=10 → 20% → INITIATOR
    conn = _make_conn([_EXTERNAL])
    result = cr.resolve_by_email("alice@globalpay.example.com", conn)
    assert result["champion_signal"] == "INITIATOR"


def test_champion_signal_none_when_no_messages():
    row = {**_EXTERNAL, "email": "zero@globalpay.example.com", "message_count": 0, "initiated_count": 0}
    conn = _make_conn([row])
    result = cr.resolve_by_email("zero@globalpay.example.com", conn)
    assert result["champion_signal"] is None


# ---------------------------------------------------------------------------
# Calendar meeting integration tests
# ---------------------------------------------------------------------------

CREATE_CALENDAR_EVENTS = """
CREATE TABLE calendar_events (
    event_id        TEXT PRIMARY KEY,
    summary         TEXT,
    start_time      TEXT,
    end_time        TEXT,
    organizer_email TEXT,
    attendees       TEXT   -- JSON array of {"email": "..."} objects
);
"""

_MEETING_A = {
    "event_id": "evt-001",
    "summary": "Kick-off",
    "start_time": "2024-06-01T10:00:00",
    "end_time": "2024-06-01T11:00:00",
    "organizer_email": "alice@globalpay.example.com",
    "attendees": '[{"email": "bob@internal.example.com"}]',  # pii-guard: ignore
}
_MEETING_B = {
    "event_id": "evt-002",
    "summary": "Quarterly Review",
    "start_time": "2024-05-15T14:00:00",
    "end_time": "2024-05-15T15:00:00",
    "organizer_email": "carol@globalpay.example.com",
    "attendees": '[{"email": "alice@globalpay.example.com"}, {"email": "bob@internal.example.com"}]',  # pii-guard: ignore
}
_MEETING_C = {
    "event_id": "evt-003",
    "summary": "Unrelated",
    "start_time": "2024-04-01T09:00:00",
    "end_time": "2024-04-01T09:30:00",
    "organizer_email": "other@other.example.com",
    "attendees": '[{"email": "nobody@nowhere.example.com"}]',  # pii-guard: ignore
}


def _make_conn_with_calendar(people_rows, meeting_rows):
    """Return an in-memory connection with people and calendar_events tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_PEOPLE)
    conn.execute(CREATE_CALENDAR_EVENTS)
    conn.executemany(
        """INSERT INTO people VALUES (
            :email, :display_name, :first_seen, :last_seen,
            :message_count, :thread_count, :initiated_count,
            :domain, :account, :is_internal, :meeting_count,
            :slack_user_id, :slack_message_count
        )""",
        people_rows,
    )
    conn.executemany(
        """INSERT INTO calendar_events VALUES (
            :event_id, :summary, :start_time, :end_time,
            :organizer_email, :attendees
        )""",
        meeting_rows,
    )
    conn.commit()
    return conn


# ── TestRecentMeetings (flattened) ─────────────────────────────────────────────


def test_contact_as_attendee_returns_meetings():
    conn = _make_conn_with_calendar([_EXTERNAL], [_MEETING_A, _MEETING_B, _MEETING_C])
    meetings = cr.get_recent_meetings("alice@globalpay.example.com", conn)
    event_ids = {m["event_id"] for m in meetings}
    # alice is organizer of evt-001 and attendee of evt-002
    assert "evt-001" in event_ids
    assert "evt-002" in event_ids
    assert "evt-003" not in event_ids


def test_contact_as_organizer_returns_meetings():
    conn = _make_conn_with_calendar([_EXTERNAL], [_MEETING_A])
    meetings = cr.get_recent_meetings("alice@globalpay.example.com", conn)
    assert len(meetings) == 1
    assert meetings[0]["event_id"] == "evt-001"  # pii-guard: ignore


def test_contact_in_neither_role_returns_empty():
    conn = _make_conn_with_calendar([_EXTERNAL], [_MEETING_C])
    meetings = cr.get_recent_meetings("alice@globalpay.example.com", conn)
    assert meetings == []


def test_limit_parameter_respected():
    # Insert 3 meetings alice is involved in, request limit=2
    meetings_data = [
        _MEETING_A,
        _MEETING_B,
        {
            "event_id": "evt-004",
            "summary": "Third meeting",
            "start_time": "2024-03-01T09:00:00",
            "end_time": "2024-03-01T10:00:00",
            "organizer_email": "alice@globalpay.example.com",
            "attendees": "[]",
        },
    ]
    conn = _make_conn_with_calendar([_EXTERNAL], meetings_data)
    meetings = cr.get_recent_meetings("alice@globalpay.example.com", conn, limit=2)
    assert len(meetings) == 2


def test_missing_calendar_events_table_returns_empty():
    # Use _make_conn which does NOT create calendar_events
    conn = _make_conn([_EXTERNAL])
    meetings = cr.get_recent_meetings("alice@globalpay.example.com", conn)
    assert meetings == []


def test_not_found_result_has_no_recent_meetings_key():
    conn = _make_conn_with_calendar([_EXTERNAL], [])
    result = cr.resolve_by_email("nobody@nowhere.example.com", conn)  # pii-guard: ignore
    assert result["type"] == "not_found"
    assert "recent_meetings" not in result


# ---------------------------------------------------------------------------
# get_recent_threads tests
# ---------------------------------------------------------------------------

CREATE_MESSAGES = """
CREATE TABLE messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT,
    subject     TEXT,
    snippet     TEXT,
    date_str    TEXT,
    date_epoch  INTEGER,
    from_addr   TEXT,
    to_addr     TEXT,
    cc_addr     TEXT,
    labels      TEXT DEFAULT '[]'
);
"""


def _make_conn_with_messages(people_rows, message_rows):
    """Return an in-memory connection with people and messages tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_PEOPLE)
    conn.execute(CREATE_MESSAGES)
    conn.executemany(
        """INSERT INTO people VALUES (
            :email, :display_name, :first_seen, :last_seen,
            :message_count, :thread_count, :initiated_count,
            :domain, :account, :is_internal, :meeting_count,
            :slack_user_id, :slack_message_count
        )""",
        people_rows,
    )
    for m in message_rows:
        conn.execute(
            """INSERT INTO messages
               (message_id, thread_id, subject, snippet, date_str, date_epoch,
                from_addr, to_addr, cc_addr)
               VALUES (:message_id, :thread_id, :subject, :snippet,
                       :date_str, :date_epoch, :from_addr, :to_addr, :cc_addr)""",
            m,
        )
    conn.commit()
    return conn


_MSG_BASE = {
    "message_id": "msg-001",
    "thread_id": "t-001",
    "subject": "OpenShift upgrade plan",
    "snippet": "Let's discuss the upgrade path",
    "date_str": "2024-06-01",
    "date_epoch": 1717200000,
    "from_addr": "alice@globalpay.example.com",
    "to_addr": "matt@internal.example.com",  # pii-guard: ignore
    "cc_addr": "",
}

_MSG_REPLY = {
    **_MSG_BASE,
    "message_id": "msg-002",
    "thread_id": "t-001",
    "subject": "Re: OpenShift upgrade plan",
    "date_epoch": 1717300000,
    "from_addr": "matt@internal.example.com",  # pii-guard: ignore
    "to_addr": "alice@globalpay.example.com",
}

_MSG_DIFFERENT = {
    **_MSG_BASE,
    "message_id": "msg-003",
    "thread_id": "t-002",
    "subject": "Budget approval",
    "snippet": "Budget is approved",
    "date_epoch": 1717400000,
}

_MSG_OOO = {
    **_MSG_BASE,
    "message_id": "msg-004",
    "thread_id": "t-003",
    "subject": "Out of office: OpenShift",
    "snippet": "I am out of the office",
    "date_epoch": 1717500000,
}


# ── TestGetRecentThreads (flattened) ─────────────────────────────────────────────


def test_returns_threads_by_from_addr():
    conn = _make_conn_with_messages([], [_MSG_BASE])
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    assert len(threads) == 1
    assert threads[0]["subject"] == "OpenShift upgrade plan"


def test_returns_threads_by_to_addr():
    conn = _make_conn_with_messages([], [_MSG_REPLY])
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    assert len(threads) == 1


def test_returns_threads_by_cc_addr():
    msg = {**_MSG_BASE, "from_addr": "other@other.example.com", "cc_addr": "alice@globalpay.example.com"}
    conn = _make_conn_with_messages([], [msg])
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    assert len(threads) == 1


def test_deduplicates_re_prefix():
    """Re: reply should be deduped with the original subject."""
    conn = _make_conn_with_messages([], [_MSG_BASE, _MSG_REPLY])
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    # Both have stem "openshift upgrade plan" — only one returned  # pii-guard: ignore
    assert len(threads) == 1


def test_deduplicates_fwd_prefix():
    fwd = {**_MSG_BASE, "message_id": "msg-fwd", "subject": "Fwd: OpenShift upgrade plan", "date_epoch": 1717100000}
    conn = _make_conn_with_messages([], [_MSG_BASE, fwd])
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    assert len(threads) == 1


def test_multiple_distinct_subjects():
    conn = _make_conn_with_messages([], [_MSG_BASE, _MSG_DIFFERENT])
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    assert len(threads) == 2


def test_filters_ooo_messages():
    conn = _make_conn_with_messages([], [_MSG_OOO])
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    assert len(threads) == 0


def test_limit_parameter():
    msgs = [
        {
            **_MSG_BASE,
            "message_id": f"msg-{i}",
            "thread_id": f"t-{i}",
            "subject": f"Topic {i}",
            "date_epoch": 1717200000 + i * 1000,
        }
        for i in range(10)
    ]
    conn = _make_conn_with_messages([], msgs)
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn, limit=3)
    assert len(threads) == 3


def test_returns_empty_when_no_messages_table():
    conn = _make_conn([_EXTERNAL])  # No messages table
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    assert threads == []


def test_returns_empty_for_unknown_contact():
    conn = _make_conn_with_messages([], [_MSG_BASE])
    threads = cr.get_recent_threads("nobody@nowhere.example.com", conn)  # pii-guard: ignore
    assert threads == []


def test_snippet_truncated_to_200_chars():
    long_snippet = "x" * 500
    msg = {**_MSG_BASE, "snippet": long_snippet}
    conn = _make_conn_with_messages([], [msg])
    threads = cr.get_recent_threads("alice@globalpay.example.com", conn)
    assert len(threads[0]["snippet"]) <= 200


# ---------------------------------------------------------------------------
# resolve() routing tests
# ---------------------------------------------------------------------------


# ── TestResolve (flattened) ─────────────────────────────────────────────


def test_resolve_with_email_routes_to_email_resolver(tmp_path: Path):
    # Create a real DB file for resolve() to connect to
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(CREATE_PEOPLE)
    conn.executemany(
        """INSERT INTO people VALUES (
            :email, :display_name, :first_seen, :last_seen,
            :message_count, :thread_count, :initiated_count,
            :domain, :account, :is_internal, :meeting_count,
            :slack_user_id, :slack_message_count
        )""",
        [_EXTERNAL],
    )
    conn.commit()
    conn.close()

    result = cr.resolve("alice@globalpay.example.com", db_path)
    assert result["type"] == "resolved"
    assert result["email"] == "alice@globalpay.example.com"


def test_resolve_with_name_routes_to_name_resolver(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(CREATE_PEOPLE)
    conn.executemany(
        """INSERT INTO people VALUES (
            :email, :display_name, :first_seen, :last_seen,
            :message_count, :thread_count, :initiated_count,
            :domain, :account, :is_internal, :meeting_count,
            :slack_user_id, :slack_message_count
        )""",
        [_EXTERNAL],
    )
    conn.commit()
    conn.close()

    result = cr.resolve("Alice", db_path)
    assert result["type"] == "resolved"


def test_resolve_none_returns_not_found():
    result = cr.resolve(None)
    assert result["type"] == "not_found"


def test_resolve_empty_string_returns_not_found():
    result = cr.resolve("")
    assert result["type"] == "not_found"


def test_resolve_whitespace_returns_not_found():
    result = cr.resolve("   ")
    assert result["type"] == "not_found"


# ---------------------------------------------------------------------------
# connect() tests
# ---------------------------------------------------------------------------


# ── TestConnect (flattened) ─────────────────────────────────────────────


def test_connect_returns_connection(tmp_path: Path):
    db_path = tmp_path / "test.db"
    # Create a minimal DB file
    sqlite3.connect(str(db_path)).close()
    conn = cr.connect(db_path)
    assert conn is not None
    conn.close()


def test_connect_enables_row_factory(tmp_path: Path):
    db_path = tmp_path / "test.db"
    sqlite3.connect(str(db_path)).close()
    conn = cr.connect(db_path)
    assert conn.row_factory is sqlite3.Row
    conn.close()


# ---------------------------------------------------------------------------
# build_profile — None field guards (historic regression)
# ---------------------------------------------------------------------------


# ── TestBuildProfileNoneGuards (flattened) ─────────────────────────────────────────────


def _make_row_with_nulls_build_profile_none_guards():
    """Return a sqlite3.Row where optional fields are NULL."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE people (
            email TEXT, display_name TEXT, first_seen TEXT, last_seen TEXT,
            message_count INTEGER, thread_count INTEGER, initiated_count INTEGER,
            domain TEXT, account TEXT, is_internal INTEGER, meeting_count INTEGER,
            slack_user_id TEXT, slack_message_count INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO people VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("user@example.com", None, None, None, None, None, None, None, None, 0, None, None, None),  # pii-guard: ignore
    )
    conn.commit()
    return conn.execute("SELECT * FROM people WHERE email = 'user@example.com'").fetchone(), conn  # pii-guard: ignore


def test_build_profile_no_crash_on_none_fields():
    row, conn = _make_row_with_nulls_build_profile_none_guards()
    profile = cr.build_profile(row, conn)
    assert isinstance(profile, dict)
    # String fields default to ''
    assert profile["display_name"] == ""
    assert profile["first_seen"] == ""
    assert profile["last_seen"] == ""
    assert profile["domain"] == ""
    assert profile["account"] == ""
    # Int fields default to 0
    assert profile["message_count"] == 0
    assert profile["thread_count"] == 0
    assert profile["initiated_count"] == 0
    assert profile["meeting_count"] == 0
    conn.close()


def test_build_profile_champion_signal_safe_on_zero_counts():
    row, conn = _make_row_with_nulls_build_profile_none_guards()
    profile = cr.build_profile(row, conn)
    # With message_count=0, champion_signal should be None (not crash)
    assert profile["champion_signal"] is None
    conn.close()
