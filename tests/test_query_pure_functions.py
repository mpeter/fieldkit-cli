"""Unit tests for the pure query functions added to src/fieldkit/commands/gmail/query.py.

Tests query_by_email(), query_blindspots(), and query_dig() using an
in-memory SQLite database populated from schema.sql fixtures.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.gmail.query import query_blindspots, query_by_email, query_dig

pytestmark = pytest.mark.unit

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "fieldkit" / "gmail" / "schema.sql"


@pytest.fixture
def db() -> sqlite3.Connection:
    """In-memory SQLite DB with schema and realistic fixture data."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())

    # Two accounts
    conn.executemany(
        "INSERT INTO threads (thread_id, subject, message_count, updated_at) VALUES (?, ?, ?, ?)",
        [
            ("t-acme-1", "Renewal discussion", 3, "2024-03-01"),
            ("t-acme-2", "Training proposal", 2, "2024-02-15"),
            ("t-other-1", "Unrelated thread", 1, "2024-01-10"),
        ],
    )

    conn.executemany(
        "INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)",
        [
            ("t-acme-1", "acme"),
            ("t-acme-2", "acme"),
            ("t-other-1", "other"),
        ],
    )

    conn.executemany(
        "INSERT INTO messages (message_id, thread_id, from_addr, to_addr, cc_addr, subject, date_str, date_epoch, body_plain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # t-acme-1: external contact alice sends, then internal reply
            (
                "m-a1-1",
                "t-acme-1",
                "alice@acme-com.example.com",
                "rep@internal.example.com",  # pii-guard: ignore
                "",
                "Renewal discussion",
                "2024-03-01T10:00:00",
                1_709_287_200,
                "Hi, let's talk renewal",
            ),
            (
                "m-a1-2",
                "t-acme-1",
                "rep@internal.example.com",  # pii-guard: ignore
                "alice@acme-com.example.com",
                "bob@acme-com.example.com",
                "Renewal discussion",
                "2024-03-01T11:00:00",
                1_709_290_800,
                "Sure, here's the proposal",
            ),
            (
                "m-a1-3",
                "t-acme-1",
                "alice@acme-com.example.com",
                "rep@internal.example.com",  # pii-guard: ignore
                "",
                "Renewal discussion",
                "2024-03-01T12:00:00",
                1_709_294_400,
                "renewal confirmed",
            ),
            # t-acme-2: training keyword in subject
            (
                "m-a2-1",
                "t-acme-2",
                "carol@acme-com.example.com",
                "rep@internal.example.com",  # pii-guard: ignore
                "",
                "Training proposal",
                "2024-02-15T09:00:00",
                1_707_984_000,
                "Can we schedule training?",
            ),
            (
                "m-a2-2",
                "t-acme-2",
                "rep@internal.example.com",  # pii-guard: ignore
                "carol@acme-com.example.com",
                "",
                "Training proposal",
                "2024-02-15T10:00:00",
                1_707_987_600,
                "Yes, training is available",
            ),
            # t-other-1: different account
            (
                "m-o1-1",
                "t-other-1",
                "dave@other-com.example.com",
                "rep@internal.example.com",  # pii-guard: ignore
                "",
                "Unrelated thread",
                "2024-01-10T08:00:00",
                1_704_873_600,
                "hello",
            ),
        ],
    )

    conn.executemany(
        "INSERT INTO people (email, display_name, message_count) VALUES (?, ?, ?)",
        [
            ("alice@acme-com.example.com", "Alice Smith", 2),
            ("bob@acme-com.example.com", "Bob Jones", 0),
            ("carol@acme-com.example.com", "Carol White", 1),
            ("rep@internal.example.com", "Internal Rep", 3),  # pii-guard: ignore
        ],
    )

    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# query_by_email
# ---------------------------------------------------------------------------


# ── TestQueryByEmail (flattened) ────────────────────────────────────────────


def test_query_by_email_found_in_from_addr(db: sqlite3.Connection) -> None:
    count, last = query_by_email(db, "alice@acme-com.example.com")
    assert count >= 1
    assert last is not None
    assert len(last) == 10  # YYYY-MM-DD


