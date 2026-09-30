"""Tests for bounded Gmail-derived CRM-review candidates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import main
from fieldkit.commands.gmail import backstory_gap as command
from fieldkit.config import ConfigError
from fieldkit.gmail import backstory_gap, query_domain, query_support
from fieldkit.gmail.publication import (
    GMAIL_QUERY_READY_KEY,
    apply_gmail_page,
    initialize_gmail_publication,
)
from fieldkit.gmail.query_domain import connect
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit

_CONFIG: dict[str, object] = {
    "accounts": {
        "acme-corp": {
            "domains": ["acme-corp.example.com"],
            "blindspots_min_messages": 2,
        }
    }
}


def _published_database(tmp_path: Path, rows: list[dict[str, Any]] | None = None) -> Path:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)

    def seed(connection: SQLiteMutationConnection) -> None:
        for row in rows or []:
            connection.execute(
                """
                INSERT INTO people (
                    email, display_name, first_seen, last_seen, message_count,
                    thread_count, initiated_count, domain, account, is_internal,
                    meeting_count, slack_message_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["email"],
                    row.get("display_name", ""),
                    "2026-01-01",
                    row.get("last_seen", "2026-01-02"),
                    row.get("message_count", 0),
                    row.get("thread_count", 1),
                    0,
                    str(row["email"]).rsplit("@", 1)[-1],
                    row.get("account", "acme-corp"),
                    row.get("is_internal", 0),
                    row.get("meeting_count", 0),
                    row.get("slack_message_count", 0),
                ),
            )
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(database, seed)
    return database


def _raw_database(tmp_path: Path) -> Path:
    database = tmp_path / "raw.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE people (email TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    return database


def _configure(monkeypatch: pytest.MonkeyPatch, config: dict[str, object] = _CONFIG) -> None:
    def configured(*, strict: bool = False) -> dict[str, object]:
        assert strict is True
        return config

    monkeypatch.setattr(command, "get_accounts_config", configured)


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("noreply@acme-corp.example.com", True),
        ("mailer-daemon@acme-corp.example.com", True),
        ("alerts@acme-corp.example.com", True),
        ("alice@acme-corp.example.com", False),
    ],
)
def test_noise_classification(email: str, expected: bool) -> None:
    assert backstory_gap.is_noise(email) is expected


def test_query_candidates_filters_and_orders_across_accounts(tmp_path: Path) -> None:
    config: dict[str, object] = {
        "accounts": {
            "acme-corp": {"domains": ["acme-corp.example.com"], "blindspots_min_messages": 2},
            "beta-co": {"domains": ["beta.example.com"], "blindspots_min_messages": 2},
        }
    }
    database = _published_database(
        tmp_path,
        [
            {"email": "low@acme-corp.example.com", "message_count": 1},
            {"email": "internal@acme-corp.example.com", "message_count": 99, "is_internal": 1},
            {"email": "noreply@acme-corp.example.com", "message_count": 90},
            {"email": "zara@acme-corp.example.com", "message_count": 10},
            {"email": "alice@acme-corp.example.com", "message_count": 10},
            {
                "email": "casey@beta.example.com",
                "account": "beta-co",
                "message_count": 20,
            },
        ],
    )
    scopes = backstory_gap.account_scopes(config, account=None, min_messages=None)

    with connect(database) as connection:
        report = backstory_gap.query_candidates(connection, scopes, limit=1)

    assert report.items[0].email == "casey@beta.example.com"
    assert report.truncated is True


def test_noise_rows_do_not_consume_the_limit(tmp_path: Path) -> None:
    database = _published_database(
        tmp_path,
        [
            {"email": "noreply@acme-corp.example.com", "message_count": 100},
            {"email": "alerts@acme-corp.example.com", "message_count": 99},
            {"email": "alice@acme-corp.example.com", "message_count": 8},
        ],
    )
    scopes = backstory_gap.account_scopes(_CONFIG, account=None, min_messages=None)

    with connect(database) as connection:
        report = backstory_gap.query_candidates(connection, scopes, limit=1)

    assert tuple(item.email for item in report.items) == ("alice@acme-corp.example.com",)
    assert report.truncated is False


