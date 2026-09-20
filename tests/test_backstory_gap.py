"""Unit tests for tools/gmail-cache/backstory_gap.py core functions."""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.commands.gmail import backstory_gap

pytestmark = pytest.mark.unit

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "fieldkit" / "gmail" / "schema.sql"

# backstory_gap imported above from fieldkit.commands.gmail


def _make_db(tmp_path, people_rows=None):
    """Create a temp SQLite DB with full schema and optional people rows.

    people_rows: list of dicts with keys matching the people table columns.
    Minimal required keys: email, account, is_internal, message_count.
    """
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text())
    if people_rows:
        for row in people_rows:
            conn.execute(
                """
                INSERT INTO people (
                    email, display_name, first_seen, last_seen,
                    message_count, thread_count, initiated_count, domain, account,
                    is_internal, meeting_count, slack_message_count
                ) VALUES (
                    :email, :display_name, :first_seen, :last_seen,
                    :message_count, :thread_count, :initiated_count, :domain, :account,
                    :is_internal, :meeting_count, :slack_message_count
                )
                """,
                {
                    "display_name": row.get("display_name", ""),
                    "first_seen": row.get("first_seen", "2024-01-01"),
                    "last_seen": row.get("last_seen", "2024-06-01"),
                    "thread_count": row.get("thread_count", 1),
                    "initiated_count": row.get("initiated_count", 0),
                    "domain": row.get("domain", row["email"].split("@")[-1]),
                    "meeting_count": row.get("meeting_count", 0),
                    "slack_message_count": row.get("slack_message_count", 0),
                    **{k: row[k] for k in ("email", "account", "is_internal", "message_count")},
                },
            )
    conn.commit()
    conn.close()
    return db_path


