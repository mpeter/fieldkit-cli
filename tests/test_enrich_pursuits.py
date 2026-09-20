"""Tests for enrich_pursuits.py pure functions."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.gmail import enrich_pursuits as ep

pytestmark = pytest.mark.unit


# ── TestGetPursuitKeywords (flattened) ──────────────────────────────────────


def test_get_pursuit_keywords_from_filename(tmp_path):
    p = tmp_path / "openshift-migration.md"
    p.write_text("---\ntitle: test\n---\nBody text", encoding="utf-8")
    keywords = ep.get_pursuit_keywords(str(p))
    assert "openshift" in keywords or "migration" in keywords


def test_get_pursuit_keywords_from_selling_line(tmp_path):
    p = tmp_path / "deal.md"
    p.write_text("---\n---\nWhat We're Selling: Ansible automation platform", encoding="utf-8")
    keywords = ep.get_pursuit_keywords(str(p))
    assert "Ansible" in keywords or "automation" in keywords


def test_get_pursuit_keywords_max_five(tmp_path):
    p = tmp_path / "many-words-here-that-are-long.md"
    p.write_text("---\n---\nWhat We're Selling: Ansible Quay RHOAI OSV ACS AAP EDA", encoding="utf-8")
    keywords = ep.get_pursuit_keywords(str(p))
    assert len(keywords) <= 5


def test_review_checklist_routes_email_evidence_to_read_only_native_coaching() -> None:
    """Email evidence is staged for read-only ClosePlan coaching."""
    checklist = "\n".join(ep._render_checklist(["account/pursuits/deal.md"]))

    assert "Stage email evidence for /grill against exact native ClosePlan questions" in checklist
    assert "Do not write qualification state from this report" in checklist
    assert "Update MEDDPICC" not in checklist


# ── TestBuildAccountReportNoDB (flattened) ──────────────────────────────────


def _build_account_report_no_db_make_pursuits(tmp_path: Path) -> Path:
    account_dir = tmp_path / "accounts" / "test-acct" / "pursuits"
    account_dir.mkdir(parents=True)
    pursuit = account_dir / "openshift-migration.md"
    pursuit.write_text("---\ntitle: test\n---\nWhat We're Selling: Ansible migration", encoding="utf-8")
    return tmp_path / "accounts"


def test_build_account_report_no_db_no_subprocess_import():
    """subprocess must not be imported in enrich_pursuits."""
    import fieldkit.commands.gmail.enrich_pursuits as m

    assert not hasattr(m, "subprocess"), "subprocess must not be imported"
    # Also verify run_query is gone
    assert not hasattr(m, "run_query"), "run_query must not exist"


def test_build_account_report_no_db_build_account_report_no_pursuits(tmp_path, monkeypatch):
    """Returns None when no pursuits exist for the account."""
    accounts_root = tmp_path / "accounts"
    (accounts_root / "empty-acct" / "pursuits").mkdir(parents=True)

    monkeypatch.setattr(ep, "get_accounts_root", lambda: accounts_root)
    result = ep.build_account_report("empty-acct")
    assert result is None


def test_build_account_report_no_db_build_account_report_calls_direct_functions(tmp_path, monkeypatch):
    """build_account_report calls query_blindspots, query_champion_signals, query_dig directly."""
    accounts_root = _build_account_report_no_db_make_pursuits(tmp_path)
    monkeypatch.setattr(ep, "get_accounts_root", lambda: accounts_root)

    # Fake DB path (won't be opened — we mock connect)
    fake_db = tmp_path / "gmail.db"
    fake_db.touch()
    monkeypatch.setattr(ep, "get_gmail_db_path", lambda: fake_db)

    fake_conn = MagicMock(spec=sqlite3.Connection)

    blindspots_data = [
        ("jane@example.com", "Jane Doe", 15, 1748000000),  # pii-guard: ignore
        ("bob@example.com", "Bob Smith", 8, 1747000000),  # pii-guard: ignore
    ]
    dig_data = [
        {
            "thread_id": "t1",
            "subject": "Migration plan",
            "message_count": 3,
            "from_addr": "jane@example.com",  # pii-guard: ignore
            "first_date": "2025-11-01",
            "last_date": "2025-11-15",
        }
    ]

    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.connect", return_value=fake_conn) as mock_connect,
        patch("fieldkit.commands.gmail.enrich_pursuits.query_blindspots", return_value=blindspots_data) as mock_bs,
        patch(
            "fieldkit.commands.gmail.enrich_pursuits.query_champion_signals",
            return_value="Champion signal: Jane Doe\n  Threads initiated   : 3  (50% initiation rate)\n  Signal: INITIATOR",
        ) as mock_champ,
        patch("fieldkit.commands.gmail.enrich_pursuits.query_dig", return_value=dig_data) as mock_dig,
    ):
        report = ep.build_account_report("test-acct")

    # Connection opened once and closed
    mock_connect.assert_called_once_with(fake_db)
    fake_conn.close.assert_called_once()

    # Direct query functions called with the shared connection.
    # since= is now a pre-converted epoch int (date_to_epoch(_since_date())).
    from fieldkit.commands.gmail.query import date_to_epoch

    expected_since = date_to_epoch(ep._since_date())
    mock_bs.assert_called_once_with(fake_conn, "test-acct", since=expected_since, limit=None)
    mock_champ.assert_called()  # called for top contacts
    mock_dig.assert_called()

    assert isinstance(report, str)
    assert "# Gmail Intelligence Report: test-acct" in report
    assert "Jane Doe" in report
    assert "Migration plan" in report


def test_build_account_report_sets_aside_suspected_masked_contacts(tmp_path, monkeypatch):
    accounts_root = _build_account_report_no_db_make_pursuits(tmp_path)
    monkeypatch.setattr(ep, "get_accounts_root", lambda: accounts_root)
    monkeypatch.setattr(
        ep,
        "get_accounts_config",
        lambda: {"accounts": {"test-acct": {"domains": ["acme-corp.com"]}}},
    )
    fake_conn = MagicMock(spec=sqlite3.Connection)
    contacts = [
        ("alex.taylor@acme-corp.com", "Alex Taylor", 8, 1_748_000_000),
        ("casey.morgan.q7zm@acme-corp.com", "Casey Morgan", 7, 1_747_000_000),
    ]

    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.connect", return_value=fake_conn),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_blindspots", return_value=contacts),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_champion_signals", return_value="") as champion,
        patch("fieldkit.commands.gmail.enrich_pursuits.query_dig", return_value=[]),
    ):
        report = ep.build_account_report("test-acct")

    assert isinstance(report, str)
    assert "alex.taylor@acme-corp.com" in report
    assert "casey.morgan.q7zm@acme-corp.com" not in report
    assert "Set aside 1 suspected masked address(es)" in report
    champion.assert_called_once_with(fake_conn, "alex.taylor")


def test_build_account_report_no_db_build_account_report_connection_closed_on_error(tmp_path, monkeypatch):
    """Connection is closed even when query_blindspots raises."""
    accounts_root = _build_account_report_no_db_make_pursuits(tmp_path)
    monkeypatch.setattr(ep, "get_accounts_root", lambda: accounts_root)

    fake_db = tmp_path / "gmail.db"
    fake_db.touch()
    monkeypatch.setattr(ep, "get_gmail_db_path", lambda: fake_db)

    fake_conn = MagicMock(spec=sqlite3.Connection)

    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.connect", return_value=fake_conn),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_blindspots", side_effect=RuntimeError("db error")),
        pytest.raises(RuntimeError, match="db error"),
    ):
        ep.build_account_report("test-acct")

    # finally block must have closed the connection
    fake_conn.close.assert_called_once()


def test_build_account_report_no_db_build_account_report_empty_blindspots(tmp_path, monkeypatch):
    """Report is generated even when blindspots returns no contacts."""
    accounts_root = _build_account_report_no_db_make_pursuits(tmp_path)
    monkeypatch.setattr(ep, "get_accounts_root", lambda: accounts_root)

    fake_db = tmp_path / "gmail.db"
    fake_db.touch()
    monkeypatch.setattr(ep, "get_gmail_db_path", lambda: fake_db)

    fake_conn = MagicMock(spec=sqlite3.Connection)

    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.connect", return_value=fake_conn),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_blindspots", return_value=[]),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_champion_signals", return_value=""),
        patch("fieldkit.commands.gmail.enrich_pursuits.query_dig", return_value=[]),
    ):
        report = ep.build_account_report("test-acct")

    assert isinstance(report, str)
    assert "## Top Contacts by Email Volume" in report
    assert "_No matching threads found._" in report
