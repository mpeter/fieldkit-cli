"""Unit tests for tools/gmail-cache/decay.py."""

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.gmail import decay
from fieldkit.gmail import decay_domain

pytestmark = pytest.mark.unit

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "fieldkit" / "gmail" / "schema.sql"


def _make_db(tmp_path, *, people=None, threads=None, messages=None, thread_accounts=None):
    """Create a temp SQLite DB with schema and optional seeded rows.

    Args:
        tmp_path: pytest tmp_path fixture
        people: list of (email, display_name) tuples
        threads: list of thread_id strings
        messages: list of (message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch) tuples
        thread_accounts: list of (thread_id, account) tuples
    """
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text())

    for thread_id in threads or []:
        conn.execute(
            "INSERT INTO threads(thread_id, subject, snippet, message_count, updated_at) VALUES (?,?,?,?,?)",
            (thread_id, "Test subject", "", 1, "2024-01-01"),
        )

    for row in people or []:
        email, display_name = row
        conn.execute(
            "INSERT INTO people(email, display_name) VALUES (?,?)",
            (email, display_name),
        )

    for row in messages or []:
        msg_id, thread_id, from_addr, to_addr, cc_addr, date_epoch = row
        conn.execute(
            "INSERT INTO messages(message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch, synced_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'))",
            (msg_id, thread_id, from_addr, to_addr, cc_addr, date_epoch),
        )

    for row in thread_accounts or []:
        thread_id, account = row
        conn.execute(
            "INSERT INTO thread_accounts(thread_id, account) VALUES (?,?)",
            (thread_id, account),
        )

    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# test_is_internal_or_noise
# ---------------------------------------------------------------------------


def test_is_internal_or_noise_configured_domain():
    """Emails at internal.example.com are flagged as internal when configured."""  # pii-guard: ignore
    with patch(
        "fieldkit.gmail.decay_domain.get_internal_domains", return_value=["internal.example.com"]
    ):  # pii-guard: ignore
        assert decay_domain.is_internal_or_noise("alice@internal.example.com") is True  # pii-guard: ignore


def test_is_internal_or_noise_ibm():
    """Emails at external.example.com are flagged as internal when configured."""
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=["external.example.com"]):
        assert decay_domain.is_internal_or_noise("bob@external.example.com") is True


def test_is_internal_or_noise_noreply():
    """noreply addresses are flagged as noise regardless of domain."""
    assert decay_domain.is_internal_or_noise("noreply@globalpay.example.com") is True


def test_is_internal_or_noise_external():
    """A normal external address is not filtered."""
    assert decay_domain.is_internal_or_noise("cfo@globalpay.example.com") is False


def test_is_internal_or_noise_support():
    """'support' substring is a noise pattern."""
    assert decay_domain.is_internal_or_noise("support@acme.example.com") is True


# ---------------------------------------------------------------------------
# test_extract_email
# ---------------------------------------------------------------------------


def test_extract_email_angle_bracket_format():
    """'Name <email@domain.example.com>' returns bare lowercased email."""
    result = decay_domain._extract_email("Alice Smith <Alice@GlobAlPay.Example.Com>")
    assert result == "alice@globalpay.example.com"


def test_extract_email_bare_address():
    """A bare email address is returned lowercased without modification."""
    result = decay_domain._extract_email("BOB@EXAMPLE.COM")
    assert result == "bob@example.com"  # pii-guard: ignore


def test_extract_email_whitespace():
    """Leading/trailing whitespace is stripped."""
    result = decay_domain._extract_email("  carol@bank.example.com  ")
    assert result == "carol@bank.example.com"


# ---------------------------------------------------------------------------
# test_decay_report_empty_db
# ---------------------------------------------------------------------------


def test_decay_report_empty_db(tmp_path, capsys):
    """A DB with no people/threads for the given account prints a 'no threads' message."""
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    decay.decay_report(conn, account="nonexistent-account", days_threshold=90, show_all=True)
    conn.close()

    out = capsys.readouterr().out
    assert "No threads found for account" in out


# ---------------------------------------------------------------------------
# test_decay_report_returns_rows
# ---------------------------------------------------------------------------


