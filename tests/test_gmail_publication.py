"""Gmail cache publication contracts."""

import sqlite3
from pathlib import Path

import pytest

from fieldkit.errors import SQLiteSnapshotError
from fieldkit.gmail.publication import (
    GMAIL_PUBLICATION_KIND,
    GMAIL_QUERY_READY_KEY,
    GMAIL_SCHEMA_DIGEST,
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
    publication_root_for,
)
from fieldkit.gmail.query_domain import connect
from fieldkit.sqlite_publication import SQLiteMutationConnection, SQLitePublicationError

pytestmark = pytest.mark.unit


def test_fresh_cache_initialization_publishes_the_canonical_non_wal_schema(tmp_path: Path) -> None:
    source = tmp_path / "gmail.db"

    receipt = initialize_gmail_publication(source)

    assert receipt.kind == GMAIL_PUBLICATION_KIND
    assert receipt.generation == 1
    assert receipt.schema_digest == GMAIL_SCHEMA_DIGEST
    assert publication_root_for(source) == tmp_path / "gmail.db.publication"
    with open_gmail_publication(source) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert journal_mode == "delete"
    assert {
        "_fieldkit_publication",
        "attachments",
        "calendar_events",
        "labels",
        "messages",
        "people",
        "slack_activity",
        "sync_state",
        "thread_accounts",
        "threads",
    } <= tables


def test_each_page_mutation_publishes_one_generation_with_its_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "gmail.db"
    initialize_gmail_publication(source)

    def apply_page(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT INTO threads(thread_id, subject, message_count) VALUES (?, ?, ?)",
            ("thread-1", "Quarterly planning", 1),
        )
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, ?)",
            ("last_page_token", "page-2"),
        )

    receipt = apply_gmail_page(source, apply_page)

    assert receipt.generation == 2
    with open_gmail_publication(source) as connection:
        subject = connection.execute("SELECT subject FROM threads WHERE thread_id = ?", ("thread-1",)).fetchone()[0]
        checkpoint = connection.execute("SELECT value FROM sync_state WHERE key = ?", ("last_page_token",)).fetchone()[
            0
        ]
    assert subject == "Quarterly planning"
    assert checkpoint == "page-2"


def test_failed_page_never_exposes_its_partial_rows(tmp_path: Path) -> None:
    source = tmp_path / "gmail.db"
    initialize_gmail_publication(source)

    def fail_page(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT INTO threads(thread_id, subject) VALUES (?, ?)",
            ("partial", "Must not publish"),
        )
        raise RuntimeError("synthetic page failure")

    with pytest.raises(RuntimeError, match="synthetic page failure"):
        apply_gmail_page(source, fail_page)

    with pytest.raises(SQLitePublicationError, match="incomplete"):
        open_gmail_publication(source)

    raw = sqlite3.connect(source)
    try:
        assert raw.execute("SELECT COUNT(*) FROM threads WHERE thread_id = 'partial'").fetchone()[0] == 0
    finally:
        raw.close()


def test_query_consumer_reads_only_the_ready_published_generation(tmp_path: Path) -> None:
    source = tmp_path / "gmail.db"
    initialize_gmail_publication(source)

    def mark_ready(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(source, mark_ready)

    with connect(source) as connection:
        assert connection.execute("SELECT value FROM sync_state WHERE key = 'messages_synced'").fetchone()[0] == "0"


def test_query_consumer_refuses_an_unpublished_raw_cache(tmp_path: Path) -> None:
    source = tmp_path / "gmail.db"
    raw = sqlite3.connect(source)
    raw.execute("CREATE TABLE messages (message_id TEXT PRIMARY KEY)")
    raw.close()

    with pytest.raises(SQLiteSnapshotError, match="requires explicit import"):
        connect(source)
