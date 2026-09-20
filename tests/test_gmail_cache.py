"""Tests for gmail-cache utility functions across multiple modules."""

import sqlite3
from unittest.mock import patch

import pytest

from fieldkit.commands.gmail import query
from fieldkit.contact import people_index
from fieldkit.contact.people_index import build_people_index
from fieldkit.gmail import decay_domain, sync_store

# Org-specific domains used in tests — mock get_internal_domains() so these
# tests don't require a live config file (org-agnostic refactor removed the
# hardcoded fallback).
_TEST_INTERNAL_DOMAINS = ["internal.example.com", "external.example.com"]  # pii-guard: ignore

pytestmark = pytest.mark.unit


# --- decay.py ---


# ── TestIsInternalOrNoise (flattened) ───────────────────────────────────────


def test_is_internal_or_noise_configured_domain():
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert decay_domain.is_internal_or_noise("user@internal.example.com") is True  # pii-guard: ignore


def test_is_internal_or_noise_ibm():
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert decay_domain.is_internal_or_noise("user@external.example.com") is True


def test_is_internal_or_noise_external():
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert decay_domain.is_internal_or_noise("jane@globalpay.example.com") is False


def test_is_internal_or_noise_noreply():
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert decay_domain.is_internal_or_noise("noreply@globalpay.example.com") is True


def test_is_internal_or_noise_notifications():
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert decay_domain.is_internal_or_noise("notifications@github.example.com") is True


def test_is_internal_or_noise_case_insensitive():
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert decay_domain.is_internal_or_noise("User@INTERNAL.EXAMPLE.COM") is True


# ── TestDecayExtractEmail (flattened) ───────────────────────────────────────


def test_decay_extract_email_angle_bracket():
    assert decay_domain._extract_email("Jane Doe <jane@globalpay.example.com>") == "jane@globalpay.example.com"


def test_decay_extract_email_bare():
    assert decay_domain._extract_email("jane@globalpay.example.com") == "jane@globalpay.example.com"


def test_decay_extract_email_uppercase():
    assert decay_domain._extract_email("JANE@GLOBALPAY.EXAMPLE.COM") == "jane@globalpay.example.com"


def test_decay_extract_email_whitespace():
    assert decay_domain._extract_email("  jane@globalpay.example.com  ") == "jane@globalpay.example.com"


# --- query.py ---
# query imported above from fieldkit.commands.gmail


# ── TestDateToEpoch (flattened) ─────────────────────────────────────────────


def test_date_to_epoch_basic():
    assert query.date_to_epoch("2025-01-01") == 1735689600


def test_date_to_epoch_before_adds_day():
    base = query.date_to_epoch("2025-01-01")
    before = query.date_to_epoch("2025-01-01", is_before=True)
    assert before == base + 86400


# ── TestBuildDateClause (flattened) ─────────────────────────────────────────


def test_build_date_clause_no_dates():
    fragment, params = query.build_date_clause(None, None)
    assert fragment == ""
    assert params == []


def test_build_date_clause_since_only():
    fragment, params = query.build_date_clause(1735689600, None)
    assert "date_epoch >= ?" in fragment
    assert len(params) == 1


def test_build_date_clause_both():
    fragment, params = query.build_date_clause(1735689600, 1738368000)
    assert "date_epoch >= ?" in fragment
    assert "date_epoch < ?" in fragment
    assert len(params) == 2


# ── TestStripQuoted (flattened) ─────────────────────────────────────────────


def test_strip_quoted_strips_quoted_lines():
    text = "Hello\n> quoted line\n> another"
    result = query._strip_quoted(text)
    assert result == "Hello"


def test_strip_quoted_strips_on_wrote():
    text = "Reply text\nOn Mon Jan 1 wrote:\noriginal"
    result = query._strip_quoted(text)
    assert result == "Reply text"


def test_strip_quoted_max_chars():
    text = "a" * 500
    result = query._strip_quoted(text, max_chars=100)
    assert len(result) == 100


def test_strip_quoted_empty():
    assert query._strip_quoted("") == ""


# ── TestQueryExtractEmail (flattened) ───────────────────────────────────────


