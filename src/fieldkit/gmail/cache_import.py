"""Bounded logical import from an explicitly selected legacy Gmail cache."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path
from typing import Any

from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.gmail.publication import (
    GMAIL_QUERY_READY_KEY,
    apply_gmail_page,
    initialize_gmail_publication,
    publication_root_for,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection
from fieldkit.sqlite_read import open_sqlite_read_only

_IMPORT_MAX_BYTES = 8 * 1024 * 1024 * 1024
_IMPORT_MAX_ROWS = 5_000_000
_IMPORT_MAX_VALUE_BYTES = 4 * 1024 * 1024
_IMPORT_MAX_ROW_BYTES = 24 * 1024 * 1024
_IMPORT_BATCH_ROWS = 16
_IMPORT_MAX_BATCH_BYTES = _IMPORT_MAX_ROW_BYTES * _IMPORT_BATCH_ROWS
_SOURCE_SUFFIXES = ("", "-journal", "-wal", "-shm")
_TABLE_ORDER = (
    "threads",
    "messages",
    "attachments",
    "sync_state",
    "labels",
    "people",
    "slack_activity",
    "calendar_events",
    "thread_accounts",
)
_REQUIRED_GMAIL_TABLES = frozenset({"threads", "messages", "attachments", "sync_state", "labels"})
_PEOPLE_BASE_COLUMNS = frozenset({"email", "display_name", "first_seen", "last_seen", "message_count"})
_PEOPLE_ENRICHMENT_COLUMNS = frozenset({"thread_count", "initiated_count", "domain", "account", "is_internal"})
_PEOPLE_COLUMN_SETS = frozenset(
    {
        _PEOPLE_BASE_COLUMNS,
        _PEOPLE_BASE_COLUMNS | _PEOPLE_ENRICHMENT_COLUMNS,
        _PEOPLE_BASE_COLUMNS | _PEOPLE_ENRICHMENT_COLUMNS | {"meeting_count"},
        _PEOPLE_BASE_COLUMNS | _PEOPLE_ENRICHMENT_COLUMNS | {"meeting_count", "slack_user_id", "slack_message_count"},
    }
)


@dataclass(frozen=True)
class GmailCacheImportResult:
    """Bounded non-sensitive summary of one completed logical import."""

    generation: int
    table_counts: dict[str, int]


@dataclass(frozen=True)
class GmailCacheImportPreview:
    """Bounded non-sensitive summary of one validated write-free preview."""

    table_counts: dict[str, int]


@dataclass(frozen=True)
class _Column:
    name: str
    declared_type: str
    primary_key_position: int


@dataclass(frozen=True)
class _Schema:
    columns: dict[str, tuple[_Column, ...]]
    indexes: dict[str, str]


def _unsupported_schema() -> SQLiteSnapshotError:
    return SQLiteSnapshotError("Legacy Gmail cache schema is not supported", reason="unverified")


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


@cache
def _canonical_schema() -> _Schema:
    connection = sqlite3.connect(":memory:")
    try:
        schema_path = Path(__file__).with_name("schema.sql")
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        columns = {
            table: tuple(
                _Column(str(row[1]), str(row[2]).strip().upper(), int(row[5]))
                for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
            )
            for table in _TABLE_ORDER
        }
        indexes = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name, tbl_name FROM sqlite_schema WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'"
            )
        }
        return _Schema(columns=columns, indexes=indexes)
    finally:
        connection.close()


def _validate_source_schema(connection: sqlite3.Connection) -> dict[str, tuple[_Column, ...]]:
    canonical = _canonical_schema()
    rows = connection.execute(
        "SELECT type, name, tbl_name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    source_tables: set[str] = set()
    for object_type, name, table in rows:
        if object_type == "table":
            if name not in canonical.columns or table != name:
                raise _unsupported_schema()
            source_tables.add(str(name))
        elif object_type == "index":
            if name not in canonical.indexes or canonical.indexes[str(name)] != table:
                raise _unsupported_schema()
        else:
            raise _unsupported_schema()

    if not _REQUIRED_GMAIL_TABLES.issubset(source_tables):
        raise _unsupported_schema()

    validated: dict[str, tuple[_Column, ...]] = {}
    for table in source_tables:
        allowed = {column.name: column for column in canonical.columns[table]}
        source_columns = tuple(
            _Column(str(row[1]), str(row[2]).strip().upper(), int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
        )
        if not source_columns:
            raise _unsupported_schema()
        source_names = frozenset(column.name for column in source_columns)
        expected_names = frozenset(allowed)
        if table == "people":
            if source_names not in _PEOPLE_COLUMN_SETS:
                raise _unsupported_schema()
        elif source_names != expected_names:
            raise _unsupported_schema()
        for column in source_columns:
            expected = allowed.get(column.name)
            if (
                expected is None
                or column.declared_type != expected.declared_type
                or column.primary_key_position != expected.primary_key_position
            ):
                raise _unsupported_schema()
        validated[table] = source_columns
    return validated


def _safe_source_files(source: Path) -> tuple[Path, ...]:
    paths = tuple(Path(f"{source}{suffix}") for suffix in _SOURCE_SUFFIXES)
    present: list[Path] = []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise SQLiteSnapshotError("Legacy Gmail cache input is unverified", reason="unverified")
        present.append(path)
    if source not in present:
        raise SQLiteSnapshotError("Legacy Gmail cache input is missing", reason="unverified")
    return tuple(present)


def _hash_source_files(
    source: Path,
    *,
    deadline: float,
    max_bytes: int,
) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    total = 0
    for path in _safe_source_files(source):
        digest = hashlib.sha256()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        size = 0
        try:
            while chunk := os.read(descriptor, 1024 * 1024):
                size += len(chunk)
                total += len(chunk)
                if total > max_bytes or time.monotonic() >= deadline:
                    raise GmailSyncPartialError("Legacy Gmail cache import exceeded its resource bound")
                digest.update(chunk)
        finally:
            os.close(descriptor)
        result[path.name] = (size, digest.hexdigest())
    return result


def _encoded_row(row: tuple[Any, ...]) -> bytes:
    values: list[str | int | float | None] = []
    maximum_encoded_size = 2 + max(0, len(row) - 1)
    for value in row:
        if (
            value is None
            or (isinstance(value, (str, int)) and not isinstance(value, bool))
            or (isinstance(value, float) and math.isfinite(value))
        ):
            if value is None:
                maximum_encoded_size += 4
            elif isinstance(value, str):
                maximum_encoded_size += len(value) * 12 + 2
            else:
                maximum_encoded_size += len(repr(value))
            values.append(value)
        else:
            raise SQLiteSnapshotError("Legacy Gmail cache contains unsupported values", reason="unverified")
    if maximum_encoded_size + 1 > _IMPORT_MAX_ROW_BYTES:
        raise SQLiteSnapshotError("Legacy Gmail cache contains unsupported values", reason="unverified")
    encoded = (json.dumps(values, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > _IMPORT_MAX_ROW_BYTES:
        raise SQLiteSnapshotError("Legacy Gmail cache contains unsupported values", reason="unverified")
    return encoded


def _validate_value_bounds(
    source: sqlite3.Connection,
    table: str,
    columns: tuple[_Column, ...],
) -> None:
    for column in columns:
        if column.declared_type not in {"TEXT", "BLOB"}:
            continue
        table_name = _quote_identifier(table)
        column_name = _quote_identifier(column.name)
        length_expression = (
            f"length(CAST({column_name} AS BLOB))" if column.declared_type == "TEXT" else f"length({column_name})"
        )
        oversized = source.execute(
            f"SELECT 1 FROM {table_name} "
            f"WHERE typeof({column_name}) IN ('text', 'blob') AND {length_expression} > ? LIMIT 1",
            (_IMPORT_MAX_VALUE_BYTES,),
        ).fetchone()
        if oversized is not None:
            raise SQLiteSnapshotError("Legacy Gmail cache contains unsupported values", reason="unverified")


def _ordered_select(table: str, columns: tuple[_Column, ...]) -> str:
    names = ", ".join(_quote_identifier(column.name) for column in columns)
    primary = [
        column.name
        for column in sorted(columns, key=lambda item: item.primary_key_position)
        if column.primary_key_position
    ]
    order = ", ".join(_quote_identifier(name) for name in primary) if primary else "rowid"
    return f"SELECT {names} FROM {_quote_identifier(table)} ORDER BY {order}"


def _scan_table(
    source: sqlite3.Connection,
    table: str,
    columns: tuple[_Column, ...],
    *,
    budget: list[int],
    deadline: float,
    max_bytes: int,
    consume_batch: Callable[[list[tuple[Any, ...]]], None],
) -> tuple[int, bytes]:
    select = _ordered_select(table, columns)
    source_digest = hashlib.sha256()
    count = 0
    cursor = source.execute(select)
    while rows := cursor.fetchmany(_IMPORT_BATCH_ROWS):
        normalized = [tuple(row) for row in rows]
        batch_bytes = 0
        for row in normalized:
            encoded = _encoded_row(row)
            batch_bytes += len(encoded)
            if batch_bytes > _IMPORT_MAX_BATCH_BYTES:
                raise GmailSyncPartialError("Legacy Gmail cache import exceeded its resource bound")
            budget[0] += len(encoded)
            budget[1] += 1
            count += 1
            if budget[0] > max_bytes or budget[1] > _IMPORT_MAX_ROWS or time.monotonic() >= deadline:
                raise GmailSyncPartialError("Legacy Gmail cache import exceeded its resource bound")
            source_digest.update(encoded)
        consume_batch(normalized)
    return count, source_digest.digest()


def _verify_copied_table(
    target: SQLiteMutationConnection,
    table: str,
    columns: tuple[_Column, ...],
    *,
    expected_count: int,
    expected_digest: bytes,
) -> None:
    target_digest = hashlib.sha256()
    target_count = 0
    target_cursor = target.execute(_ordered_select(table, columns))
    while rows := target_cursor.fetchmany(_IMPORT_BATCH_ROWS):
        for row in rows:
            target_digest.update(_encoded_row(tuple(row)))
            target_count += 1
    if target_count != expected_count or target_digest.digest() != expected_digest:
        raise SQLiteSnapshotError("Legacy Gmail cache content could not be verified", reason="unverified")


def _validate_request(
    source_path: Path,
    target_path: Path,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[Path, Path, float]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_bytes <= 0 or max_bytes > _IMPORT_MAX_BYTES:
        raise ValueError("Gmail cache import bounds are invalid")
    source = source_path.absolute()
    target = target_path.absolute()
    _validate_fresh_target(source, target)
    return source, target, time.monotonic() + timeout_seconds


def _validate_fresh_target(source: Path, target: Path) -> None:
    publication_root = publication_root_for(target)
    try:
        parent = target.parent
        parent_is_stable = parent.is_dir() and parent.resolve(strict=True) == parent
    except (OSError, RuntimeError):
        parent_is_stable = False
    if not parent_is_stable:
        raise SQLiteSnapshotError("Gmail cache import target parent is not stable", reason="unverified")
    if (
        source == target
        or target.exists()
        or target.is_symlink()
        or publication_root.exists()
        or publication_root.is_symlink()
    ):
        raise SQLiteSnapshotError("Gmail cache import target is not fresh", reason="unverified")


@contextmanager
def _validated_source(
    source: Path,
    *,
    deadline: float,
    max_bytes: int,
) -> Iterator[tuple[sqlite3.Connection, dict[str, tuple[_Column, ...]], dict[str, tuple[int, str]]]]:
    """Open and validate one bounded read-only legacy source."""
    before = _hash_source_files(source, deadline=deadline, max_bytes=max_bytes)
    connection = open_sqlite_read_only(source, timeout_seconds=max(0.001, deadline - time.monotonic()))
    try:
        source_schema = _validate_source_schema(connection)
        for table, columns in source_schema.items():
            _validate_value_bounds(connection, table, columns)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _IMPORT_MAX_ROW_BYTES)
        yield connection, source_schema, before
    finally:
        connection.close()


def _scan_source_tables(
    source: sqlite3.Connection,
    source_schema: dict[str, tuple[_Column, ...]],
    *,
    deadline: float,
    max_bytes: int,
    consume_batch: Callable[[str, tuple[_Column, ...], list[tuple[Any, ...]]], None],
    verify_table: Callable[[str, tuple[_Column, ...], int, bytes], None],
) -> dict[str, int]:
    """Validate every source row through one bounded batch scanner."""
    counts: dict[str, int] = {}
    budget = [0, 0]
    for table in _TABLE_ORDER:
        columns = source_schema.get(table)
        if columns is None:
            continue

        count, source_digest = _scan_table(
            source,
            table,
            columns,
            budget=budget,
            deadline=deadline,
            max_bytes=max_bytes,
            consume_batch=partial(consume_batch, table, columns),
        )
        verify_table(table, columns, count, source_digest)
        if count:
            counts[table] = count
    return dict(sorted(counts.items()))


def _discard_batch(
    _table: str,
    _columns: tuple[_Column, ...],
    _rows: list[tuple[Any, ...]],
) -> None:
    """Exhaust validated preview rows without retaining their contents."""


def _skip_table_verification(
    _table: str,
    _columns: tuple[_Column, ...],
    _count: int,
    _digest: bytes,
) -> None:
    """Accept a source-only preview after its bounded row scan."""


def preview_gmail_cache_import(
    source_path: Path,
    target_path: Path,
    *,
    timeout_seconds: float = 30.0,
    max_bytes: int = _IMPORT_MAX_BYTES,
) -> GmailCacheImportPreview:
    """Validate a complete import against the actual fresh target without writing."""
    source, target, deadline = _validate_request(
        source_path,
        target_path,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )
    try:
        with _validated_source(source, deadline=deadline, max_bytes=max_bytes) as (
            connection,
            source_schema,
            before,
        ):
            counts = _scan_source_tables(
                connection,
                source_schema,
                deadline=deadline,
                max_bytes=max_bytes,
                consume_batch=_discard_batch,
                verify_table=_skip_table_verification,
            )
            if _hash_source_files(source, deadline=deadline, max_bytes=max_bytes) != before:
                raise SQLiteSnapshotError("Legacy Gmail cache changed during import", reason="unverified")
            _validate_fresh_target(source, target)
        return GmailCacheImportPreview(table_counts=counts)
    except (SQLiteSnapshotError, GmailSyncPartialError):
        raise
    except (sqlite3.DatabaseError, OSError) as exc:
        raise SQLiteSnapshotError("Legacy Gmail cache could not be read safely", reason="unverified") from exc


def import_gmail_cache(
    source_path: Path,
    target_path: Path,
    *,
    timeout_seconds: float = 30.0,
    max_bytes: int = _IMPORT_MAX_BYTES,
) -> GmailCacheImportResult:
    """Logically copy one explicit legacy cache into a fresh managed identity."""
    source, target, deadline = _validate_request(
        source_path,
        target_path,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )
    try:
        with _validated_source(source, deadline=deadline, max_bytes=max_bytes) as (
            connection,
            source_schema,
            before,
        ):
            initialized = initialize_gmail_publication(target)
            counts: dict[str, int] = {}

            def copy_tables(target_connection: SQLiteMutationConnection) -> None:
                nonlocal counts
                for table in reversed(_TABLE_ORDER):
                    target_connection.execute(f"DELETE FROM {_quote_identifier(table)}")

                def insert_batch(
                    table: str,
                    columns: tuple[_Column, ...],
                    rows: list[tuple[Any, ...]],
                ) -> None:
                    names = ", ".join(_quote_identifier(column.name) for column in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    insert = f"INSERT OR REPLACE INTO {_quote_identifier(table)} ({names}) VALUES ({placeholders})"
                    target_connection.executemany(insert, rows)

                counts = _scan_source_tables(
                    connection,
                    source_schema,
                    deadline=deadline,
                    max_bytes=max_bytes,
                    consume_batch=insert_batch,
                    verify_table=lambda table, columns, count, digest: _verify_copied_table(
                        target_connection,
                        table,
                        columns,
                        expected_count=count,
                        expected_digest=digest,
                    ),
                )
                target_connection.execute(
                    "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
                    (GMAIL_QUERY_READY_KEY,),
                )
                if _hash_source_files(source, deadline=deadline, max_bytes=max_bytes) != before:
                    raise SQLiteSnapshotError("Legacy Gmail cache changed during import", reason="unverified")

            receipt = apply_gmail_page(target, copy_tables, expected_receipt=initialized)
        return GmailCacheImportResult(generation=receipt.generation, table_counts=dict(sorted(counts.items())))
    except BaseException as exc:
        if isinstance(exc, (SQLiteSnapshotError, GmailSyncPartialError)):
            raise
        if isinstance(exc, (sqlite3.DatabaseError, OSError)):
            raise SQLiteSnapshotError("Legacy Gmail cache could not be read safely", reason="unverified") from exc
        raise