def test_query_by_email_found_in_cc_addr(db: sqlite3.Connection) -> None:
    # bob@acme-com.example.com appears only in cc_addr of m-a1-2
    count, _last = query_by_email(db, "bob@acme-com.example.com")
    assert count >= 1


def test_query_by_email_no_match_returns_zero_and_none(db: sqlite3.Connection) -> None:
    count, last = query_by_email(db, "nobody@nowhere-com.example.com")  # pii-guard: ignore
    assert count == 0
    assert last is None


def test_query_by_email_returns_date_iso_format(db: sqlite3.Connection) -> None:
    count, last = query_by_email(db, "carol@acme-com.example.com")
    assert count >= 1
    assert last is not None
    # Should be YYYY-MM-DD (10 chars)
    assert last.count("-") == 2


def test_query_by_email_partial_email_match(db: sqlite3.Connection) -> None:
    # LIKE pattern — prefix match
    count, _last = query_by_email(db, "alice@")
    assert count >= 1


# ---------------------------------------------------------------------------
# query_blindspots
# ---------------------------------------------------------------------------


# ── TestQueryBlindspots (flattened) ─────────────────────────────────────────


def _query_blindspots_patch_noise():
    """Patch _is_noise so only the fixture's internal address is filtered."""

    def _fake_is_noise(email: str) -> bool:
        return email.endswith("@" + "internal.example.com")  # pii-guard: ignore

    return patch("fieldkit.commands.gmail.query._is_noise", side_effect=_fake_is_noise)


def test_query_blindspots_returns_external_contacts(db: sqlite3.Connection) -> None:
    with _query_blindspots_patch_noise():
        results = query_blindspots(db, "acme")
    emails = [r[0] for r in results]
    assert "alice@acme-com.example.com" in emails
    assert "carol@acme-com.example.com" in emails


def test_query_blindspots_filters_internal_addresses(db: sqlite3.Connection) -> None:
    with _query_blindspots_patch_noise():
        results = query_blindspots(db, "acme")
    emails = [r[0] for r in results]
    assert "rep@" + "internal.example.com" not in emails  # pii-guard: ignore


def test_query_blindspots_unknown_account_returns_empty(db: sqlite3.Connection) -> None:
    with _query_blindspots_patch_noise():
        results = query_blindspots(db, "nonexistent-account")
    assert results == []


def test_query_blindspots_returns_display_name(db: sqlite3.Connection) -> None:
    with _query_blindspots_patch_noise():
        results = query_blindspots(db, "acme")
    name_map = {r[0]: r[1] for r in results}
    assert name_map.get("alice@acme-com.example.com") == "Alice Smith"


def test_query_blindspots_tuple_structure(db: sqlite3.Connection) -> None:
    with _query_blindspots_patch_noise():
        results = query_blindspots(db, "acme")
    assert len(results) > 0
    email, name, msg_count, last_epoch = results[0]
    assert isinstance(email, str)
    assert isinstance(name, str)
    assert isinstance(msg_count, int)
    assert isinstance(last_epoch, int)


def test_query_blindspots_limit_respected(db: sqlite3.Connection) -> None:
    with _query_blindspots_patch_noise():
        results = query_blindspots(db, "acme", limit=1)
    assert len(results) <= 1


def test_query_blindspots_since_filter(db: sqlite3.Connection) -> None:
    with _query_blindspots_patch_noise():
        # Use a since date that excludes the February thread
        results_all = query_blindspots(db, "acme")
        results_march = query_blindspots(db, "acme", since="2024-03-01")
    # March-only should have fewer or equal results
    assert len(results_march) <= len(results_all)


# ---------------------------------------------------------------------------
# query_dig
# ---------------------------------------------------------------------------


# ── TestQueryDig (flattened) ────────────────────────────────────────────────


def test_query_dig_keyword_in_subject_matched(db: sqlite3.Connection) -> None:
    results = query_dig(db, "acme", "training")
    assert len(results) >= 1
    subjects = [r["subject"] for r in results]
    assert any("Training" in (s or "") for s in subjects)


def test_query_dig_keyword_in_body_matched(db: sqlite3.Connection) -> None:
    results = query_dig(db, "acme", "renewal")
    assert len(results) >= 1