def test_noise_only_scan_has_row_and_sqlite_work_bounds() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE people (
            email TEXT, display_name TEXT, message_count INTEGER,
            thread_count INTEGER, meeting_count INTEGER,
            slack_message_count INTEGER, last_seen TEXT,
            is_internal INTEGER, account TEXT
        )
        """
    )
    connection.executemany(
        "INSERT INTO people VALUES (?, '', ?, 1, 0, 0, '2026-01-01', 0, 'acme-corp')",
        [(f"noreply-{index}@acme-corp.example.com", 20_000 - index) for index in range(10_000)],
    )
    scopes = backstory_gap.account_scopes(_CONFIG, account=None, min_messages=None)

    report = backstory_gap.query_candidates(connection, scopes, limit=1)

    assert report.items == ()
    assert report.scan_truncated is True
    assert report.scanned_rows <= query_support.scan_row_budget(1)
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    connection.close()


def test_zero_row_account_scopes_have_a_fixed_query_budget() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE people (
            email TEXT, display_name TEXT, message_count INTEGER,
            thread_count INTEGER, meeting_count INTEGER,
            slack_message_count INTEGER, last_seen TEXT,
            is_internal INTEGER, account TEXT
        )
        """
    )
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    config = {
        "accounts": {
            f"account-{index}": {
                "domains": [f"account-{index}.example.com"],
                "blindspots_min_messages": 1,
            }
            for index in range(query_support.scan_row_budget(1) + 1)
        }
    }
    scopes = backstory_gap.account_scopes(config, account=None, min_messages=None)

    report = backstory_gap.query_candidates(connection, scopes, limit=1)
    connection.close()

    people_queries = [statement for statement in statements if "FROM people" in statement]
    assert report.items == ()
    assert report.scan_truncated is True
    assert len(people_queries) == query_support.scan_row_budget(1)


@pytest.mark.parametrize(
    ("config", "account", "min_messages"),
    [
        ({}, None, None),
        ({"accounts": []}, None, None),
        ({"accounts": {"acme-corp": {"domains": []}}}, None, None),
        ({"accounts": {"bad_key": {"domains": ["acme-corp.example.com"]}}}, None, None),
        (_CONFIG, "unknown", None),
        (_CONFIG, None, -1),
    ],
)
def test_account_scopes_reject_invalid_configuration(
    config: dict[str, object], account: str | None, min_messages: int | None
) -> None:
    with pytest.raises(ConfigError, match=r"account|threshold|domain"):
        backstory_gap.account_scopes(config, account=account, min_messages=min_messages)