def _open_db(db_path):
    """Open a DB the same way the module does (row_factory set)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# test_is_noise
# ---------------------------------------------------------------------------


# ── TestIsNoise (flattened) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "email",
    [
        "noreply@company.example.com",  # pii-guard: ignore
        "no-reply@service.example.com",  # pii-guard: ignore
        "notifications@github.example.com",
        "mailer-daemon@example.com",  # pii-guard: ignore
        "donotreply@bank.example.com",  # pii-guard: ignore
        "do-not-reply@hr.example.com",  # pii-guard: ignore
        "bounce@mailing.example.com",  # pii-guard: ignore
        "postmaster@domain.example.org",  # pii-guard: ignore
        "support@helpdesk.example.com",  # pii-guard: ignore
        "alerts@monitoring.example.com",  # pii-guard: ignore
        "automated@system.example.net",  # pii-guard: ignore
    ],
)
def test_is_noise_noise_emails_are_detected(email):
    assert backstory_gap.is_noise(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "alice@globalpay.example.com",
        "bob.smith@acme-bank.example.com",  # pii-guard: ignore
        "john.doe@partner.example.com",  # pii-guard: ignore
        "jane@customer.example.com",
    ],
)
def test_is_noise_real_contacts_pass_through(email):
    assert backstory_gap.is_noise(email) is False


# ---------------------------------------------------------------------------
# test_gap_report_for_account_empty
# ---------------------------------------------------------------------------


def test_gap_report_for_account_empty(tmp_path):
    """An empty people table returns an empty gap list."""
    db_path = _make_db(tmp_path)
    conn = _open_db(db_path)
    account_cfg = {"domains": ["globalpay.example.com"], "blindspots_min_messages": 5}
    result = backstory_gap.gap_report_for_account(conn, "global-pay", account_cfg, min_messages=5)
    conn.close()
    assert result == []


# ---------------------------------------------------------------------------
# test_gap_report_for_account_filters_threshold
# ---------------------------------------------------------------------------


def test_gap_report_for_account_filters_threshold(tmp_path):
    """Contacts below min_messages threshold are excluded; contacts at or above are included."""
    db_path = _make_db(
        tmp_path,
        people_rows=[
            # below threshold — should be excluded
            {"email": "low@globalpay.example.com", "account": "global-pay", "is_internal": 0, "message_count": 2},
            # exactly at threshold — should be included
            {"email": "exact@globalpay.example.com", "account": "global-pay", "is_internal": 0, "message_count": 5},
            # above threshold — should be included
            {"email": "high@globalpay.example.com", "account": "global-pay", "is_internal": 0, "message_count": 20},
        ],
    )
    conn = _open_db(db_path)
    account_cfg = {"domains": ["globalpay.example.com"]}
    result = backstory_gap.gap_report_for_account(conn, "global-pay", account_cfg, min_messages=5)
    conn.close()
    emails = {r["email"] for r in result}
    assert "low@globalpay.example.com" not in emails
    assert "exact@globalpay.example.com" in emails
    assert "high@globalpay.example.com" in emails


def test_gap_report_excludes_internal_contacts(tmp_path):
    """Internal contacts (is_internal=1) are never returned even if above threshold."""
    db_path = _make_db(
        tmp_path,
        people_rows=[
            {
                "email": "internal@globalpay.example.com",
                "account": "global-pay",
                "is_internal": 1,
                "message_count": 100,
            },
            {
                "email": "external@globalpay.example.com",
                "account": "global-pay",
                "is_internal": 0,
                "message_count": 100,
            },
        ],
    )
    conn = _open_db(db_path)
    account_cfg = {"domains": ["globalpay.example.com"]}
    result = backstory_gap.gap_report_for_account(conn, "global-pay", account_cfg, min_messages=5)
    conn.close()
    emails = {r["email"] for r in result}
    assert "internal@globalpay.example.com" not in emails
    assert "external@globalpay.example.com" in emails


def test_gap_report_excludes_noise_emails(tmp_path):
    """Noise email addresses (noreply, mailer-daemon, etc.) are filtered out."""
    db_path = _make_db(
        tmp_path,
        people_rows=[
            {
                "email": "noreply@globalpay.example.com",
                "account": "global-pay",
                "is_internal": 0,
                "message_count": 50,
            },  # pii-guard: ignore
            {
                "email": "real.person@globalpay.example.com",
                "account": "global-pay",
                "is_internal": 0,
                "message_count": 50,
            },
        ],
    )
    conn = _open_db(db_path)
    account_cfg = {"domains": ["globalpay.example.com"]}
    result = backstory_gap.gap_report_for_account(conn, "global-pay", account_cfg, min_messages=5)
    conn.close()
    emails = {r["email"] for r in result}
    assert "noreply@globalpay.example.com" not in emails  # pii-guard: ignore
    assert "real.person@globalpay.example.com" in emails


# ---------------------------------------------------------------------------
# test_render_markdown_empty
# ---------------------------------------------------------------------------


def test_render_markdown_empty():
    """render_markdown with an empty contact list produces a graceful no-contacts message."""
    output = backstory_gap.render_markdown(
        account_key="global-pay",
        contacts=[],
        min_messages=10,
        as_of="2024-06-01",
    )
    assert "## global-pay" in output
    assert "No gap contacts found" in output
    # Should not contain a table header
    assert "| Email |" not in output


# ---------------------------------------------------------------------------
# test_render_markdown_with_contacts
# ---------------------------------------------------------------------------


def test_render_markdown_with_contacts():
    """render_markdown with contacts produces a markdown table with expected structure."""
    contacts = [
        {
            "email": "alice@globalpay.example.com",
            "name": "Alice Smith",
            "message_count": 42,
            "thread_count": 10,
            "meeting_count": 3,
            "slack_message_count": 7,
            "last_seen": "2024-06-15T12:00:00",
        },
        {
            "email": "bob@globalpay.example.com",
            "name": "",
            "message_count": 15,
            "thread_count": 5,
            "meeting_count": 0,
            "slack_message_count": 0,
            "last_seen": "",
        },
    ]
    output = backstory_gap.render_markdown(
        account_key="global-pay",
        contacts=contacts,
        min_messages=10,
        as_of="2024-06-01",
    )
    assert "## global-pay" in output
    assert "| Email |" in output
    assert "alice@globalpay.example.com" in output
    assert "Alice Smith" in output
    assert "bob@globalpay.example.com" in output
    # last_seen sliced to 10 chars
    assert "2024-06-15" in output
    # empty last_seen renders as em dash
    assert "—" in output
    assert "2 contact(s)" in output


# ---------------------------------------------------------------------------
# test_db_not_found_exits
# ---------------------------------------------------------------------------


def test_db_not_found_exits(tmp_path, capsys):
    """connect() with a nonexistent DB path prints an error to stderr and calls sys.exit(1)."""
    nonexistent = tmp_path / "does_not_exist.db"
    with pytest.raises(SystemExit) as exc_info:
        backstory_gap.connect(nonexistent)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "not found" in captured.err


def test_connect_returns_connection(tmp_path):
    """connect() returns an open sqlite3.Connection for an existing DB file."""
    import sqlite3

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()

    result = backstory_gap.connect(db_path)
    assert isinstance(result, sqlite3.Connection)
    result.close()
