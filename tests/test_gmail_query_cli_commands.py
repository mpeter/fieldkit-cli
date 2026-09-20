"""Coverage boost tests for gmail query CLI commands.

Uses Click's CliRunner with a patched connect() to test the command handlers
without requiring a real gmail.db file. Targets the uncovered CLI command
bodies to push query.py file-level coverage above 83%.
"""

import json
import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.query import cli

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers: in-memory DB
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """Create a minimal in-memory gmail.db with required schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            message_count INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE messages (
            msg_id TEXT PRIMARY KEY,
            thread_id TEXT,
            subject TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_str TEXT,
            date_epoch INTEGER,
            body_plain TEXT
        );
        CREATE TABLE people (
            person_id TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE thread_accounts (
            thread_id TEXT,
            account TEXT
        );
        """
    )
    conn.commit()
    return conn


def _insert_message(
    conn: sqlite3.Connection,
    thread_id: str,
    from_addr: str,
    to_addr: str = "",
    cc_addr: str = "",
    date_epoch: int = 1700000000,
    date_str: str = "2023-11-14",
    subject: str = "Test thread",
    body_plain: str = "",
) -> None:
    conn.execute(
        "INSERT INTO messages (msg_id, thread_id, subject, from_addr, to_addr, cc_addr, date_str, date_epoch, body_plain)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), thread_id, subject, from_addr, to_addr, cc_addr, date_str, date_epoch, body_plain),
    )
    conn.execute(
        "INSERT OR IGNORE INTO threads (thread_id, subject, message_count, updated_at) VALUES (?, ?, 1, ?)",
        (thread_id, subject, date_str),
    )
    conn.commit()


def _insert_person(conn: sqlite3.Connection, email: str, display_name: str) -> None:
    conn.execute(
        "INSERT INTO people (person_id, email, display_name) VALUES (?, ?, ?)",
        (email, email, display_name),
    )
    conn.commit()


def _insert_thread_account(conn: sqlite3.Connection, thread_id: str, account: str) -> None:
    conn.execute("INSERT INTO thread_accounts (thread_id, account) VALUES (?, ?)", (thread_id, account))
    conn.commit()


# ---------------------------------------------------------------------------
# cmd_person_click
# ---------------------------------------------------------------------------


# ── TestCmdPersonClick (flattened) ──────────────────────────────────────────


