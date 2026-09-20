"""CLI-layer smoke tests for fieldkit gmail decay (commands/gmail/decay.py).

implementation note: new test file targeting the CLI layer of the decay command, which had
~44% coverage. Domain logic is tested in test_decay.py; this file covers the
Click command interface and error paths.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def _get_cli():
    from fieldkit.commands.gmail.decay import cli

    return cli


def _make_gmail_db(tmp_path: Path) -> Path:
    """Create a minimal gmail.db with the contacts table."""
    db = tmp_path / "gmail.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE contacts (
            email TEXT PRIMARY KEY,
            name TEXT,
            last_seen TEXT,
            message_count INTEGER DEFAULT 0,
            domain TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return db


def test_decay_cli_help_exits_zero() -> None:
    """fieldkit gmail decay --help exits 0."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_decay_cli_no_db_exits_nonzero(tmp_path: Path) -> None:
    """decay exits non-zero when the specified db path does not exist."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--db", str(tmp_path / "missing.db"), "acme-corp"])
    assert result.exit_code != 0


def test_decay_cli_missing_account_shows_error(tmp_path: Path) -> None:
    """decay without an account name exits 2 with a helpful error message."""
    db_path = _make_gmail_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--db", str(db_path)])
    assert result.exit_code == 2
    assert "account" in result.output.lower()


def test_decay_cli_missing_table_shows_guidance(tmp_path: Path) -> None:
    """decay with a DB missing thread_accounts exits 1 with guidance."""
    db_path = _make_gmail_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--db", str(db_path), "--all", "acme-corp"])
    assert result.exit_code == 1
    assert "gmail people" in result.output or "thread_accounts" in result.output


def test_decay_cli_json_emits_single_empty_report(tmp_path: Path) -> None:
    db_path = _make_gmail_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE thread_accounts (thread_id TEXT, account TEXT)")
    conn.commit()
    conn.close()

    result = CliRunner().invoke(_get_cli(), ["--db", str(db_path), "--json", "acme-corp"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["account"] == "acme-corp"
    assert payload["contacts"] == []
    assert isinstance(payload["as_of"], str)
