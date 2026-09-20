"""Characterization tests for gmail-cache query.py and apply-intel.py behaviors.

Locks in current observable behavior of:
  - query.date_to_epoch / query.build_date_clause
  - apply_intel.get_account_contacts
  - apply_intel.get_pursuit_contacts

These tests use an in-memory SQLite DB seeded via schema.sql — no binary DB files.
"""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.commands.gmail import apply_intel, query

pytestmark = pytest.mark.characterization

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "fieldkit" / "gmail" / "schema.sql"

# Use a value guaranteed to pass the `date_epoch >= SINCE_EPOCH` filter regardless
# of the system timezone (SINCE_EPOCH is derived from datetime(2025, 10, 1).timestamp()
# which is timezone-local). One day after is always safe.
TEST_EPOCH = apply_intel.SINCE_EPOCH + 86_400


@pytest.fixture
def gmail_db():
    """In-memory SQLite DB with schema and representative test data for acme-bank.

    Populates threads, messages (from_addr/to_addr/cc_addr per MEM060),
    thread_accounts, and people with matching foreign keys so queries return rows.
    threads.message_count is 0 per MEM199 — sync.py never populates it.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())

    # 3 threads — 2 tagged to acme-bank, 1 untagged
    conn.executemany(
        "INSERT INTO threads (thread_id, subject, snippet, message_count) VALUES (?, ?, ?, ?)",
        [
            ("thread001", "ADS Repave Project Kickoff", "Let's discuss the scope", 0),
            ("thread002", "OpenShift Migration Timeline", "Following up...", 0),
            ("thread003", "Unrelated vendor invoice", "Invoice attached", 0),
        ],
    )

    # 5 messages — column names from_addr/to_addr/cc_addr (not from_email)
    conn.executemany(
        """INSERT INTO messages
           (message_id, thread_id, from_addr, to_addr, cc_addr, subject, date_str, date_epoch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "msg001",
                "thread001",
                "jane.smith@acmebank.example.com",
                "user@example.com",  # pii-guard: ignore
                "",
                "ADS Repave Project Kickoff",
                "2026-01-03",
                TEST_EPOCH,
            ),
            (
                "msg002",
                "thread001",
                "user@example.com",  # pii-guard: ignore
                "jane.smith@acmebank.example.com",
                "bob.jones@acme-bank.example.com",  # pii-guard: ignore
                "Re: ADS Repave Project Kickoff",
                "2026-01-04",
                TEST_EPOCH + 86_400,
            ),
            (
                "msg003",
                "thread002",
                "bob.jones@acme-bank.example.com",  # pii-guard: ignore
                "user@example.com",  # pii-guard: ignore
                "",
                "OpenShift Migration Timeline",
                "2026-01-05",
                TEST_EPOCH + 172_800,
            ),
            (
                "msg004",
                "thread002",
                "alice.chen@acmebank.example.com",
                "user@example.com",  # pii-guard: ignore
                "jane.smith@acmebank.example.com",
                "Re: OpenShift Migration Timeline",
                "2026-01-06",
                TEST_EPOCH + 259_200,
            ),
            (
                "msg005",
                "thread003",
                "vendor@example.com",  # pii-guard: ignore
                "user@example.com",  # pii-guard: ignore
                "",
                "Unrelated vendor invoice",
                "2026-01-07",
                TEST_EPOCH + 345_600,
            ),
        ],
    )

    # Link threads to accounts — thread003 intentionally NOT tagged to acme-bank
    conn.executemany(
        "INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)",
        [("thread001", "acme-bank"), ("thread002", "acme-bank")],
    )

    # People — emails match domains in config/accounts.yaml acme-bank entry
    # (acmebank.example.com, acme-bank.example.com, acmebank.example.com)
    conn.executemany(
        """INSERT INTO people (email, display_name, message_count, thread_count, domain, account)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            ("jane.smith@acmebank.example.com", "Jane Smith", 2, 2, "acmebank.example.com", "acme-bank"),
            (
                "bob.jones@acme-bank.example.com",
                "Bob Jones",
                1,
                1,
                "acme-bank.example.com",
                "acme-bank",
            ),  # pii-guard: ignore
            ("alice.chen@acmebank.example.com", "Alice Chen", 1, 1, "acmebank.example.com", "acme-bank"),
        ],
    )

    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# apply_intel.get_account_contacts
# ---------------------------------------------------------------------------


_TEST_ACCOUNT_DOMAINS = {
    "acme-bank": ["acmebank.example.com", "acme-bank.example.com"],
}


# ── TestGetAccountContacts (flattened) ──────────────────────────────────────


@pytest.fixture(autouse=True)
def patch_account_domains(monkeypatch, request):
    """Patch _account_domains to use test fixture domains (independent of live config)."""
    original = apply_intel._account_domains
    original.cache_clear()
    monkeypatch.setattr(apply_intel, "_account_domains", lambda: _TEST_ACCOUNT_DOMAINS)
    request.addfinalizer(original.cache_clear)


def test_get_account_contacts_returns_contacts_for_acme_bank(gmail_db):
    """Current behavior: returns people rows whose emails match acme-bank domains
    and who appear as from_addr in acme-bank-tagged threads after SINCE_EPOCH."""
    rows = apply_intel.get_account_contacts(gmail_db, "acme-bank", limit=10)
    assert len(rows) > 0, "Expected at least one contact for acme-bank"
    emails = [r[0] for r in rows]
    # All returned contacts must have acme-bank-domain emails
    acme_bank_domains = ("acmebank.example.com", "acme-bank.example.com", "acmebank.example.com")
    for email in emails:
        assert any(email.endswith(d) for d in acme_bank_domains), f"{email} does not belong to a acme-bank domain"


def test_get_account_contacts_returns_empty_for_nonexistent_account(gmail_db):
    """Current behavior: account not in ACCOUNT_DOMAINS → domains list empty → returns []."""
    rows = apply_intel.get_account_contacts(gmail_db, "nonexistent_corp", limit=10)
    assert rows == []


def test_get_account_contacts_result_shape_has_five_columns(gmail_db):
    """Current behavior: each row is (email, display_name, thread_count, msg_count, last_epoch)."""
    rows = apply_intel.get_account_contacts(gmail_db, "acme-bank", limit=10)
    assert len(rows) > 0
    row = rows[0]
    assert len(row) == 5, f"Expected 5 columns, got {len(row)}: {tuple(row)}"


def test_get_account_contacts_limit_is_respected(gmail_db):
    """Current behavior: LIMIT ? applied — result set does not exceed requested limit."""
    rows = apply_intel.get_account_contacts(gmail_db, "acme-bank", limit=1)
    assert len(rows) <= 1


# ---------------------------------------------------------------------------
# apply_intel.get_pursuit_contacts
# ---------------------------------------------------------------------------


# ── TestGetPursuitContacts (flattened) ──────────────────────────────────────


def test_get_pursuit_contacts_fallback_to_account_wide_when_fewer_than_five_contacts(gmail_db):
    """Current behavior: if the pursuit-scoped CTE returns < 5 rows, falls back
    to get_account_contacts. Our fixture has 3 contacts total, so the fallback fires."""
    known_emails = {"jane.smith@acmebank.example.com"}
    rows = apply_intel.get_pursuit_contacts(gmail_db, "acme-bank", known_emails, limit=10)
    # Should return the account-wide set (3 contacts) via fallback
    assert len(rows) > 0


def test_get_pursuit_contacts_returns_empty_for_account_with_no_domains(gmail_db):
    """Current behavior: account not in ACCOUNT_DOMAINS → get_account_contacts returns []
    → get_pursuit_contacts also returns []."""
    rows = apply_intel.get_pursuit_contacts(
        gmail_db,
        "unknown_account",
        {"some@email.example.com"},  # pii-guard: ignore
        limit=10,  # pii-guard: ignore
    )  # pii-guard: ignore
    assert rows == []


def test_get_pursuit_contacts_fallback_when_no_known_emails(gmail_db):
    """Current behavior: empty known_emails triggers immediate fallback to get_account_contacts."""
    rows = apply_intel.get_pursuit_contacts(gmail_db, "acme-bank", set(), limit=10)
    # Same as get_account_contacts for acme-bank
    account_rows = apply_intel.get_account_contacts(gmail_db, "acme-bank", limit=10)
    assert len(rows) == len(account_rows)


# ---------------------------------------------------------------------------
# query.date_to_epoch and query.build_date_clause (characterization completeness)
# ---------------------------------------------------------------------------


# ── TestDateHelpersRoundtrip (flattened) ────────────────────────────────────


def test_date_helpers_roundtrip_date_to_epoch_known_value():
    """2025-01-01 UTC maps to a stable epoch — characterizes current output."""
    epoch = query.date_to_epoch("2025-01-01")
    # 2025-01-01 00:00:00 UTC = 1735689600 (calendar.timegm uses UTC)
    assert epoch == 1_735_689_600


def test_date_helpers_roundtrip_date_to_epoch_before_adds_exactly_one_day():
    """is_before=True adds exactly 86400 seconds (exclusive upper bound)."""
    base = query.date_to_epoch("2025-06-15")
    before = query.date_to_epoch("2025-06-15", is_before=True)
    assert before - base == 86_400


def test_date_helpers_roundtrip_build_date_clause_no_args_returns_empty_fragment():
    fragment, params = query.build_date_clause(None, None)
    assert fragment == ""
    assert params == []


def test_date_helpers_roundtrip_build_date_clause_since_only():
    fragment, params = query.build_date_clause("2025-01-01", None)
    assert "date_epoch >= ?" in fragment
    assert len(params) == 1


def test_date_helpers_roundtrip_build_date_clause_before_only():
    fragment, params = query.build_date_clause(None, "2025-06-01")
    assert "date_epoch < ?" in fragment
    assert len(params) == 1


def test_date_helpers_roundtrip_build_date_clause_both_dates():
    fragment, params = query.build_date_clause("2025-01-01", "2025-06-01")
    assert "date_epoch >= ?" in fragment
    assert "date_epoch < ?" in fragment
    assert len(params) == 2


# ---------------------------------------------------------------------------
# Characterize query module API surface
# ---------------------------------------------------------------------------


# ── TestQueryModuleApiSurface (flattened) ───────────────────────────────────


def test_query_module_api_surface_build_query_does_not_exist():
    """Characterization: query.py has no top-level build_query function.
    SQL assembly is inlined per command function (cmd_account, cmd_person, etc.).
    This test documents the current API — do not add build_query without updating tests."""
    assert not hasattr(query, "build_query"), (
        "build_query unexpectedly appeared in query.py — update characterization tests"
    )


def test_query_module_api_surface_public_helper_functions_exist():
    """Characterization: these helpers are the stable query.py public surface."""
    assert callable(query.date_to_epoch)
    assert callable(query.build_date_clause)
    assert callable(query.connect)