def test_query_dig_no_match_returns_empty(db: sqlite3.Connection) -> None:
    results = query_dig(db, "acme", "xyznotaword")
    assert results == []


def test_query_dig_wrong_account_returns_empty(db: sqlite3.Connection) -> None:
    # "training" exists in acme threads, not in "other"
    results = query_dig(db, "other", "training")
    assert results == []


def test_query_dig_result_dict_keys(db: sqlite3.Connection) -> None:
    results = query_dig(db, "acme", "training")
    assert len(results) >= 1
    r = results[0]
    for key in ("thread_id", "subject", "message_count", "from_addr", "first_date", "last_date"):
        assert key in r, f"Missing key: {key}"


def test_query_dig_limit_respected(db: sqlite3.Connection) -> None:
    results = query_dig(db, "acme", "proposal", limit=1)
    assert len(results) <= 1


def test_query_dig_since_filter_excludes_old_threads(db: sqlite3.Connection) -> None:
    # With a future since date, nothing should match
    results = query_dig(db, "acme", "training", since="2030-01-01")
    assert results == []


# ---------------------------------------------------------------------------
# historic regression: _internal_blind_domains includes Salesforce notification domains
# ---------------------------------------------------------------------------


# ── TestBug043SalesforceBlindDomains (flattened) ────────────────────────────


def test_internal_blind_domains_salesforce_com_is_blind() -> None:
    """salesforce.com is in the default internal blind domains."""
    from unittest.mock import patch

    from fieldkit.commands.gmail.query import _internal_blind_domains

    # Patch get_internal_domains to return [] — salesforce.com is in AUTOMATION_NOISE_DOMAINS (always filtered)
    with patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]):
        domains = _internal_blind_domains()

    assert "salesforce.com" in domains, "salesforce.com must be in default blind domains (historic regression)"


def test_internal_blind_domains_sfdctest_com_is_blind() -> None:
    """sfdctest.com (Salesforce sandbox) is in the default internal blind domains."""
    from unittest.mock import patch

    from fieldkit.commands.gmail.query import _internal_blind_domains

    with patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]):
        domains = _internal_blind_domains()

    assert "sfdctest.com" in domains, "sfdctest.com must be in default blind domains (historic regression)"


def test_internal_blind_domains_force_com_is_blind() -> None:
    """force.com is in the default internal blind domains."""
    from unittest.mock import patch

    from fieldkit.commands.gmail.query import _internal_blind_domains

    with patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]):
        domains = _internal_blind_domains()

    assert "force.com" in domains, "force.com must be in default blind domains (historic regression)"


def test_internal_blind_domains_exacttarget_com_is_blind() -> None:
    """exacttarget.com (Salesforce Marketing Cloud) is in the default internal blind domains."""
    from unittest.mock import patch

    from fieldkit.commands.gmail.query import _internal_blind_domains

    with patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]):
        domains = _internal_blind_domains()

    assert "exacttarget.com" in domains, "exacttarget.com must be in default blind domains (historic regression)"


def test_internal_blind_domains_salesforce_email_filtered_from_blindspots(db: sqlite3.Connection) -> None:
    """Emails from salesforce.com are filtered out of blindspot results."""
    from unittest.mock import patch

    from fieldkit.commands.gmail.query import query_blindspots

    # Insert a Salesforce notification message into the acme account threads
    db.execute(
        "INSERT INTO messages (message_id, thread_id, from_addr, to_addr, cc_addr, subject, date_str, date_epoch, body_plain) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "m-sf-1",
            "t-acme-1",
            "noreply@salesforce-com.example.com",
            "rep@your-org-com.example.com",
            "",
            "Salesforce notification",
            "2024-03-02T08:00:00",
            1_709_373_600,
            "Your Salesforce record was updated.",
        ),
    )
    db.commit()

    # Use the real _is_noise (which calls _internal_blind_domains) but patch
    # get_internal_domains to return None so the default set (with salesforce.com) applies.
    with patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]):
        results = query_blindspots(db, "acme")

    emails = [r[0] for r in results]
    assert "noreply@salesforce-com.example.com" not in emails, (
        "noreply@salesforce-com.example.com must be filtered from blindspots (historic regression)"
    )
