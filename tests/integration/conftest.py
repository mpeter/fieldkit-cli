"""Shared fixtures for integration tests."""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.commands.gmail import apply_intel as _apply_intel

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "src" / "fieldkit" / "gmail" / "schema.sql"

# One day past SINCE_EPOCH to survive timezone differences (per MEM208)
TEST_EPOCH = _apply_intel.SINCE_EPOCH + 86_400


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory):
    """Session-scoped fixture: returns Path to a seeded SQLite DB file.

    Schema applied from src/fieldkit/gmail/schema.sql.
    Seed data mirrors tests/test_gmail_cache_characterization.py:
      threads=3, messages=5, thread_accounts=2, people=3, labels=0
    """
    db_path = tmp_path_factory.mktemp("integration_db") / "gmail_test.db"

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text())

    # 3 threads
    conn.executemany(
        "INSERT INTO threads (thread_id, subject, snippet, message_count) VALUES (?, ?, ?, ?)",
        [
            ("thread001", "ADS Repave Project Kickoff", "Let's discuss the scope", 0),
            ("thread002", "OpenShift Migration Timeline", "Following up...", 0),
            ("thread003", "Unrelated vendor invoice", "Invoice attached", 0),
        ],
    )

    # 5 messages (from_addr/to_addr/cc_addr column names per MEM060)
    conn.executemany(
        """INSERT INTO messages
           (message_id, thread_id, from_addr, to_addr, cc_addr, subject, date_str, date_epoch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "msg001",
                "thread001",
                "jane.smith@acmebank.example.com",
                "user@example.com",
                "",
                "ADS Repave Project Kickoff",
                "2026-01-03",
                TEST_EPOCH,
            ),
            (
                "msg002",
                "thread001",
                "user@example.com",
                "jane.smith@acmebank.example.com",
                "bob.jones@acme-bank.example.com",
                "Re: ADS Repave Project Kickoff",
                "2026-01-04",
                TEST_EPOCH + 86_400,
            ),
            (
                "msg003",
                "thread002",
                "bob.jones@acme-bank.example.com",
                "user@example.com",
                "",
                "OpenShift Migration Timeline",
                "2026-01-05",
                TEST_EPOCH + 172_800,
            ),
            (
                "msg004",
                "thread002",
                "alice.chen@acmebank.example.com",
                "user@example.com",
                "jane.smith@acmebank.example.com",
                "Re: OpenShift Migration Timeline",
                "2026-01-06",
                TEST_EPOCH + 259_200,
            ),
            (
                "msg005",
                "thread003",
                "vendor@example.com",
                "user@example.com",
                "",
                "Unrelated vendor invoice",
                "2026-01-07",
                TEST_EPOCH + 345_600,
            ),
        ],
    )

    # 2 thread_accounts rows
    conn.executemany(
        "INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)",
        [("thread001", "acme-bank"), ("thread002", "acme-bank")],
    )

    # 3 people rows
    conn.executemany(
        """INSERT INTO people (email, display_name, message_count, thread_count, domain, account)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            ("jane.smith@acmebank.example.com", "Jane Smith", 2, 2, "acmebank.example.com", "acme-bank"),
            ("bob.jones@acme-bank.example.com", "Bob Jones", 1, 1, "acme-bank.example.com", "acme-bank"),
            ("alice.chen@acmebank.example.com", "Alice Chen", 1, 1, "acmebank.example.com", "acme-bank"),
        ],
    )

    conn.commit()
    conn.close()

    return db_path


@pytest.fixture()
def pipeline_db(tmp_path):
    """Function-scoped fixture: fresh DB seeded for full pipeline integration tests.

    Seed: 3 threads, 5 messages with labels JSON referencing LABEL_BANK,
    1 label row (LABEL_BANK → ref/acme-bank), 3 people rows with acme-bank-domain emails,
    0 thread_accounts rows (so account-tags can prove it populates them).
    Returns a Path to the SQLite file inside tmp_path.
    """
    db_path = tmp_path / "pipeline_test.db"

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text())

    # 3 threads
    conn.executemany(
        "INSERT INTO threads (thread_id, subject, snippet, message_count) VALUES (?, ?, ?, ?)",
        [
            ("thread001", "Acme Bank OpenShift Kickoff", "Kick off discussion", 0),
            ("thread002", "Acme Bank Migration Timeline", "Following up on timeline", 0),
            ("thread003", "Acme Bank Q2 Review", "Quarterly review agenda", 0),
        ],
    )

    # 1 label row
    conn.execute(
        "INSERT INTO labels (label_id, label_name) VALUES (?, ?)",
        ("LABEL_BANK", "ref/acme-bank"),
    )

    # 5 messages with labels JSON column populated
    conn.executemany(
        """INSERT INTO messages
           (message_id, thread_id, from_addr, to_addr, cc_addr, subject, date_str, date_epoch, labels)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "msg001",
                "thread001",
                "jane.smith@acmebank.example.com",
                "user@example.com",
                "",
                "Acme Bank OpenShift Kickoff",
                "2026-01-03",
                TEST_EPOCH,
                '["LABEL_BANK"]',
            ),
            (
                "msg002",
                "thread001",
                "user@example.com",
                "jane.smith@acmebank.example.com",
                "",
                "Re: Acme Bank OpenShift Kickoff",
                "2026-01-04",
                TEST_EPOCH + 86_400,
                '["LABEL_BANK"]',
            ),
            (
                "msg003",
                "thread002",
                "bob.jones@acme-bank.example.com",
                "user@example.com",
                "",
                "Acme Bank Migration Timeline",
                "2026-01-05",
                TEST_EPOCH + 172_800,
                '["LABEL_BANK"]',
            ),
            (
                "msg004",
                "thread002",
                "user@example.com",
                "bob.jones@acme-bank.example.com",
                "",
                "Re: Acme Bank Migration Timeline",
                "2026-01-06",
                TEST_EPOCH + 259_200,
                '["LABEL_BANK"]',
            ),
            (
                "msg005",
                "thread003",
                "alice.chen@acmebank.example.com",
                "user@example.com",
                "",
                "Acme Bank Q2 Review",
                "2026-01-07",
                TEST_EPOCH + 345_600,
                '["LABEL_BANK"]',
            ),
        ],
    )

    # 3 people rows — acme-bank-domain emails; no thread_accounts rows seeded
    conn.executemany(
        """INSERT INTO people (email, display_name, message_count, thread_count, domain, account)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            ("jane.smith@acmebank.example.com", "Jane Smith", 2, 2, "acmebank.example.com", "acme-bank"),
            ("bob.jones@acme-bank.example.com", "Bob Jones", 2, 1, "acme-bank.example.com", "acme-bank"),
            ("alice.chen@acmebank.example.com", "Alice Chen", 1, 1, "acmebank.example.com", "acme-bank"),
        ],
    )

    conn.commit()
    conn.close()

    return db_path


