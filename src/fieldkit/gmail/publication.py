"""Canonical committed-generation storage for the Gmail cache."""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from fieldkit.sqlite_publication import (
    SQLiteMutationConnection,
    SQLitePublicationReceipt,
    open_published_sqlite,
    sqlite_publication_writer,
)

GMAIL_PUBLICATION_KIND = "gmail-cache"
GMAIL_SCHEMA_DIGEST = "527a79ec4414b59179814ed2d6b57d99ff3e88985f223d6227a438bb15a41bfb"
GMAIL_QUERY_READY_KEY = "fieldkit_query_ready"


def publication_root_for(source_path: Path) -> Path:
    """Return the sole publication directory associated with a Gmail source."""
    return source_path.with_name(f"{source_path.name}.publication")


def _schema_statements() -> Iterator[str]:
    """Yield complete statements from the maintained Gmail schema."""
    pending = ""
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    for line in schema.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise RuntimeError("Gmail schema contains an incomplete SQL statement")


def _initialize_schema(connection: SQLiteMutationConnection) -> None:
    for statement in _schema_statements():
        connection.execute(statement)


def initialize_gmail_publication(source_path: Path) -> SQLitePublicationReceipt:
    """Create and publish a fresh managed Gmail cache."""
    with sqlite_publication_writer(
        source_path,
        publication_root_for(source_path),
        kind=GMAIL_PUBLICATION_KIND,
        expected_schema_digest=GMAIL_SCHEMA_DIGEST,
    ) as writer:
        _initialize_schema(writer.connection)
    if writer.receipt is None:
        raise RuntimeError("Gmail cache initialization did not publish a generation")
    if writer.receipt.schema_digest != GMAIL_SCHEMA_DIGEST:
        raise RuntimeError("Gmail cache schema does not match the supported release")
    return writer.receipt


def apply_gmail_page(
    source_path: Path,
    mutation: Callable[[SQLiteMutationConnection], None],
    *,
    expected_receipt: SQLitePublicationReceipt | None = None,
) -> SQLitePublicationReceipt:
    """Apply one already-fetched page and publish its checkpoint atomically."""
    with sqlite_publication_writer(
        source_path,
        publication_root_for(source_path),
        kind=GMAIL_PUBLICATION_KIND,
        expected_schema_digest=GMAIL_SCHEMA_DIGEST,
        expected_receipt=expected_receipt,
    ) as writer:
        mutation(writer.connection)
    if writer.receipt is None:
        raise RuntimeError("Gmail cache page did not publish a generation")
    if writer.receipt.schema_digest != GMAIL_SCHEMA_DIGEST:
        raise RuntimeError("Gmail cache schema does not match the supported release")
    return writer.receipt


def open_gmail_publication(source_path: Path) -> sqlite3.Connection:
    """Open the exact ready Gmail cache generation for read-only queries."""
    return open_published_sqlite(
        publication_root_for(source_path),
        expected_kind=GMAIL_PUBLICATION_KIND,
        expected_schema_digest=GMAIL_SCHEMA_DIGEST,
    )
