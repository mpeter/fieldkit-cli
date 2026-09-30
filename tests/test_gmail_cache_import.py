"""Explicit logical import of an existing Gmail cache."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.gmail import cache_import
from fieldkit.gmail.cache_import import import_gmail_cache, preview_gmail_cache_import
from fieldkit.gmail.publication import (
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
    publication_root_for,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection, SQLitePublicationError, SQLitePublicationReceipt
from fieldkit.sqlite_read import open_sqlite_read_only

pytestmark = pytest.mark.unit


def test_import_refuses_generation_published_after_real_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    published: dict[str, bytes] = {}

    def initialize_then_publish(path: Path) -> SQLitePublicationReceipt:
        receipt = initialize_gmail_publication(path)

        def concurrent_mutation(connection: SQLiteMutationConnection) -> None:
            connection.execute("INSERT INTO threads(thread_id, subject) VALUES ('concurrent-thread', 'Concurrent')")

        concurrent = apply_gmail_page(path, concurrent_mutation)
        assert concurrent.generation == receipt.generation + 1
        root = publication_root_for(path)
        published.update({name: (root / name).read_bytes() for name in ("state.json", "receipt.json")})
        published["source"] = path.read_bytes()
        return receipt

    monkeypatch.setattr(cache_import, "initialize_gmail_publication", initialize_then_publish)

    with pytest.raises(SQLitePublicationError, match="changed before conditional update"):
        import_gmail_cache(source, target)

    root = publication_root_for(target)
    assert {name: (root / name).read_bytes() for name in ("state.json", "receipt.json")} == {
        name: published[name] for name in ("state.json", "receipt.json")
    }
    assert target.read_bytes() == published["source"]
    with open_gmail_publication(target) as connection:
        assert [row[0] for row in connection.execute("SELECT thread_id FROM threads")] == ["concurrent-thread"]


def _legacy_cache(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY, subject TEXT, snippet TEXT,
            message_count INTEGER DEFAULT 0, updated_at TEXT
        );
        CREATE TABLE messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES threads(thread_id),
            from_addr TEXT, to_addr TEXT, cc_addr TEXT, subject TEXT,
            date_str TEXT, date_epoch INTEGER, labels TEXT, body_plain TEXT DEFAULT '',
            body_html TEXT DEFAULT '', size_bytes INTEGER DEFAULT 0, snippet TEXT,
            synced_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE attachments (
            attachment_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages(message_id),
            filename TEXT, mime_type TEXT, size_bytes INTEGER DEFAULT 0, part_id TEXT
        );
        CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE labels (
            label_id TEXT PRIMARY KEY, label_name TEXT UNIQUE,
            synced_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE people (
            email TEXT PRIMARY KEY, display_name TEXT, first_seen TEXT, last_seen TEXT,
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE calendar_events (
            event_id TEXT PRIMARY KEY, summary TEXT, start_time TEXT, end_time TEXT,
            organizer_email TEXT, attendees TEXT, location TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE slack_activity (
            channel_id TEXT NOT NULL, channel_name TEXT, account TEXT NOT NULL,
            message_count INTEGER DEFAULT 0, last_activity TEXT, synced_at TEXT NOT NULL,
            PRIMARY KEY (channel_id, account)
        );
        CREATE TABLE thread_accounts (
            thread_id TEXT NOT NULL REFERENCES threads(thread_id), account TEXT NOT NULL,
            PRIMARY KEY (thread_id, account)
        );
        INSERT INTO threads VALUES ('thread-1', 'Planning', '', 1, '2026-09-01');
        INSERT INTO messages(
            message_id, thread_id, from_addr, to_addr, subject, date_str, date_epoch, body_plain
        ) VALUES (
            'message-1', 'thread-1', 'alice@example.com', 'bob@acme-corp.example.com',
            'Planning', '2026-09-01', 1788220800, 'Fictional fixture'
        );
        INSERT INTO people VALUES (
            'alice@example.com', 'Alice Example', '2026-09-01', '2026-09-01', 1
        );
        INSERT INTO calendar_events(
            event_id, summary, organizer_email, attendees, created_at
        ) VALUES (
            'event-1', 'Planning', 'alice@example.com', '[]', '2026-09-01T12:00:00Z'
        );
        INSERT INTO slack_activity VALUES (
            'channel-1', 'acme-public', 'acme', 3, '2026-09-01T13:00:00Z',
            '2026-09-01T13:05:00Z'
        );
        INSERT INTO thread_accounts VALUES ('thread-1', 'acme');
        """
    )
    connection.commit()
    connection.close()


def test_import_preserves_known_co_resident_tables_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    source_before = source.read_bytes()

    result = import_gmail_cache(source, target)

    assert result.generation == 2
    assert result.table_counts == {
        "calendar_events": 1,
        "messages": 1,
        "people": 1,
        "slack_activity": 1,
        "thread_accounts": 1,
        "threads": 1,
    }
    assert source.read_bytes() == source_before
    with open_gmail_publication(target) as connection:
        assert connection.execute("SELECT body_plain FROM messages").fetchone()[0] == "Fictional fixture"
        person = connection.execute(
            "SELECT email, display_name, message_count, slack_message_count FROM people"
        ).fetchone()
        assert tuple(person) == ("alice@example.com", "Alice Example", 1, 0)
        assert connection.execute("SELECT summary FROM calendar_events").fetchone()[0] == "Planning"
        assert connection.execute("SELECT message_count FROM slack_activity").fetchone()[0] == 3
        assert connection.execute("SELECT account FROM thread_accounts").fetchone()[0] == "acme"


