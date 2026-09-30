"""CLI regressions for bounded reads from the published Gmail cache."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import main
from fieldkit.commands.gmail import decay as command
from fieldkit.gmail import query_domain
from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page, initialize_gmail_publication
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit

_CONFIG: dict[str, object] = {
    "accounts": {"acme-corp": {"domains": ["acme-corp.example.com"]}},
    "internal_domains": ["internal.example.com"],
}


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    def configured(*, strict: bool = False) -> dict[str, object]:
        assert strict is True
        return _CONFIG

    monkeypatch.setattr(command, "get_accounts_config", configured)


def _published_database(tmp_path: Path, *, messages: int = 1) -> Path:
    database = tmp_path / "gmail.db"
    initialize_gmail_publication(database)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    old_epoch = int((now - timedelta(days=120)).timestamp())

    def seed(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT INTO threads(thread_id, subject, message_count, updated_at) VALUES ('ta', '', ?, '')",
            (messages,),
        )
        connection.execute("INSERT INTO thread_accounts VALUES ('ta', 'acme-corp')")
        connection.executemany(
            """
            INSERT INTO messages(message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch)
            VALUES (?, 'ta', ?, 'me@internal.example.com', '', ?)
            """,
            ((f"m{index}", f"buyer{index}@acme-corp.example.com", old_epoch) for index in range(messages)),
        )
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(database, seed)
    return database


def test_decay_help_has_only_required_account_option() -> None:
    result = CliRunner().invoke(command.cli, ["--help"])

    assert result.exit_code == 0
    assert "--account" in result.output
    assert "[ACCOUNT]" not in result.output
    assert "tools/gmail-cache" not in result.output


@pytest.mark.parametrize("argv", [[], ["acme-corp"]])
def test_decay_rejects_missing_or_legacy_positional_account(argv: list[str]) -> None:
    assert main(["gmail", "decay", *argv]) == 3


def test_decay_json_reads_actual_cold_contact_from_ready_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _published_database(tmp_path)
    _configure(monkeypatch)

    exit_code = main(["gmail", "decay", "--account", "acme-corp", "--limit", "10", "--db", str(database), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["contacts"][0]["email"] == "buyer0@acme-corp.example.com"
    assert payload["contacts"][0]["signal"] == "COLD"
    assert payload["scan_truncated"] is False
    assert payload["filters"]["limit"] == 10


def test_decay_explicit_domain_filter_applies_with_all_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _published_database(tmp_path)

    def add_subsidiary(connection: SQLiteMutationConnection) -> None:
        recent_epoch = int(datetime(2026, 9, 20, tzinfo=UTC).timestamp())
        connection.execute(
            """
            INSERT INTO messages(message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch)
            VALUES ('m-subsidiary', 'ta', 'buyer@subsidiary.example.com',
                    'me@internal.example.com', '', ?)
            """,
            (recent_epoch,),
        )

    apply_gmail_page(database, add_subsidiary)
    _configure(monkeypatch)

    exit_code = main(
        [
            "gmail",
            "decay",
            "--account",
            "acme-corp",
            "--all",
            "--domain",
            "subsidiary.example.com",
            "--max-age-days",
            "30",
            "--db",
            str(database),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert [contact["email"] for contact in payload["contacts"]] == ["buyer@subsidiary.example.com"]


def test_decay_refuses_raw_cache_without_mutation_or_private_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "fictional-private-marker.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE marker (value INTEGER)")
    connection.commit()
    connection.close()
    before = (database.read_bytes(), database.stat().st_mtime_ns, {path.name for path in tmp_path.iterdir()})
    _configure(monkeypatch)

    exit_code = main(["gmail", "decay", "--account", "acme-corp", "--db", str(database), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.out == ""
    assert "SQLite snapshot could not be verified" in captured.err
    assert str(tmp_path) not in captured.err
    after = (database.read_bytes(), database.stat().st_mtime_ns, {path.name for path in tmp_path.iterdir()})
    assert after == before


def test_decay_missing_cache_is_retryable_and_does_not_create_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "missing.db"
    _configure(monkeypatch)

    exit_code = main(["gmail", "decay", "--account", "acme-corp", "--db", str(database), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert str(tmp_path) not in captured.err
    assert list(tmp_path.iterdir()) == []


def test_decay_incomplete_scan_emits_one_report_then_returns_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _published_database(tmp_path, messages=110)
    _configure(monkeypatch)

    exit_code = main(["gmail", "decay", "--account", "acme-corp", "--limit", "1", "--db", str(database), "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["scan_truncated"] is True
    assert payload["scanned_rows"] <= 100
    assert str(tmp_path) not in captured.err


def test_decay_missing_consumed_schema_column_is_fixed_data_failure(
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
            thread_id TEXT, from_addr TEXT, to_addr TEXT, cc_addr TEXT,
            subject TEXT, date_str TEXT, date_epoch INTEGER, body_plain TEXT
        );
        CREATE TABLE people (
            email TEXT, display_name TEXT, message_count INTEGER,
            thread_count INTEGER, meeting_count INTEGER,
            slack_message_count INTEGER, last_seen TEXT,
            is_internal INTEGER, account TEXT
        );
        CREATE TABLE thread_accounts (thread_id TEXT, account TEXT);
        CREATE TABLE sync_state (key TEXT, value TEXT);
        """
    )
    monkeypatch.setattr(query_domain, "connect_read_only", lambda _path: connection)
    _configure(monkeypatch)
    private_path = tmp_path / "fictional-private-marker.db"

    exit_code = main(["gmail", "decay", "--account", "acme-corp", "--db", str(private_path), "--json"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.out == ""
    assert "SQLite snapshot could not be verified" in captured.err
    assert str(private_path) not in captured.err
    assert "message_id" not in captured.err