def test_cmd_person_click_person_no_match_prints_not_found(tmp_path: Path) -> None:
    """person command prints 'No people matched' when name not in DB."""
    db = _make_db()
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["person", "Unknown Person", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "No people matched" in result.output


def test_cmd_person_click_person_with_match_shows_threads(tmp_path: Path) -> None:
    """person command shows threads when person is found."""
    db = _make_db()
    _insert_person(db, "alice@acme-corp.com", "Alice Smith")
    _insert_message(db, "t-001", "alice@acme-corp.com", subject="Alice's thread")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["person", "Alice Smith", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0


def test_cmd_person_click_person_with_since_filter(tmp_path: Path) -> None:
    """person command accepts --since date filter."""
    db = _make_db()
    _insert_person(db, "bob@acme-corp.com", "Bob Jones")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["person", "Bob Jones", "--db", str(tmp_path / "fake.db"), "--since", "2023-01-01"])

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# cmd_account_click
# ---------------------------------------------------------------------------


# ── TestCmdAccountClick (flattened) ─────────────────────────────────────────


def test_cmd_account_click_account_no_threads_shows_zero(tmp_path: Path) -> None:
    """account command shows zero threads for unknown account."""
    db = _make_db()
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["account", "nonexistent-account", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "0 thread(s)" in result.output


def test_cmd_account_click_account_with_date_filter(tmp_path: Path) -> None:
    """account command with --since applies date filter (date_clause branch)."""
    db = _make_db()
    _insert_message(db, "t-001", "sales@acme-corp.com", date_epoch=1700000000)
    _insert_thread_account(db, "t-001", "acme-corp")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(
            cli,
            ["account", "acme-corp", "--db", str(tmp_path / "fake.db"), "--since", "2023-01-01"],
        )

    assert result.exit_code == 0


def test_cmd_account_click_account_without_date_filter(tmp_path: Path) -> None:
    """account command without date filter uses the else branch."""
    db = _make_db()
    _insert_message(db, "t-001", "sales@acme-corp.com", date_epoch=1700000000)
    _insert_thread_account(db, "t-001", "acme-corp")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["account", "acme-corp", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0


def test_query_account_option_matches_account_subcommand_defaults(tmp_path: Path) -> None:
    runner = CliRunner()

    subcommand_db = _make_db()
    _insert_message(subcommand_db, "t-001", "sales@acme-corp.com", date_epoch=1700000000)
    _insert_thread_account(subcommand_db, "t-001", "acme-corp")
    with patch("fieldkit.commands.gmail.query.connect", return_value=subcommand_db):
        subcommand = runner.invoke(cli, ["account", "acme-corp", "--db", str(tmp_path / "subcommand.db")])

    option_db = _make_db()
    _insert_message(option_db, "t-001", "sales@acme-corp.com", date_epoch=1700000000)
    _insert_thread_account(option_db, "t-001", "acme-corp")
    with patch("fieldkit.commands.gmail.query.connect", return_value=option_db):
        option = runner.invoke(cli, ["--account", "acme-corp"])

    assert option.exit_code == 0
    assert option.output == subcommand.output


def test_query_without_selection_shows_help_before_database_access() -> None:
    with patch("fieldkit.commands.gmail.query.connect") as connect_mock:
        result = CliRunner().invoke(cli)

    assert result.exit_code == 2
    assert "Usage: query [OPTIONS] [COMMAND] [ARGS]..." in result.output
    connect_mock.assert_not_called()


def test_query_account_option_rejects_subcommand_before_database_access(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.__main__ import main

    with patch("fieldkit.commands.gmail.query.connect") as connect_mock:
        result = main(["gmail", "query", "--account", "acme-corp", "threads", "renewal"])

    assert result == 3
    assert capsys.readouterr().err == "Error: --account cannot be combined with a query subcommand\n"
    connect_mock.assert_not_called()


# ---------------------------------------------------------------------------
# cmd_threads_click
# ---------------------------------------------------------------------------


# ── TestCmdThreadsClick (flattened) ─────────────────────────────────────────


def test_cmd_threads_click_threads_no_match_shows_zero(tmp_path: Path) -> None:
    """threads command shows zero results for unknown keyword."""
    db = _make_db()
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["threads", "nonexistent-keyword", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "0 thread(s)" in result.output


def test_cmd_threads_click_threads_with_date_filter(tmp_path: Path) -> None:
    """threads command with --since applies date filter."""
    db = _make_db()
    _insert_message(db, "t-001", "sender@acme-corp.com", subject="OpenShift deal")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(
            cli,
            ["threads", "OpenShift", "--db", str(tmp_path / "fake.db"), "--since", "2023-01-01"],
        )

    assert result.exit_code == 0


def test_cmd_threads_click_threads_without_date_filter(tmp_path: Path) -> None:
    """threads command without date filter uses the else branch."""
    db = _make_db()
    _insert_message(db, "t-001", "sender@acme-corp.com", subject="OpenShift deal")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["threads", "OpenShift", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# cmd_dig_click
# ---------------------------------------------------------------------------


# ── TestCmdDigClick (flattened) ─────────────────────────────────────────────


def test_cmd_dig_click_dig_no_match_shows_zero(tmp_path: Path) -> None:
    """dig command shows zero results for unknown account/keyword."""
    db = _make_db()
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["dig", "nonexistent-account", "keyword", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "0 thread(s)" in result.output


def test_cmd_dig_click_dig_with_date_filter(tmp_path: Path) -> None:
    """dig command with --since applies date filter (date_clause branch)."""
    db = _make_db()
    _insert_message(db, "t-001", "sales@acme-corp.com", subject="Deal discussion", body_plain="Deal details.")
    _insert_thread_account(db, "t-001", "acme-corp")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(
            cli,
            ["dig", "acme-corp", "Deal", "--db", str(tmp_path / "fake.db"), "--since", "2023-01-01"],
        )

    assert result.exit_code == 0


def test_cmd_dig_click_dig_without_date_filter(tmp_path: Path) -> None:
    """dig command without date filter uses the else branch."""
    db = _make_db()
    _insert_message(db, "t-001", "sales@acme-corp.com", subject="Deal discussion", body_plain="Deal details.")
    _insert_thread_account(db, "t-001", "acme-corp")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["dig", "acme-corp", "Deal", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0


def test_cmd_dig_click_dig_with_results_shows_table(tmp_path: Path) -> None:
    """dig command with matching threads shows formatted table."""
    db = _make_db()
    _insert_message(
        db,
        "t-deal",
        "sales@acme-corp.com",
        subject="OpenShift deal",
        body_plain="OpenShift pricing discussion.",
        date_epoch=1700000000,
        date_str="2023-11-14",
    )
    _insert_thread_account(db, "t-deal", "acme-corp")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["dig", "acme-corp", "OpenShift", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# cmd_champion_click
# ---------------------------------------------------------------------------


# ── TestCmdChampionClick (flattened) ────────────────────────────────────────


def test_cmd_champion_click_champion_no_match_shows_not_found(tmp_path: Path) -> None:
    """champion command shows not-found when name not in DB."""
    db = _make_db()
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["champion", "Unknown Person", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "No people matched" in result.output


def test_cmd_champion_click_champion_with_match_shows_signal(tmp_path: Path) -> None:
    """champion command shows signal report when person is found."""
    db = _make_db()
    _insert_person(db, "carol@acme-corp.com", "Carol White")
    _insert_message(db, "t-001", "carol@acme-corp.com", subject="Carol's thread")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["champion", "Carol White", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "Signal:" in result.output


# ---------------------------------------------------------------------------
# cmd_blindspots_click
# ---------------------------------------------------------------------------


# ── TestCmdBlindspotsClick (flattened) ──────────────────────────────────────


def test_cmd_blindspots_click_blindspots_no_threads_for_account(tmp_path: Path) -> None:
    """blindspots command shows no-threads message for unknown account."""
    db = _make_db()
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["blindspots", "nonexistent-account", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "No threads found" in result.output


def test_cmd_blindspots_click_blindspots_with_external_contacts(tmp_path: Path) -> None:
    """blindspots command shows external contacts above min-messages threshold."""
    db = _make_db()
    # Insert multiple messages from an external contact
    for i in range(4):
        _insert_message(
            db,
            thread_id=f"t-{i}",
            from_addr="external@acme-corp.com",
            to_addr="me@acme-corp.com",
            date_epoch=1700000000 + i * 1000,
        )
        _insert_thread_account(db, f"t-{i}", "acme-corp")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(
            cli, ["blindspots", "acme-corp", "--db", str(tmp_path / "fake.db"), "--min-messages", "1"]
        )

    assert result.exit_code == 0


def test_cmd_blindspots_click_blindspots_with_known_filter(tmp_path: Path) -> None:
    """blindspots command with --known excludes matching contacts."""
    db = _make_db()
    for i in range(4):
        _insert_message(
            db,
            thread_id=f"t-{i}",
            from_addr="known@acme-corp.com",
            date_epoch=1700000000 + i * 1000,
        )
        _insert_thread_account(db, f"t-{i}", "acme-corp")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(
            cli,
            [
                "blindspots",
                "acme-corp",
                "--db",
                str(tmp_path / "fake.db"),
                "--min-messages",
                "1",
                "--known",
                "known@acme-corp.com",
            ],
        )

    assert result.exit_code == 0


def test_cmd_blindspots_click_blindspots_no_results_after_filter(tmp_path: Path) -> None:
    """blindspots command shows no-results message when all contacts filtered."""
    db = _make_db()
    # Insert only internal contacts (acme-corp.com is filtered)
    _insert_message(db, "t-1", "me@acme-corp.com", date_epoch=1700000000)
    _insert_thread_account(db, "t-1", "acme-corp")
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(
            cli, ["blindspots", "acme-corp", "--db", str(tmp_path / "fake.db"), "--min-messages", "1"]
        )

    assert result.exit_code == 0


def _add_blindspot_contact(db: sqlite3.Connection, email: str, *, thread_id: str, date_epoch: int) -> None:
    _insert_message(db, thread_id, email, date_epoch=date_epoch)
    _insert_person(db, email, email.split("@")[0])
    _insert_thread_account(db, thread_id, "acme-corp")


def test_cmd_blindspots_sets_aside_suspected_masked_address_by_default(tmp_path: Path) -> None:
    db = _make_db()
    _add_blindspot_contact(db, "alex.taylor@acme-corp.com", thread_id="ordinary", date_epoch=1_700_000_000)
    _add_blindspot_contact(db, "casey.morgan.q7zm@acme-corp.com", thread_id="suspected", date_epoch=1_700_001_000)
    config = {"accounts": {"acme-corp": {"domains": ["acme-corp.com"]}}}

    with (
        patch("fieldkit.commands.gmail.query.connect", return_value=db),
        patch("fieldkit.commands.gmail.query.get_accounts_config", return_value=config),
        patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]),
    ):
        result = CliRunner().invoke(
            cli, ["blindspots", "acme-corp", "--db", str(tmp_path / "fake.db"), "--min-messages", "1"]
        )

    assert result.exit_code == 0
    assert "alex.taylor@acme-corp.com" in result.output
    assert "casey.morgan.q7zm@acme-corp.com" not in result.output
    assert "Set aside 1 suspected masked address(es)" in result.output
    assert "--include-suspected" in result.output


def test_cmd_blindspots_include_suspected_marks_address(tmp_path: Path) -> None:
    db = _make_db()
    email = "casey.morgan.q7zm@acme-corp.com"
    _add_blindspot_contact(db, email, thread_id="suspected", date_epoch=1_700_001_000)
    config = {"accounts": {"acme-corp": {"domains": ["acme-corp.com"]}}}

    with (
        patch("fieldkit.commands.gmail.query.connect", return_value=db),
        patch("fieldkit.commands.gmail.query.get_accounts_config", return_value=config),
        patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "blindspots",
                "acme-corp",
                "--db",
                str(tmp_path / "fake.db"),
                "--min-messages",
                "1",
                "--include-suspected",
            ],
            terminal_width=180,
        )

    assert result.exit_code == 0
    assert "SUSPECTED:" in result.output
    assert "casey.morgan.q7zm" in result.output


def test_cmd_blindspots_json_preserves_suspect_without_consuming_limit(tmp_path: Path) -> None:
    db = _make_db()
    ordinary = "alex.taylor@acme-corp.com"
    suspected = "casey.morgan.q7zm@acme-corp.com"
    second_suspected = "jamie.river.abcd@acme-corp.com"
    _add_blindspot_contact(db, ordinary, thread_id="ordinary", date_epoch=1_700_000_000)
    _add_blindspot_contact(db, suspected, thread_id="suspected", date_epoch=1_700_001_000)
    _add_blindspot_contact(db, second_suspected, thread_id="second-suspected", date_epoch=1_700_002_000)
    config = {"accounts": {"acme-corp": {"domains": ["acme-corp.com"]}}}

    with (
        patch("fieldkit.commands.gmail.query.connect", return_value=db),
        patch("fieldkit.commands.gmail.query.get_accounts_config", return_value=config),
        patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "blindspots",
                "acme-corp",
                "--db",
                str(tmp_path / "fake.db"),
                "--min-messages",
                "1",
                "--limit",
                "1",
                "--json",
            ],
        )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["items"][0]["email"] == ordinary
    assert payload["count"] == 1
    assert [item["email"] for item in payload["suspected_items"]] == [second_suspected, suspected]
    assert all(item["quality_reason"] == "account-domain-four-character-suffix" for item in payload["suspected_items"])
    assert payload["suspected_count"] == 2
    assert payload["filters"]["include_suspected"] is False


def test_cmd_blindspots_missing_domains_preserves_matching_shape(tmp_path: Path) -> None:
    db = _make_db()
    email = "casey.morgan.q7zm@acme-corp.com"
    _add_blindspot_contact(db, email, thread_id="suspected", date_epoch=1_700_001_000)

    with (
        patch("fieldkit.commands.gmail.query.connect", return_value=db),
        patch("fieldkit.commands.gmail.query.get_accounts_config", return_value={}),
        patch("fieldkit.commands.gmail.query.get_internal_domains", return_value=[]),
    ):
        result = CliRunner().invoke(
            cli, ["blindspots", "acme-corp", "--db", str(tmp_path / "fake.db"), "--min-messages", "1"]
        )

    assert result.exit_code == 0
    assert email in result.output
    assert "Set aside" not in result.output


def test_cmd_blindspots_empty_json_keeps_suspected_metadata_schema(tmp_path: Path) -> None:
    db = _make_db()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = CliRunner().invoke(
            cli,
            ["blindspots", "nonexistent-account", "--db", str(tmp_path / "fake.db"), "--json"],
        )

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["items"] == []
    assert payload["suspected_items"] == []
    assert payload["suspected_count"] == 0


# ---------------------------------------------------------------------------
# cmd_context_click
# ---------------------------------------------------------------------------


# ── TestCmdContextClick (flattened) ─────────────────────────────────────────


def test_cmd_context_click_context_no_match_shows_not_found(tmp_path: Path) -> None:
    """context command shows not-found when name not in DB."""
    db = _make_db()
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["context", "Unknown Person", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "No people matched" in result.output


def test_cmd_context_click_context_with_match_shows_threads(tmp_path: Path) -> None:
    """context command shows thread context when person is found."""
    db = _make_db()
    _insert_person(db, "dave@acme-corp.com", "Dave Brown")
    _insert_message(
        db,
        "t-001",
        "dave@acme-corp.com",
        subject="Dave's thread",
        body_plain="Some context here.",
    )
    runner = CliRunner()

    with patch("fieldkit.commands.gmail.query.connect", return_value=db):
        result = runner.invoke(cli, ["context", "Dave Brown", "--db", str(tmp_path / "fake.db")])

    assert result.exit_code == 0
    assert "Dave" in result.output or "dave" in result.output