def test_preview_validates_every_row_without_creating_a_target(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    source_before = source.read_bytes()

    result = preview_gmail_cache_import(source, target)

    assert result.table_counts == {
        "calendar_events": 1,
        "messages": 1,
        "people": 1,
        "slack_activity": 1,
        "thread_accounts": 1,
        "threads": 1,
    }
    assert source.read_bytes() == source_before
    assert not target.exists()
    assert not publication_root_for(target).exists()


def test_preview_rejects_an_existing_actual_target_without_reading_it(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    target.write_bytes(b"existing target")
    target_before = target.read_bytes()

    with pytest.raises(SQLiteSnapshotError, match="target is not fresh"):
        preview_gmail_cache_import(source, target)

    assert target.read_bytes() == target_before


@pytest.mark.parametrize("redirected", [False, True])
def test_preview_rejects_unstable_target_parent_without_creating_it(
    tmp_path: Path,
    redirected: bool,
) -> None:
    source = tmp_path / "legacy.db"
    _legacy_cache(source)
    target_parent = tmp_path / "missing"
    if redirected:
        real_parent = tmp_path / "real"
        real_parent.mkdir()
        target_parent.symlink_to(real_parent, target_is_directory=True)
    target = target_parent / "gmail.db"

    with pytest.raises(SQLiteSnapshotError, match="target parent is not stable"):
        preview_gmail_cache_import(source, target)

    assert not target.exists()
    assert not publication_root_for(target).exists()


def test_preview_rejects_target_created_during_validation_without_changing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    original_hash = cache_import._hash_source_files
    calls = 0

    def create_target_before_final_validation(
        path: Path,
        *,
        deadline: float,
        max_bytes: int,
    ) -> dict[str, tuple[int, str]]:
        nonlocal calls
        result = original_hash(path, deadline=deadline, max_bytes=max_bytes)
        calls += 1
        if calls == 2:
            target.write_bytes(b"concurrent target")
        return result

    monkeypatch.setattr(cache_import, "_hash_source_files", create_target_before_final_validation)

    with pytest.raises(SQLiteSnapshotError, match="target is not fresh"):
        preview_gmail_cache_import(source, target)

    assert target.read_bytes() == b"concurrent target"
    assert not publication_root_for(target).exists()


def test_import_does_not_remove_target_created_by_initialization_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"

    @contextmanager
    def validated_source(
        _source: Path,
        *,
        deadline: float,
        max_bytes: int,
    ) -> Iterator[tuple[sqlite3.Connection, dict[str, tuple[object, ...]], dict[str, tuple[int, str]]]]:
        del deadline, max_bytes
        connection = sqlite3.connect(":memory:")
        try:
            yield connection, {}, {}
        finally:
            connection.close()

    def lose_initialization_race(_target: Path) -> None:
        target.write_bytes(b"concurrent target")
        raise SQLiteSnapshotError("Managed target already exists", reason="unverified")

    monkeypatch.setattr(cache_import, "_validated_source", validated_source)
    monkeypatch.setattr(cache_import, "initialize_gmail_publication", lose_initialization_race)

    with pytest.raises(SQLiteSnapshotError, match="already exists"):
        import_gmail_cache(source, target)

    assert target.read_bytes() == b"concurrent target"
    assert not publication_root_for(target).exists()


def test_import_retains_failed_initialized_publication_as_nonpassing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    source_before = source.read_bytes()

    def fail_scan(*_args: object, **_kwargs: object) -> dict[str, int]:
        raise GmailSyncPartialError("Legacy Gmail cache import exceeded its resource bound")

    monkeypatch.setattr(cache_import, "_scan_source_tables", fail_scan)

    with pytest.raises(GmailSyncPartialError, match="resource bound"):
        import_gmail_cache(source, target)

    assert source.read_bytes() == source_before
    assert target.is_file()
    assert publication_root_for(target).is_dir()
    with pytest.raises(SQLitePublicationError, match="incomplete"):
        open_gmail_publication(target)


def test_preview_rejects_unsupported_values_without_creating_a_target(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    connection = sqlite3.connect(source)
    connection.execute("UPDATE messages SET body_plain = ?", (sqlite3.Binary(b"unsupported"),))
    connection.commit()
    connection.close()

    with pytest.raises(SQLiteSnapshotError, match="unsupported values"):
        preview_gmail_cache_import(source, target)

    assert not target.exists()
    assert not publication_root_for(target).exists()


def test_preview_rejects_unknown_schema_without_creating_a_target(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE private_notes (value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(SQLiteSnapshotError, match="schema is not supported"):
        preview_gmail_cache_import(source, target)

    assert not target.exists()
    assert not publication_root_for(target).exists()


def test_preview_enforces_the_same_row_resource_budget(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)

    with pytest.raises(GmailSyncPartialError, match="resource bound"):
        preview_gmail_cache_import(source, target, max_bytes=1)

    assert not target.exists()
    assert not publication_root_for(target).exists()


@pytest.mark.parametrize(
    "unsupported_schema",
    [
        "CREATE TABLE private_notes (value TEXT)",
        "CREATE TABLE people (email TEXT PRIMARY KEY, display_name TEXT, private_score INTEGER)",
        "CREATE VIEW people_view AS SELECT email FROM people",
        "CREATE TRIGGER people_trigger AFTER INSERT ON people BEGIN DELETE FROM people; END",
    ],
)
def test_import_rejects_unknown_schema_before_creating_target(
    tmp_path: Path,
    unsupported_schema: str,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    connection = sqlite3.connect(source)
    if (
        "people" not in unsupported_schema
        or unsupported_schema.startswith("CREATE VIEW")
        or unsupported_schema.startswith("CREATE TRIGGER")
    ):
        connection.execute("CREATE TABLE people (email TEXT PRIMARY KEY, display_name TEXT)")
    connection.executescript(unsupported_schema)
    connection.commit()
    connection.close()
    source_before = source.read_bytes()

    with pytest.raises(SQLiteSnapshotError, match="schema is not supported"):
        import_gmail_cache(source, target)

    assert source.read_bytes() == source_before
    assert not target.exists()
    assert not publication_root_for(target).exists()


def test_import_rejects_missing_required_gmail_tables(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE people (email TEXT PRIMARY KEY, display_name TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(SQLiteSnapshotError, match="schema is not supported"):
        import_gmail_cache(source, target)

    assert not target.exists()
    assert not publication_root_for(target).exists()


def test_import_rejects_incomplete_required_table_schema(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    connection = sqlite3.connect(source)
    connection.execute("ALTER TABLE labels RENAME TO old_labels")
    connection.execute("CREATE TABLE labels (label_id TEXT PRIMARY KEY)")
    connection.execute("DROP TABLE old_labels")
    connection.commit()
    connection.close()

    with pytest.raises(SQLiteSnapshotError, match="schema is not supported"):
        import_gmail_cache(source, target)

    assert not publication_root_for(target).exists()


def test_import_rejects_wal_source_without_opening_or_changing_it(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    writer = sqlite3.connect(source)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("INSERT INTO sync_state VALUES ('fixture', 'value')")
    writer.commit()
    files = [source, Path(f"{source}-wal"), Path(f"{source}-shm")]
    before = {path.name: path.read_bytes() for path in files if path.exists()}

    with pytest.raises(SQLiteSnapshotError, match="active"):
        import_gmail_cache(source, target)

    assert {path.name: path.read_bytes() for path in files if path.exists()} == before
    assert not target.exists()
    assert not publication_root_for(target).exists()
    writer.close()


def test_import_never_opens_original_after_concurrent_wal_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    sidecar_bytes: dict[str, bytes] = {}
    writer: sqlite3.Connection | None = None

    def enter_wal_then_snapshot(path: Path, *, timeout_seconds: float) -> sqlite3.Connection:
        nonlocal writer
        writer = sqlite3.connect(path)
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("INSERT INTO sync_state VALUES ('race', 'fixture')")
        writer.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            sidecar_bytes[suffix] = sidecar.read_bytes()
        return open_sqlite_read_only(path, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(cache_import, "open_sqlite_read_only", enter_wal_then_snapshot)

    with pytest.raises(SQLiteSnapshotError):
        import_gmail_cache(source, target)

    assert writer is not None
    for suffix, before in sidecar_bytes.items():
        assert Path(f"{source}{suffix}").read_bytes() == before
    assert not target.exists()
    assert not publication_root_for(target).exists()
    writer.close()


def test_failed_import_retains_nonpassing_target_instead_of_deleting_by_path(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    connection = sqlite3.connect(source)
    connection.execute("UPDATE messages SET body_plain = ?", (sqlite3.Binary(b"unsupported"),))
    connection.commit()
    connection.close()

    with pytest.raises(SQLiteSnapshotError, match="unsupported values"):
        import_gmail_cache(source, target)

    assert target.is_file()
    assert publication_root_for(target).is_dir()
    with pytest.raises(SQLitePublicationError, match="incomplete"):
        open_gmail_publication(target)
    with pytest.raises(SQLiteSnapshotError, match="target is not fresh"):
        import_gmail_cache(source, target)


def test_import_rejects_oversized_value_before_target_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "gmail.db"
    _legacy_cache(source)
    connection = sqlite3.connect(source)
    connection.execute("UPDATE messages SET body_plain = ?", ("x" * 64,))
    connection.commit()
    connection.close()
    monkeypatch.setattr("fieldkit.gmail.cache_import._IMPORT_MAX_VALUE_BYTES", 32)

    with pytest.raises(SQLiteSnapshotError, match="unsupported values"):
        import_gmail_cache(source, target)

    assert not target.exists()
    assert not publication_root_for(target).exists()