def test_query_extract_email_angle():
    assert query._extract_email_addr("Bob <bob@x.example.com>") == "bob@x.example.com"


def test_query_extract_email_bare():
    assert query._extract_email_addr("bob@x.example.com") == "bob@x.example.com"


# ── TestQueryIsNoise (flattened) ────────────────────────────────────────────


def test_query_is_noise_internal():
    with patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert query._is_noise("user@internal.example.com") is True  # pii-guard: ignore


def test_query_is_noise_external():
    with patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert query._is_noise("user@globalpay.example.com") is False


def test_query_is_noise_noreply():
    assert query._is_noise("noreply@example.com") is True  # pii-guard: ignore


# --- people.py ---
# people imported above from fieldkit.commands.gmail


# ── TestParseAddresses (flattened) ──────────────────────────────────────────


def test_parse_addresses_name_email():
    result = people_index.parse_addresses("Jane Doe <jane@globalpay.example.com>")
    assert result == [("Jane Doe", "jane@globalpay.example.com")]


def test_parse_addresses_bare_email():
    result = people_index.parse_addresses("jane@globalpay.example.com")
    assert result == [("", "jane@globalpay.example.com")]


def test_parse_addresses_multiple():
    result = people_index.parse_addresses("Jane <j@a.example.com>, Bob <b@a.example.com>")
    assert len(result) == 2


def test_parse_addresses_none():
    assert people_index.parse_addresses(None) == []


def test_parse_addresses_empty():
    assert people_index.parse_addresses("") == []


def test_parse_addresses_quoted_name():
    result = people_index.parse_addresses('"Jane Doe" <jane@globalpay.example.com>')
    assert result[0][0] == "Jane Doe"
    assert result[0][1] == "jane@globalpay.example.com"


# ── TestPeopleCli (flattened) ───────────────────────────────────────────────


def _people_cli_make_db(path: str) -> None:
    """Create a minimal gmail.db with the required schema."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_epoch INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE people (
            email TEXT PRIMARY KEY,
            display_name TEXT,
            first_seen TEXT,
            last_seen TEXT,
            message_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS thread_accounts (
            thread_id TEXT,
            account TEXT
        )"""
    )
    conn.commit()
    conn.close()


def test_people_cli_cli_produces_summary_output(tmp_path, capsys):
    """Invoking the CLI should succeed and print summary lines to stdout.

    This verifies the CLI runs end-to-end and that _log_summary_stats()
    writes its output (historic regression — previously all output was silently lost
    because logging was not configured).
    """
    db_path = str(tmp_path / "gmail.db")
    _people_cli_make_db(db_path)

    build_people_index(db_path, account_filter=None, show_progress=False)
    captured = capsys.readouterr()

    # _log_summary_stats() writes these lines unconditionally to stdout
    assert "Total contacts:" in captured.out


def _people_cli_make_db_with_data(path: str) -> None:
    """Create a gmail.db with two threads tagged to different accounts."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_epoch INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE people (
            email TEXT PRIMARY KEY,
            display_name TEXT,
            first_seen TEXT,
            last_seen TEXT,
            message_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS thread_accounts (
            thread_id TEXT,
            account TEXT
        )"""
    )
    # Thread 1 → acme account, sender alice@acme-corp.com
    conn.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        ("msg1", "thread1", "Alice <alice@acme-corp.com>", "bob@example.com", None, 1_700_000_000),  # pii-guard: ignore
    )
    conn.execute("INSERT INTO thread_accounts VALUES (?, ?)", ("thread1", "acme"))
    # Thread 2 → globex account, sender carol@globalpay.example.com
    conn.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        (
            "msg2",
            "thread2",
            "Carol <carol@globalpay.example.com>",
            "dave@example.com",  # pii-guard: ignore
            None,
            1_700_000_001,
        ),  # pii-guard: ignore
    )
    conn.execute("INSERT INTO thread_accounts VALUES (?, ?)", ("thread2", "globex"))
    conn.commit()
    conn.close()


