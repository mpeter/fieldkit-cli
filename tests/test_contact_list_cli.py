"""Tests for `fieldkit contact list` — a pure read of the people-index cache.

`contact list` never rebuilds the cache; a missing `people` table produces a
clean error pointing at `fieldkit sync`, not a rebuild-on-read.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from fieldkit.commands.contact.list_cmd import cli as contact_list_cli
from fieldkit.errors import SQLiteSnapshotError
from fieldkit.gmail.publication import (
    GMAIL_QUERY_READY_KEY,
    apply_gmail_page,
    initialize_gmail_publication,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit


def _make_db_with_people(path: Path, rows: list[tuple]) -> None:
    initialize_gmail_publication(path)

    def publish(connection: SQLiteMutationConnection) -> None:
        connection.executemany(
            """
            INSERT INTO people(
                email, display_name, first_seen, last_seen, message_count,
                thread_count, initiated_count, domain, account, is_internal,
                meeting_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(path, publish)


def _row(email: str, name: str, account: str, last_seen: str = "2026-01-01") -> tuple:
    return (email, name, "2025-01-01", last_seen, 3, 2, 1, email.split("@")[1], account, 0, 0)


def test_contact_list_reads_existing_cache_without_rebuilding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """contact list prints cached people and never triggers a cache rebuild."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(db_path, [_row("alice@acme-corp.example.com", "Alice", "acme-corp")])

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, [])

    assert result.exit_code == 0, result.output
    assert "alice@acme-corp.example.com" in result.output
    assert "Alice" in result.output


def test_contact_list_active_cache_is_retryable_without_private_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_detail = "/fictional-private/operator/private-customer/gmail.db"
    monkeypatch.setattr(
        "fieldkit.commands.contact.list_cmd.list_people",
        MagicMock(side_effect=SQLiteSnapshotError(private_detail, reason="active")),
    )

    result = CliRunner().invoke(contact_list_cli, [])

    assert result.exit_code == 1
    assert private_detail not in result.output


def test_contact_list_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--json emits a JSON array of people records."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(db_path, [_row("bob@acme-corp.example.com", "Bob", "acme-corp")])

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, ["--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["email"] == "bob@acme-corp.example.com"


def test_contact_list_account_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--account restricts output to the matching account slug."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(
        db_path,
        [
            _row("alice@acme-corp.example.com", "Alice", "acme-corp"),
            _row("carol@globalpay.example.com", "Carol", "globalpay"),
        ],
    )

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, ["--account", "acme-corp", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    emails = {p["email"] for p in data}
    assert emails == {"alice@acme-corp.example.com"}


def test_contact_list_empty_cache_prints_no_people_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty (but present) people table prints a friendly message, not an error."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(db_path, [])

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, [])

    assert result.exit_code == 0, result.output
    assert "No people found." in result.output


def test_contact_list_missing_people_table_gives_clean_error_not_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpublished cache errors cleanly and does not rebuild itself.

    This is the load-bearing "pure read, no silent rebuild" contract: contact list
    must not attempt to build the people index itself.
    """
    db_path = tmp_path / "gmail.db"
    db_path.write_bytes(b"not-a-published-cache")
    before = db_path.read_bytes()

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, [])

    assert result.exit_code == 3
    assert "could not be verified" in result.output
    assert db_path.read_bytes() == before


def test_contact_list_missing_database_does_not_create_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "gmail.db"
    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    result = CliRunner().invoke(contact_list_cli, [])

    assert result.exit_code == 1
    assert "fieldkit gmail sync" in result.output
    assert not db_path.exists()


def test_contact_list_limit_restricts_row_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--limit caps the number of rows returned."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(
        db_path,
        [
            _row("alice@acme-corp.example.com", "Alice", "acme-corp", last_seen="2026-01-03"),
            _row("bob@acme-corp.example.com", "Bob", "acme-corp", last_seen="2026-01-02"),
            _row("carol@globalpay.example.com", "Carol", "globalpay", last_seen="2026-01-01"),
        ],
    )

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, ["--limit", "2", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 2