@pytest.fixture()
def account_tags_db(tmp_path):
    """Function-scoped fixture: fresh DB seeded for account-tags integration tests.

    4 threads, 3 labels (ref/acme-bank, ref/global-pay, ref/keep), 4 messages.
    thread003 carries ref/keep and must be excluded from thread_accounts.
    Returns the db_path (Path object).
    """
    db_path = tmp_path / "account_tags_test.db"

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text())

    # 4 threads
    conn.executemany(
        "INSERT INTO threads (thread_id, subject, snippet, message_count) VALUES (?, ?, ?, ?)",
        [
            ("thread001", "Acme Bank kickoff", "Let's kick off", 0),
            ("thread002", "Acme Bank follow-up", "Following up", 0),
            ("thread003", "Keep thread", "Saved for later", 0),
            ("thread004", "Global Pay intro", "Hello Global Pay team", 0),
        ],
    )

    # 3 labels: ref/acme-bank, ref/global-pay, ref/keep
    conn.executemany(
        "INSERT INTO labels (label_id, label_name) VALUES (?, ?)",
        [
            ("LABEL_BANK", "ref/acme-bank"),
            ("LABEL_GPAY", "ref/global-pay"),
            ("LABEL_KEEP", "ref/keep"),
        ],
    )

    # 4 messages with labels JSON arrays
    conn.executemany(
        """INSERT INTO messages
           (message_id, thread_id, from_addr, to_addr, cc_addr, subject, date_str, date_epoch, labels)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "msg001",
                "thread001",
                "a@acme-bank.example.com",
                "user@example.com",
                "",
                "Acme Bank kickoff",
                "2026-01-03",
                TEST_EPOCH,
                '["LABEL_BANK"]',
            ),
            (
                "msg002",
                "thread002",
                "b@acme-bank.example.com",
                "user@example.com",
                "",
                "Acme Bank follow-up",
                "2026-01-04",
                TEST_EPOCH + 86_400,
                '["LABEL_BANK"]',
            ),
            (
                "msg003",
                "thread003",
                "c@example.com",
                "user@example.com",
                "",
                "Keep thread",
                "2026-01-05",
                TEST_EPOCH + 172_800,
                '["LABEL_KEEP"]',
            ),
            (
                "msg004",
                "thread004",
                "d@globalpay.example.com",
                "user@example.com",
                "",
                "Global Pay intro",
                "2026-01-06",
                TEST_EPOCH + 259_200,
                '["LABEL_GPAY"]',
            ),
        ],
    )

    conn.commit()
    conn.close()

    return db_path


# ---------------------------------------------------------------------------
# _account_domains patch — keeps integration tests independent of live config
# ---------------------------------------------------------------------------

_INTEGRATION_ACCOUNT_DOMAINS = {
    "acme-bank": ["acmebank.example.com", "acme-bank.example.com"],
    "global-pay": ["globalpay.example.com"],
}


@pytest.fixture(autouse=True)
def patch_account_domains(monkeypatch, request):
    """Patch apply_intel._account_domains so integration tests work without
    a live config/accounts.yaml containing acme-bank / global-pay entries.
    This became necessary after the M007/S02 migration replaced parents[3]
    path derivations with get_fieldkit_home(), which now correctly resolves the
    real config that lacks these test-only accounts.
    """
    original = _apply_intel._account_domains
    original.cache_clear()
    monkeypatch.setattr(_apply_intel, "_account_domains", lambda: _INTEGRATION_ACCOUNT_DOMAINS)
    request.addfinalizer(original.cache_clear)