def test_decay_report_returns_rows(tmp_path, capsys):
    """Seeded DB with messages for 'global-pay' returns external contacts in output."""
    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)  # 120 days ago — COLD
    recent_epoch = now_epoch - (10 * 86400)  # 10 days ago — active

    db_path = _make_db(
        tmp_path,
        people=[
            ("cfo@globalpay.example.com", "Global Pay CFO"),
            ("pm@globalpay.example.com", "Global Pay PM"),
        ],
        threads=["t1", "t2"],
        messages=[
            # cfo@globalpay.example.com last wrote 120 days ago (COLD)
            ("m1", "t1", "cfo@globalpay.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
            # pm@globalpay.example.com last wrote 10 days ago (active)
            ("m2", "t2", "pm@globalpay.example.com", "me@internal.example.com", "", recent_epoch),  # pii-guard: ignore
        ],
        thread_accounts=[
            ("t1", "global-pay"),
            ("t2", "global-pay"),
        ],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # show_all=True so we see both contacts regardless of days threshold
    decay.decay_report(conn, account="global-pay", days_threshold=90, show_all=True)
    conn.close()

    out = capsys.readouterr().out
    assert "cfo@globalpay.example.com" in out
    assert "pm@globalpay.example.com" in out
    # With show_all=True, decay sorts ascending (most-recent first) so active contact
    # (10d) appears before COLD contact (120d).
    assert out.index("pm@globalpay.example.com") < out.index("cfo@globalpay.example.com")
    assert "COLD" in out


def test_decay_report_filters_internal(tmp_path, capsys):
    """internal.example.com addresses appearing in messages are NOT listed as contacts."""  # pii-guard: ignore
    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        threads=["t1"],
        messages=[
            (
                "m1",
                "t1",
                "internal@internal.example.com",
                "external@globalpay.example.com",
                "",
                old_epoch,
            ),  # pii-guard: ignore
        ],
        thread_accounts=[("t1", "global-pay")],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    with patch(
        "fieldkit.gmail.decay_domain.get_internal_domains", return_value=["internal.example.com"]
    ):  # pii-guard: ignore
        decay.decay_report(conn, account="global-pay", days_threshold=90, show_all=True)
    conn.close()

    out = capsys.readouterr().out
    assert "internal@internal.example.com" not in out  # pii-guard: ignore


# ---------------------------------------------------------------------------
# test_db_not_found_exits
# ---------------------------------------------------------------------------


def test_db_not_found_exits(tmp_path, capsys):
    """connect() with a nonexistent path raises GmailDbNotFoundError (historic regression)."""
    from fieldkit.gmail.exceptions import GmailDbNotFoundError

    bad_path = tmp_path / "does_not_exist.db"
    with pytest.raises(GmailDbNotFoundError) as exc_info:
        decay.connect(bad_path)

    assert "not found" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# _build_contact_list — set domain_filter (historic regression)
# ---------------------------------------------------------------------------


# ── TestBuildContactListDomainFilterSet (flattened) ─────────────────────────


def _build_contact_list_domain_filter_set_make_person_stats(emails: list[str]) -> dict:
    now = int(time.time())
    stats = {}
    for email in emails:
        stats[email] = {
            "last_any": now - 200 * 86400,
            "first_any": now - 400 * 86400,
            "last_sent": 0,
            "last_recv": 0,
            "msgs_sent": 0,
            "msgs_recv": 5,
            "threads": {"t1"},
            "messages": 5,
            "display_name": "",
        }
    return stats


def test_build_contact_list_domain_filter_set_set_filter_includes_matching_domains():
    person_stats = _build_contact_list_domain_filter_set_make_person_stats(
        ["alice@acme-bank.example.com", "bob@globalpay.example.com", "carol@acme-bank.example.com"]  # pii-guard: ignore
    )  # pii-guard: ignore
    now = int(time.time())
    contacts = decay_domain._build_contact_list(
        person_stats,
        now_epoch=now,
        days_threshold=30,
        show_all=True,
        domain_filter={"acme-bank.example.com"},
        min_messages=1,
        limit=None,
        max_age_days=None,
    )
    emails = [c["email"] for c in contacts]
    assert "alice@acme-bank.example.com" in emails  # pii-guard: ignore
    assert "carol@acme-bank.example.com" in emails  # pii-guard: ignore
    assert "bob@globalpay.example.com" not in emails


def test_build_contact_list_domain_filter_set_set_filter_with_multiple_domains():
    person_stats = _build_contact_list_domain_filter_set_make_person_stats(
        ["alice@acme-bank.example.com", "bob@globalpay.example.com", "carol@other.example.com"]  # pii-guard: ignore
    )  # pii-guard: ignore
    now = int(time.time())
    contacts = decay_domain._build_contact_list(
        person_stats,
        now_epoch=now,
        days_threshold=30,
        show_all=True,
        domain_filter={"acme-bank.example.com", "globalpay.example.com"},
        min_messages=1,
        limit=None,
        max_age_days=None,
    )
    emails = [c["email"] for c in contacts]
    assert "alice@acme-bank.example.com" in emails  # pii-guard: ignore
    assert "bob@globalpay.example.com" in emails
    assert "carol@other.example.com" not in emails


def test_build_contact_list_domain_filter_set_none_filter_shows_all_external():
    person_stats = _build_contact_list_domain_filter_set_make_person_stats(
        ["alice@acme-bank.example.com", "bob@globalpay.example.com"]  # pii-guard: ignore
    )  # pii-guard: ignore
    now = int(time.time())
    contacts = decay_domain._build_contact_list(
        person_stats,
        now_epoch=now,
        days_threshold=30,
        show_all=True,
        domain_filter=None,
        min_messages=1,
        limit=None,
        max_age_days=None,
    )
    emails = [c["email"] for c in contacts]
    assert "alice@acme-bank.example.com" in emails  # pii-guard: ignore
    assert "bob@globalpay.example.com" in emails


# ── TestDecayReportAutoDomainFilter (flattened) ─────────────────────────────


def test_decay_report_auto_domain_filter_auto_domain_filter_excludes_cross_account_contacts(tmp_path, capsys):
    """When no --domain is passed (show_all=False), contacts from other accounts are excluded.

    Note: show_all=True bypasses domain filtering entirely (historic regression fix). This test
    uses show_all=False to verify the auto-domain-filter path still works correctly.
    Both contacts are 120d old so they pass the days_threshold=30 stale filter.
    """
    from unittest.mock import patch

    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        people=[
            ("cfo@acme-bank.example.com", "Acme CFO"),  # pii-guard: ignore
            ("pm@globalpay.example.com", "GlobalPay PM"),
        ],
        threads=["t1", "t2"],
        messages=[
            ("m1", "t1", "cfo@acme-bank.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
            ("m2", "t2", "pm@globalpay.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
        ],
        thread_accounts=[("t1", "acme-bank"), ("t2", "acme-bank")],
    )

    domain_map = {"acme-bank.example.com": "acme-bank", "globalpay.example.com": "global-pay"}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    with patch("fieldkit.gmail.decay_domain.build_domain_account_map", return_value=domain_map):
        # show_all=False: domain filter applies; days_threshold=30 so 120d-old contacts show
        decay.decay_report(conn, account="acme-bank", days_threshold=30, show_all=False)

    conn.close()
    out = capsys.readouterr().out
    assert "cfo@acme-bank.example.com" in out  # pii-guard: ignore
    assert "pm@globalpay.example.com" not in out


def test_decay_report_auto_domain_filter_auto_domain_filter_no_domains_configured_shows_all(tmp_path, capsys):
    """When accounts.yaml has no domains for the account, filter is not applied."""
    from unittest.mock import patch

    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        people=[("cfo@acme-bank.example.com", "Acme CFO")],  # pii-guard: ignore
        threads=["t1"],
        messages=[
            ("m1", "t1", "cfo@acme-bank.example.com", "me@internal.example.com", "", old_epoch)
        ],  # pii-guard: ignore
        thread_accounts=[("t1", "acme-bank")],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    with patch("fieldkit.gmail.decay_domain.build_domain_account_map", return_value={}):
        decay.decay_report(conn, account="acme-bank", days_threshold=90, show_all=True)

    conn.close()
    out = capsys.readouterr().out
    assert "cfo@acme-bank.example.com" in out  # pii-guard: ignore


def test_decay_report_auto_domain_filter_auto_domain_filter_no_domains_configured_warns(tmp_path, caplog):
    """When accounts.yaml has no domains for the account, a warning is logged.

    Note: show_all=True bypasses domain filtering entirely (historic regression fix), so the
    warning path is only reached when show_all=False and no explicit domain_filter
    is provided. This test uses show_all=False to exercise the warning branch.
    """
    import logging
    from unittest.mock import patch

    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        people=[("cfo@acme-bank.example.com", "Acme CFO")],  # pii-guard: ignore
        threads=["t1"],
        messages=[
            ("m1", "t1", "cfo@acme-bank.example.com", "me@internal.example.com", "", old_epoch)
        ],  # pii-guard: ignore
        thread_accounts=[("t1", "acme-bank")],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    with (
        patch("fieldkit.gmail.decay_domain.build_domain_account_map", return_value={}),
        caplog.at_level(logging.WARNING, logger="fieldkit.gmail.decay_domain"),
    ):
        # show_all=False so the auto-lookup branch runs and can emit the warning
        decay.decay_report(conn, account="acme-bank", days_threshold=30, show_all=False)

    conn.close()
    assert any(
        "no domains configured" in record.message and "acme-bank" in record.message for record in caplog.records
    ), "Expected a WARNING about missing domain configuration"


def test_decay_report_auto_domain_filter_auto_domain_filter_no_domains_configured_warns_stderr(tmp_path, caplog):
    """historic regression: when no domain filter is configured, a WARNING is logged.

    Previously emitted via click.echo(..., err=True); now uses log.warning() so the
    domain layer stays click-free. The warning is captured by the logging system and
    visible via log propagation (e.g. pytest caplog, structured logging handlers).
    """
    import logging
    from unittest.mock import patch

    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        people=[("cfo@acme-bank.example.com", "Acme CFO")],  # pii-guard: ignore
        threads=["t1"],
        messages=[
            ("m1", "t1", "cfo@acme-bank.example.com", "me@your-org.example.com", "", old_epoch)
        ],  # pii-guard: ignore
        thread_accounts=[("t1", "acme-bank")],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    with (
        patch("fieldkit.gmail.decay_domain.build_domain_account_map", return_value={}),
        caplog.at_level(logging.WARNING, logger="fieldkit.gmail.decay_domain"),
    ):
        decay.decay_report(conn, account="acme-bank", days_threshold=30, show_all=False)

    conn.close()
    assert any(
        "no domains configured" in record.message.lower() and "acme-bank" in record.message for record in caplog.records
    ), f"Expected WARNING about missing domain configuration in log records, got: {caplog.records}"


def test_decay_report_auto_domain_filter_show_all_bypasses_domain_filter(tmp_path, capsys):
    """historic regression: show_all=True must set resolved_domain_filter=None so ALL external
    contacts are returned regardless of what build_domain_account_map() returns."""
    from unittest.mock import patch

    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        people=[
            ("cfo@acme-bank.example.com", "Acme CFO"),  # pii-guard: ignore
            ("pm@globalpay.example.com", "GlobalPay PM"),
        ],
        threads=["t1", "t2"],
        messages=[
            ("m1", "t1", "cfo@acme-bank.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
            ("m2", "t2", "pm@globalpay.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
        ],
        thread_accounts=[("t1", "acme-bank"), ("t2", "acme-bank")],
    )

    # accounts.yaml maps only acme-bank.example.com to acme-bank — without the fix,
    # show_all=True would still restrict to acme-bank.example.com and hide pm@globalpay.example.com.
    domain_map = {"acme-bank.example.com": "acme-bank"}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    with patch("fieldkit.gmail.decay_domain.build_domain_account_map", return_value=domain_map):
        decay.decay_report(conn, account="acme-bank", days_threshold=90, show_all=True)

    conn.close()
    out = capsys.readouterr().out
    # Both contacts must appear when show_all=True — domain filter must be bypassed.
    assert "cfo@acme-bank.example.com" in out  # pii-guard: ignore
    assert "pm@globalpay.example.com" in out


def test_decay_report_auto_domain_filter_show_all_false_still_applies_domain_filter(tmp_path, capsys):
    """historic regression complement: show_all=False must still apply the domain filter."""
    from unittest.mock import patch

    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        people=[
            ("cfo@acme-bank.example.com", "Acme CFO"),  # pii-guard: ignore
            ("pm@globalpay.example.com", "GlobalPay PM"),
        ],
        threads=["t1", "t2"],
        messages=[
            ("m1", "t1", "cfo@acme-bank.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
            ("m2", "t2", "pm@globalpay.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
        ],
        thread_accounts=[("t1", "acme-bank"), ("t2", "acme-bank")],
    )

    domain_map = {"acme-bank.example.com": "acme-bank"}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    with patch("fieldkit.gmail.decay_domain.build_domain_account_map", return_value=domain_map):
        # show_all=False, days_threshold=30 — both contacts are old (120d) so both
        # would pass the stale filter, but domain filter should exclude globalpay.
        decay.decay_report(conn, account="acme-bank", days_threshold=30, show_all=False)

    conn.close()
    out = capsys.readouterr().out
    assert "cfo@acme-bank.example.com" in out  # pii-guard: ignore
    assert "pm@globalpay.example.com" not in out


def test_decay_report_auto_domain_filter_domain_filter_accepts_set_of_strings(tmp_path, capsys):
    """historic regression: decay_report must accept domain_filter as a set[str]."""
    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        people=[
            ("cfo@acme-bank.example.com", "Acme CFO"),  # pii-guard: ignore
            ("pm@globalpay.example.com", "GlobalPay PM"),
            ("ops@midwestins.example.com", "Midwest Ops"),
        ],
        threads=["t1", "t2", "t3"],
        messages=[
            ("m1", "t1", "cfo@acme-bank.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
            ("m2", "t2", "pm@globalpay.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
            ("m3", "t3", "ops@midwestins.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
        ],
        thread_accounts=[("t1", "acme-bank"), ("t2", "acme-bank"), ("t3", "acme-bank")],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Pass a set — this is the historic regression signature change
    decay.decay_report(
        conn,
        account="acme-bank",
        days_threshold=30,
        show_all=False,
        domain_filter={"acme-bank.example.com", "globalpay.example.com"},
    )
    conn.close()

    out = capsys.readouterr().out
    assert "cfo@acme-bank.example.com" in out  # pii-guard: ignore
    assert "pm@globalpay.example.com" in out
    assert "ops@midwestins.example.com" not in out


def test_decay_report_auto_domain_filter_explicit_domain_flag_overrides_auto_filter(tmp_path, capsys):
    """When --domain is explicitly passed (show_all=False), it takes precedence over auto-lookup.

    Note: show_all=True bypasses all domain filtering (historic regression fix). This test uses
    show_all=False so the explicit domain_filter is honoured and we can verify it
    overrides the auto-lookup result.
    """
    from unittest.mock import patch

    now_epoch = int(time.time())
    old_epoch = now_epoch - (120 * 86400)

    db_path = _make_db(
        tmp_path,
        people=[
            ("cfo@acme-bank.example.com", "Acme CFO"),  # pii-guard: ignore
            ("ops@fixture-domain.example.com", "Ops Contact"),
        ],
        threads=["t1", "t2"],
        messages=[
            ("m1", "t1", "cfo@acme-bank.example.com", "me@internal.example.com", "", old_epoch),  # pii-guard: ignore
            (
                "m2",
                "t2",
                "ops@fixture-domain.example.com",
                "me@internal.example.com",
                "",
                old_epoch,
            ),  # pii-guard: ignore
        ],
        thread_accounts=[("t1", "acme-bank"), ("t2", "acme-bank")],
    )

    domain_map = {"acme-bank.example.com": "acme-bank"}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    with patch("fieldkit.gmail.decay_domain.build_domain_account_map", return_value=domain_map):
        decay.decay_report(
            conn,
            account="acme-bank",
            days_threshold=30,  # both contacts are 120d old, so both pass stale filter
            show_all=False,
            domain_filter="fixture-domain.example.com",
        )

    conn.close()
    out = capsys.readouterr().out
    assert "ops@fixture-domain.example.com" in out
    assert "cfo@acme-bank.example.com" not in out  # pii-guard: ignore


# ---------------------------------------------------------------------------
# implementation change: max_age_days cutoff tests
# ---------------------------------------------------------------------------


# ── TestMaxAgeDaysCutoff (flattened) ────────────────────────────────────────


def _max_age_days_cutoff_make_person_stats(email: str, days_silent: int) -> dict:
    """Build a minimal person_stats dict for a single contact N days silent."""
    now = int(time.time())
    last_any = now - days_silent * 86400
    return {
        email: {
            "last_any": last_any,
            "first_any": last_any - 30 * 86400,
            "last_sent": last_any,
            "last_recv": 0,
            "msgs_sent": 3,
            "msgs_recv": 2,
            "threads": {"t1"},
            "messages": 5,
            "display_name": "",
        }
    }


def test_max_age_days_cutoff_contact_beyond_max_age_is_excluded() -> None:
    """Contact silent 400 days with max_age_days=365 is excluded from the report."""
    person_stats = _max_age_days_cutoff_make_person_stats(
        "buyer@acme-bank.example.com", days_silent=400
    )  # pii-guard: ignore
    now = int(time.time())
    contacts = decay_domain._build_contact_list(
        person_stats,
        now_epoch=now,
        days_threshold=30,
        show_all=False,
        domain_filter=None,
        min_messages=1,
        limit=None,
        max_age_days=365,
    )
    assert contacts == [], "Contact silent 400d should be excluded when max_age_days=365"


def test_max_age_days_cutoff_contact_within_max_age_is_included() -> None:
    """Contact silent 300 days with max_age_days=365 is included in the report."""
    person_stats = _max_age_days_cutoff_make_person_stats(
        "buyer@acme-bank.example.com", days_silent=300
    )  # pii-guard: ignore
    now = int(time.time())
    contacts = decay_domain._build_contact_list(
        person_stats,
        now_epoch=now,
        days_threshold=30,
        show_all=False,
        domain_filter=None,
        min_messages=1,
        limit=None,
        max_age_days=365,
    )
    emails = [c["email"] for c in contacts]
    assert "buyer@acme-bank.example.com" in emails, (  # pii-guard: ignore
        "Contact silent 300d should be included when max_age_days=365"
    )  # pii-guard: ignore


def test_max_age_days_cutoff_no_cutoff_when_max_age_days_is_none() -> None:
    """Contact silent 400 days with max_age_days=None (--max-age-days 0) is included."""
    person_stats = _max_age_days_cutoff_make_person_stats(
        "buyer@acme-bank.example.com", days_silent=400
    )  # pii-guard: ignore
    now = int(time.time())
    contacts = decay_domain._build_contact_list(
        person_stats,
        now_epoch=now,
        days_threshold=30,
        show_all=False,
        domain_filter=None,
        min_messages=1,
        limit=None,
        max_age_days=None,
    )
    emails = [c["email"] for c in contacts]
    assert "buyer@acme-bank.example.com" in emails, (  # pii-guard: ignore
        "Contact silent 400d should be included when max_age_days=None"
    )  # pii-guard: ignore


def test_max_age_days_cutoff_show_all_bypasses_max_age_cutoff() -> None:
    """Contact silent 400 days with show_all=True is included regardless of max_age_days."""
    person_stats = _max_age_days_cutoff_make_person_stats(
        "buyer@acme-bank.example.com", days_silent=400
    )  # pii-guard: ignore
    now = int(time.time())
    contacts = decay_domain._build_contact_list(
        person_stats,
        now_epoch=now,
        days_threshold=30,
        show_all=True,
        domain_filter=None,
        min_messages=1,
        limit=None,
        max_age_days=365,
    )
    emails = [c["email"] for c in contacts]
    assert "buyer@acme-bank.example.com" in emails, "show_all=True must bypass max_age_days cutoff"  # pii-guard: ignore


def test_max_age_days_cutoff_decay_report_max_age_excludes_old_contact(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """decay_report() with default max_age_days=365 excludes contacts silent 400 days."""
    now_epoch = int(time.time())
    old_epoch = now_epoch - 400 * 86400  # 400 days ago — beyond default 365d cutoff
    recent_epoch = now_epoch - 200 * 86400  # 200 days ago — within cutoff

    db_path = _make_db(
        tmp_path,
        people=[
            ("old@acme-bank.example.com", "Old Contact"),  # pii-guard: ignore
            ("recent@acme-bank.example.com", "Recent Contact"),  # pii-guard: ignore
        ],
        threads=["t1", "t2"],
        messages=[
            ("m1", "t1", "old@acme-bank.example.com", "me@your-org.example.com", "", old_epoch),  # pii-guard: ignore
            (
                "m2",
                "t2",
                "recent@acme-bank.example.com",
                "me@your-org.example.com",
                "",
                recent_epoch,
            ),  # pii-guard: ignore
        ],
        thread_accounts=[("t1", "acme-bank"), ("t2", "acme-bank")],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    decay.decay_report(
        conn,
        account="acme-bank",
        days_threshold=30,
        show_all=False,
        max_age_days=365,
    )
    conn.close()

    out = capsys.readouterr().out
    assert "old@acme-bank.example.com" not in out, (  # pii-guard: ignore
        "Contact silent 400d must be excluded with max_age_days=365"
    )  # pii-guard: ignore
    assert "recent@acme-bank.example.com" in out, (  # pii-guard: ignore
        "Contact silent 200d must be included with max_age_days=365"
    )  # pii-guard: ignore


def test_max_age_days_cutoff_decay_report_show_all_bypasses_max_age(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """decay_report() with show_all=True includes contacts silent 400 days despite max_age_days=365."""
    now_epoch = int(time.time())
    old_epoch = now_epoch - 400 * 86400

    db_path = _make_db(
        tmp_path,
        people=[("old@acme-bank.example.com", "Old Contact")],  # pii-guard: ignore
        threads=["t1"],
        messages=[
            ("m1", "t1", "old@acme-bank.example.com", "me@your-org.example.com", "", old_epoch)
        ],  # pii-guard: ignore
        thread_accounts=[("t1", "acme-bank")],
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    decay.decay_report(
        conn,
        account="acme-bank",
        days_threshold=30,
        show_all=True,
        max_age_days=365,
    )
    conn.close()

    out = capsys.readouterr().out
    assert "old@acme-bank.example.com" in out, (  # pii-guard: ignore
        "show_all=True must bypass max_age_days cutoff in decay_report()"
    )  # pii-guard: ignore


# ---------------------------------------------------------------------------
# org-agnostic-config: task 5.1 — internal-domain fallback tests
# ---------------------------------------------------------------------------


def test_is_internal_or_noise_empty_internal_domains_returns_false_for_external() -> None:
    """With no internal_domains configured, external addresses are not filtered."""
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=[]):
        assert decay_domain.is_internal_or_noise("cfo@globalpay.example.com") is False


def test_is_internal_or_noise_empty_internal_domains_noise_still_filtered() -> None:
    """With no internal_domains, AUTOMATION_NOISE_DOMAINS still filters noise (D2 split)."""
    with patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=[]):
        assert decay_domain.is_internal_or_noise("noreply@docusign.example.com") is True


def test_is_internal_or_noise_docusign_always_noise() -> None:
    """docusign.example.com is in AUTOMATION_NOISE_DOMAINS — filtered regardless of org config."""
    with (
        patch("fieldkit.gmail.decay_domain.get_internal_domains", return_value=[]),
        patch("fieldkit.gmail.decay_domain.AUTOMATION_NOISE_DOMAINS", {"docusign.example.com"}),
    ):
        assert decay_domain.is_internal_or_noise("contracts@docusign.example.com") is True  # pii-guard: ignore