def test_cli_reads_ready_publication_and_states_comparison_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _published_database(
        tmp_path,
        [{"email": "alice@acme-corp.example.com", "display_name": "Alice", "message_count": 9}],
    )
    _configure(monkeypatch)

    result = CliRunner().invoke(command.cli, ["--db", str(database), "--limit", "1", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["items"][0]["email"] == "alice@acme-corp.example.com"
    assert payload["count"] == 1
    assert payload["scanned_rows"] == 1
    assert payload["scan_truncated"] is False
    assert payload["filters"]["limit"] == 1
    assert payload["comparison"] == {
        "crm": "not_performed",
        "result_kind": "review_candidates",
    }


def test_human_output_is_bounded_truthful_and_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _published_database(
        tmp_path,
        [
            {
                "email": "alice@acme-corp.example.com",
                "display_name": "<img src=x onerror=alert(1)> [click](https://example.com)|Admin\n\x1b[31m",
                "message_count": 9,
            },
            {"email": "bob@acme-corp.example.com", "message_count": 8},
        ],
    )
    _configure(monkeypatch)

    result = CliRunner().invoke(command.cli, ["--db", str(database), "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "CRM comparison was not performed" in result.output
    assert "Results truncated at --limit 1" in result.output
    assert "<img" not in result.output
    assert "[click](" not in result.output
    assert "\\[click\\]\\(" in result.output
    assert "\\|Admin" in result.output
    assert "\x1b" not in result.output
    assert "not tracked" not in result.output


@pytest.mark.parametrize("exhaustion", ["rows", "sqlite"])
def test_incomplete_cli_report_is_canonical_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exhaustion: str,
) -> None:
    rows = [{"email": f"noreply-{index}@acme-corp.example.com", "message_count": 1_000 - index} for index in range(101)]
    if exhaustion == "sqlite":
        rows = [{"email": "alice@acme-corp.example.com", "message_count": 9}]
        monkeypatch.setattr(query_support, "MAX_SQLITE_STEPS", 0)
        monkeypatch.setattr(query_support, "SQLITE_PROGRESS_INTERVAL", 1)
    database = _published_database(tmp_path, rows)
    _configure(monkeypatch)

    result = CliRunner().invoke(command.cli, ["--db", str(database), "--limit", "1", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["scan_truncated"] is True
    assert payload["count"] == 0


def test_cli_refuses_unpublished_cache_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _raw_database(tmp_path)
    _configure(monkeypatch)
    before = (
        database.read_bytes(),
        database.stat().st_mtime_ns,
        sorted(path.name for path in tmp_path.iterdir()),
    )

    exit_code = main(["gmail", "backstory-gap", "--db", str(database), "--json"])

    after = (
        database.read_bytes(),
        database.stat().st_mtime_ns,
        sorted(path.name for path in tmp_path.iterdir()),
    )
    captured = capsys.readouterr()
    assert exit_code == 3
    assert "SQLite snapshot could not be verified" in captured.err
    assert str(database) not in captured.out + captured.err
    assert after == before


def test_backstory_missing_consumed_schema_column_is_fixed_data_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE threads (
            thread_id TEXT, subject TEXT, message_count INTEGER, updated_at TEXT
        );
        CREATE TABLE messages (
            message_id TEXT, thread_id TEXT, from_addr TEXT, to_addr TEXT,
            cc_addr TEXT, subject TEXT, date_str TEXT, date_epoch INTEGER,
            body_plain TEXT
        );
        CREATE TABLE people (
            email TEXT, display_name TEXT, message_count INTEGER,
            thread_count INTEGER, meeting_count INTEGER,
            slack_message_count INTEGER, is_internal INTEGER, account TEXT
        );
        CREATE TABLE thread_accounts (thread_id TEXT, account TEXT);
        CREATE TABLE sync_state (key TEXT, value TEXT);
        """
    )
    monkeypatch.setattr(query_domain, "connect_read_only", lambda _path: connection)
    _configure(monkeypatch)
    private_path = tmp_path / "fictional-private-marker.db"

    exit_code = main(["gmail", "backstory-gap", "--db", str(private_path), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.out == ""
    assert "SQLite snapshot could not be verified" in captured.err
    assert str(private_path) not in captured.err
    assert "last_seen" not in captured.err


@pytest.mark.parametrize(
    "row",
    [
        {"email": "@acme-corp.example.com", "message_count": 9},
        {"email": "alice@acme-corp.example.com", "last_seen": "2026-99-99private", "message_count": 9},
    ],
)
def test_backstory_corrupt_contact_identity_or_date_is_fixed_data_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    row: dict[str, object],
) -> None:
    database = _published_database(tmp_path, [row])
    _configure(monkeypatch)

    exit_code = main(["gmail", "backstory-gap", "--db", str(database), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.out == ""
    assert "SQLite snapshot could not be verified" in captured.err
    assert "2026-99-99private" not in captured.err
    assert "@acme-corp.example.com" not in captured.err


def test_missing_database_diagnostic_does_not_reveal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    private_path = tmp_path / "fictional-private-marker.db"
    _configure(monkeypatch)

    exit_code = main(["gmail", "backstory-gap", "--db", str(private_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert str(private_path) not in captured.out + captured.err


@pytest.mark.parametrize("value", ["0", "501", "not-a-number"])
def test_cli_rejects_invalid_limits(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["gmail", "backstory-gap", "--limit", value, "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert payload == {"outcome": "invalid", "error": "invalid_usage", "exit_code": 3}