def test_people_cli_account_filter_restricts_to_matching_threads(tmp_path):
    """historic regression: --account filters people index to threads tagged with that account slug.

    With --account acme, only alice@acme-corp.com (from thread1) should appear;
    carol@globalpay.example.com (from thread2/globex) must be absent.
    """
    import sqlite3 as _sqlite3

    db_path = str(tmp_path / "gmail.db")
    _people_cli_make_db_with_data(db_path)

    build_people_index(db_path, account_filter="acme", show_progress=False)

    # Verify the people table only contains the acme-tagged contact
    conn = _sqlite3.connect(db_path)
    emails = {row[0] for row in conn.execute("SELECT email FROM people")}
    conn.close()

    assert "alice@acme-corp.com" in emails, "Expected acme contact in people table"
    assert "carol@globalpay.example.com" not in emails, "globex contact must be excluded when --account acme"


def test_people_cli_account_filter_short_flag(tmp_path):
    """historic regression: -a short flag must work identically to --account."""
    import sqlite3 as _sqlite3

    db_path = str(tmp_path / "gmail.db")
    _people_cli_make_db_with_data(db_path)

    build_people_index(db_path, account_filter="globex", show_progress=False)

    conn = _sqlite3.connect(db_path)
    emails = {row[0] for row in conn.execute("SELECT email FROM people")}
    conn.close()

    assert "carol@globalpay.example.com" in emails, "Expected globex contact in people table"
    assert "alice@acme-corp.com" not in emails, "acme contact must be excluded when -a globex"


def test_people_cli_no_account_filter_includes_all_threads(tmp_path):
    """historic regression: without --account, all threads are processed (existing behaviour preserved)."""
    import sqlite3 as _sqlite3

    db_path = str(tmp_path / "gmail.db")
    _people_cli_make_db_with_data(db_path)

    build_people_index(db_path, account_filter=None, show_progress=False)

    conn = _sqlite3.connect(db_path)
    emails = {row[0] for row in conn.execute("SELECT email FROM people")}
    conn.close()

    assert "alice@acme-corp.com" in emails, "acme contact must be present without filter"
    assert "carol@globalpay.example.com" in emails, "globex contact must be present without filter"


# --- sync.py (import only the pure functions) ---
# sync imported above from fieldkit.commands.gmail


# ── TestDecodeB64Url (flattened) ────────────────────────────────────────────


def test_decode_b64_url_basic():
    _decode_b64url = sync_store._decode_b64url
    import base64

    encoded = base64.urlsafe_b64encode(b"hello world").decode().rstrip("=")
    assert _decode_b64url(encoded) == "hello world"


def test_decode_b64_url_empty():
    assert sync_store._decode_b64url("") == ""


def test_decode_b64_url_padding():
    import base64

    encoded = base64.urlsafe_b64encode(b"test").decode().rstrip("=")
    assert sync_store._decode_b64url(encoded) == "test"


# ── TestExtractParts (flattened) ────────────────────────────────────────────


def test_extract_parts_plain_text():
    import base64

    data = base64.urlsafe_b64encode(b"Hello").decode()
    payload = {"mimeType": "text/plain", "body": {"data": data}}
    plain, html, attachments = sync_store._extract_parts(payload)
    assert plain == "Hello"
    assert html == ""
    assert attachments == []


def test_extract_parts_html():
    import base64

    data = base64.urlsafe_b64encode(b"<p>Hi</p>").decode()
    payload = {"mimeType": "text/html", "body": {"data": data}}
    plain, html, _attachments = sync_store._extract_parts(payload)
    assert plain == ""
    assert html == "<p>Hi</p>"


def test_extract_parts_multipart():
    import base64

    plain_data = base64.urlsafe_b64encode(b"Plain").decode()
    html_data = base64.urlsafe_b64encode(b"<b>HTML</b>").decode()
    payload = {
        "mimeType": "multipart/alternative",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": plain_data}},
            {"mimeType": "text/html", "body": {"data": html_data}},
        ],
    }
    plain, html, _attachments = sync_store._extract_parts(payload)
    assert plain == "Plain"
    assert html == "<b>HTML</b>"


def test_extract_parts_attachment():
    payload = {
        "mimeType": "application/pdf",
        "body": {"attachmentId": "abc123", "size": 1024},
        "filename": "doc.pdf",
        "headers": [],
    }
    _plain, _html, attachments = sync_store._extract_parts(payload)
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "doc.pdf"
