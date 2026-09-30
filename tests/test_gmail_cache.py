"""Tests for gmail-cache utility functions across multiple modules."""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.gmail.query import _strip_quoted
from fieldkit.contact.people_index import build_people_index
from fieldkit.gmail import query_domain as query
from fieldkit.gmail import sync_store
from fieldkit.gmail.addresses import parse_address_header
from fieldkit.gmail.exceptions import GmailSchemaError
from fieldkit.gmail.publication import (
    GMAIL_QUERY_READY_KEY,
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection

# Org-specific domains used in tests — mock get_internal_domains() so these
# tests don't require a live config file (org-agnostic refactor removed the
# hardcoded fallback).
_TEST_INTERNAL_DOMAINS = ["internal.example.com", "external.example.com"]  # pii-guard: ignore

pytestmark = pytest.mark.unit


# --- query domain ---
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
    result = _strip_quoted(text)
    assert result == "Hello"


def test_strip_quoted_strips_on_wrote():
    text = "Reply text\nOn Mon Jan 1 wrote:\noriginal"
    result = _strip_quoted(text)
    assert result == "Reply text"


def test_strip_quoted_max_chars():
    text = "a" * 500
    result = _strip_quoted(text, max_chars=100)
    assert len(result) == 100


def test_strip_quoted_empty():
    assert _strip_quoted("") == ""


# ── TestQueryIsNoise (flattened) ────────────────────────────────────────────


def test_query_is_noise_internal():
    with patch("fieldkit.gmail.query_domain.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert query._is_noise("user@internal.example.com") is True  # pii-guard: ignore


def test_query_is_noise_external():
    with patch("fieldkit.gmail.query_domain.get_internal_domains", return_value=_TEST_INTERNAL_DOMAINS):
        assert query._is_noise("user@globalpay.example.com") is False


def test_query_is_noise_noreply():
    assert query._is_noise("noreply@example.com") is True  # pii-guard: ignore


# --- people.py ---
# people imported above from fieldkit.commands.gmail


# ── TestParseAddresses (flattened) ──────────────────────────────────────────


def test_parse_addresses_name_email():
    result = parse_address_header("Jane Doe <jane@globalpay.example.com>", field="message address")
    assert result == (("Jane Doe", "jane@globalpay.example.com"),)


def test_parse_addresses_bare_email():
    result = parse_address_header("jane@globalpay.example.com", field="message address")
    assert result == (("", "jane@globalpay.example.com"),)


def test_parse_addresses_multiple():
    result = parse_address_header("Jane <j@a.example.com>, Bob <b@a.example.com>", field="message address")
    assert len(result) == 2


def test_parse_addresses_none():
    assert parse_address_header(None, field="message address") == ()


def test_parse_addresses_empty():
    assert parse_address_header("", field="message address") == ()


def test_parse_addresses_quoted_name():
    result = parse_address_header('"Jane Doe" <jane@globalpay.example.com>', field="message address")
    assert result[0][0] == "Jane Doe"
    assert result[0][1] == "jane@globalpay.example.com"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("alice@example.com (Alice)", (("Alice", "alice@example.com"),)),
        (
            "Group: alice@example.com, bob@example.com;",
            (("", "alice@example.com"), ("", "bob@example.com")),
        ),
        ('"Doe, Jane" <Jane@Example.COM>', (("Doe, Jane", "jane@example.com"),)),
    ],
)
def test_parse_addresses_accepts_supported_rfc_forms(
    header: str,
    expected: tuple[tuple[str, str], ...],
) -> None:
    assert parse_address_header(header, field="message address") == expected


@pytest.mark.parametrize("header", ["Group:;", "Undisclosed recipients:;", "Group: ;"])
def test_parse_addresses_accepts_addressless_rfc_groups(header: str) -> None:
    assert parse_address_header(header, field="message address") == ()


@pytest.mark.parametrize(
    "header",
    [
        "bad <not-address>",
        "a@b@example.com",
        "name <a b@example.com>",
        "a@example.com garbage",
        "a@.example.com",
        "a@example..com",
        "a@example.com.",
        ".a@example.com",
        "a..b@example.com",
        "a.@example.com",
        "Alice <alice@example.com",
    ],
)
def test_parse_addresses_rejects_malformed_mailboxes_without_payload(header: str) -> None:
    with pytest.raises(GmailSchemaError, match="invalid message address data") as captured:
        parse_address_header(header, field="message address")

    assert header not in str(captured.value)


# ── TestPeopleCli (flattened) ───────────────────────────────────────────────


def _people_cli_make_db(path: str) -> None:
    """Create an empty ready published Gmail cache."""
    source = Path(path)
    initialize_gmail_publication(source)

    def mark_ready(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(source, mark_ready)


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
    """Create a ready published Gmail cache with two account-tagged threads."""
    source = Path(path)
    initialize_gmail_publication(source)

    def publish(connection: SQLiteMutationConnection) -> None:
        connection.executemany(
            "INSERT INTO threads(thread_id, subject, message_count, updated_at) VALUES (?, '', 1, '')",
            (("thread1",), ("thread2",)),
        )
        connection.executemany(
            """
            INSERT INTO messages(message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "msg1",
                    "thread1",
                    "Alice <alice@acme-corp.example.com>",
                    "bob@example.com",  # pii-guard: ignore
                    None,
                    1_700_000_000,
                ),
                (
                    "msg2",
                    "thread2",
                    "Carol <carol@globalpay.example.com>",
                    "dave@example.com",  # pii-guard: ignore
                    None,
                    1_700_000_001,
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO thread_accounts(thread_id, account) VALUES (?, ?)",
            (("thread1", "acme"), ("thread2", "globex")),
        )
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(source, publish)


def test_people_cli_account_filter_reconciles_complete_index(tmp_path):
    """An account-triggered rebuild retains contacts shared with other accounts."""
    db_path = str(tmp_path / "gmail.db")
    _people_cli_make_db_with_data(db_path)

    build_people_index(db_path, account_filter="acme", show_progress=False)

    with open_gmail_publication(Path(db_path)) as conn:
        emails = {row[0] for row in conn.execute("SELECT email FROM people")}

    assert "alice@acme-corp.example.com" in emails, "Expected acme contact in people table"
    assert "carol@globalpay.example.com" in emails, "Complete index must preserve the other account"


def test_people_cli_account_filter_short_flag_reconciles_complete_index(tmp_path):
    """The short account flag triggers the same complete reconciliation."""
    db_path = str(tmp_path / "gmail.db")
    _people_cli_make_db_with_data(db_path)

    build_people_index(db_path, account_filter="globex", show_progress=False)

    with open_gmail_publication(Path(db_path)) as conn:
        emails = {row[0] for row in conn.execute("SELECT email FROM people")}

    assert "carol@globalpay.example.com" in emails, "Expected globex contact in people table"
    assert "alice@acme-corp.example.com" in emails, "Complete index must preserve the other account"


def test_people_cli_no_account_filter_includes_all_threads(tmp_path):
    """historic regression: without --account, all threads are processed (existing behaviour preserved)."""
    db_path = str(tmp_path / "gmail.db")
    _people_cli_make_db_with_data(db_path)

    build_people_index(db_path, account_filter=None, show_progress=False)

    with open_gmail_publication(Path(db_path)) as conn:
        emails = {row[0] for row in conn.execute("SELECT email FROM people")}

    assert "alice@acme-corp.example.com" in emails, "acme contact must be present without filter"
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
