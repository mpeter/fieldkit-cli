"""Tests for `fieldkit contact list` — a pure read of the people-index cache.

`contact list` never rebuilds the cache; a missing `people` table produces a
clean error pointing at `fieldkit sync`, not a rebuild-on-read.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.contact.list_cmd import cli as contact_list_cli

pytestmark = pytest.mark.unit


def _make_db_with_people(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE people (
            email TEXT PRIMARY KEY,
            display_name TEXT,
            first_seen TEXT,
            last_seen TEXT,
            message_count INTEGER,
            thread_count INTEGER,
            initiated_count INTEGER,
            domain TEXT,
            account TEXT,
            is_internal INTEGER,
            meeting_count INTEGER
        )"""
    )
    conn.executemany(
        "INSERT INTO people VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _row(email: str, name: str, account: str, last_seen: str = "2026-01-01") -> tuple:
    return (email, name, "2025-01-01", last_seen, 3, 2, 1, email.split("@")[1], account, 0, 0)


def test_contact_list_reads_existing_cache_without_rebuilding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """contact list prints cached people and never triggers a cache rebuild."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(db_path, [_row("alice@acme-corp.com", "Alice", "acme-corp")])

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, [])

    assert result.exit_code == 0, result.output
    assert "alice@acme-corp.com" in result.output
    assert "Alice" in result.output


def test_contact_list_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--json emits a JSON array of people records."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(db_path, [_row("bob@acme-corp.com", "Bob", "acme-corp")])

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, ["--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["email"] == "bob@acme-corp.com"


def test_contact_list_account_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--account restricts output to the matching account slug."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(
        db_path,
        [
            _row("alice@acme-corp.com", "Alice", "acme-corp"),
            _row("carol@globalpay.example.com", "Carol", "globalpay"),
        ],
    )

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, ["--account", "acme-corp", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    emails = {p["email"] for p in data}
    assert emails == {"alice@acme-corp.com"}


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
    """A missing `people` table (cache never built) errors cleanly pointing at `fieldkit sync`.

    This is the load-bearing "pure read, no silent rebuild" contract: contact list
    must not attempt to build the people index itself.
    """
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, [])

    assert result.exit_code == 1
    assert "fieldkit sync" in result.output


def test_contact_list_limit_restricts_row_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--limit caps the number of rows returned."""
    db_path = tmp_path / "gmail.db"
    _make_db_with_people(
        db_path,
        [
            _row("alice@acme-corp.com", "Alice", "acme-corp", last_seen="2026-01-03"),
            _row("bob@acme-corp.com", "Bob", "acme-corp", last_seen="2026-01-02"),
            _row("carol@globalpay.example.com", "Carol", "globalpay", last_seen="2026-01-01"),
        ],
    )

    monkeypatch.setattr("fieldkit.commands.contact.list_cmd.get_gmail_db_path", lambda: db_path)

    runner = CliRunner()
    result = runner.invoke(contact_list_cli, ["--limit", "2", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 2
