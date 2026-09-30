"""Writer-published committed SQLite generations for read-only consumers.

The receipt proves what a cooperating fieldkit writer published. It does not
turn arbitrary SQLite main-file bytes into committed provenance and does not
claim protection from a hostile process running as the same operating-system
user. Owner-only storage, strict bindings and one canonical lock make crashes,
ordinary concurrency, malformed state and path redirection fail closed.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fieldkit import _sqlite_publication_storage as storage

PublicationFailureReason = Literal["active", "unverified", "resource"]


class SQLitePublicationError(RuntimeError):
    """A published SQLite generation could not be proven safe to use."""

    def __init__(self, message: str, *, reason: PublicationFailureReason) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SQLitePublicationReceipt:
    """The exact committed SQLite generation named by ready state."""

    kind: str
    database_uuid: str
    generation: int
    schema_digest: str
    artifact_path: str
    artifact_sha256: str
    artifact_size: int


@dataclass
class SQLitePublicationWriter:
    """The sole transaction exposed during one managed publication."""

    connection: SQLiteMutationConnection
    receipt: SQLitePublicationReceipt | None = None


class SQLiteMutationCursor:
    """A cursor view that does not expose its privileged connection."""

    __slots__ = ("__cursor",)

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.__cursor = cursor

    @property
    def rowcount(self) -> int:
        return self.__cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self.__cursor.lastrowid

    def fetchone(self) -> Any:
        return self.__cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self.__cursor.fetchall()

    def fetchmany(self, size: int | None = None) -> list[Any]:
        if size is None:
            return self.__cursor.fetchmany()
        return self.__cursor.fetchmany(size)

    def __iter__(self) -> Iterator[Any]:
        while (row := self.__cursor.fetchone()) is not None:
            yield row

    def close(self) -> None:
        self.__cursor.close()


class SQLiteMutationConnection:
    """The intentionally small SQL surface available to a publication caller."""

    __slots__ = ("__connection",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.__connection = connection

    def execute(
        self,
        sql: str,
        parameters: Any = (),
    ) -> SQLiteMutationCursor:
        return SQLiteMutationCursor(self.__connection.execute(sql, parameters))

    def executemany(
        self,
        sql: str,
        parameters: Iterable[Any],
    ) -> SQLiteMutationCursor:
        return SQLiteMutationCursor(self.__connection.executemany(sql, parameters))


def _validate_kind(kind: str) -> None:
    if storage._KIND_PATTERN.fullmatch(kind) is None:
        raise ValueError("SQLite publication kind must be a lowercase portable identifier")


def _validate_bound(timeout_seconds: float, max_bytes: int) -> None:
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("SQLite publication timeout must be non-negative and finite")
    if max_bytes <= 0 or max_bytes > storage._MAX_DATABASE_BYTES:
        raise ValueError("SQLite publication byte bound is invalid")


def _receipt_payload(receipt: SQLitePublicationReceipt) -> dict[str, object]:
    return {
        "artifact": {
            "path": receipt.artifact_path,
            "sha256": receipt.artifact_sha256,
            "size": receipt.artifact_size,
        },
        "database_uuid": receipt.database_uuid,
        "generation": receipt.generation,
        "kind": receipt.kind,
        "schema_digest": receipt.schema_digest,
        "version": storage._PUBLICATION_VERSION,
    }


def _ready_state(receipt: SQLitePublicationReceipt, receipt_sha256: str) -> dict[str, object]:
    return {
        "generation": receipt.generation,
        "kind": receipt.kind,
        "receipt_sha256": receipt_sha256,
        "status": "ready",
        "version": storage._PUBLICATION_VERSION,
    }


def _lexical_source_path(source_path: Path, publication_root: Path) -> Path:
    # abspath normalizes ``..`` without following a symlink out of either tree.
    source = Path(os.path.abspath(source_path))  # noqa: PTH100
    root = Path(os.path.abspath(publication_root))  # noqa: PTH100
    if source == root or source.is_relative_to(root):
        raise SQLitePublicationError(
            "Managed SQLite source cannot be a publication artifact",
            reason="unverified",
        )
    return source


def _validate_existing_binding(
    root: Path,
    source_metadata: os.stat_result | None,
    *,
    kind: str,
) -> storage._PublicationIdentity | None:
    identity_path = root / "identity.json"
    has_identity = identity_path.exists() or identity_path.is_symlink()
    if not has_identity:
        if source_metadata is not None or any(
            (root / name).exists() or (root / name).is_symlink() for name in ("state.json", "receipt.json", "artifacts")
        ):
            raise SQLitePublicationError(
                "Existing SQLite publication requires explicit supported import or recovery",
                reason="unverified",
            )
        return None
    if source_metadata is None:
        raise SQLitePublicationError("SQLite publication identity source is missing", reason="unverified")
    identity = storage._read_identity(identity_path)
    if identity.kind != kind or (identity.source_device, identity.source_inode) != (
        source_metadata.st_dev,
        source_metadata.st_ino,
    ):
        raise SQLitePublicationError(
            "SQLite publication identity does not match the managed source", reason="unverified"
        )
    receipt_path = root / "receipt.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        receipt_payload, _receipt_bytes = storage._strict_json(receipt_path)
        receipt = _parse_receipt(receipt_payload)
        if receipt.kind != identity.kind or receipt.database_uuid != identity.database_uuid:
            raise SQLitePublicationError("SQLite publication identity continuity is unverified", reason="unverified")
    return identity


def _validate_conditional_update(
    source: Path,
    root: Path,
    expected: SQLitePublicationReceipt,
    *,
    kind: str,
    timeout_seconds: float,
) -> None:
    """Require the caller's committed receipt while the publication lock is held."""
    state, _state_bytes = storage._strict_json(root / "state.json")
    generation, receipt_sha256 = _parse_ready_state(state, expected_kind=kind)
    payload, receipt_bytes = storage._strict_json(root / "receipt.json")
    current = _parse_receipt(payload)
    if (
        current != expected
        or current.kind != kind
        or current.generation != generation
        or hashlib.sha256(receipt_bytes).hexdigest() != receipt_sha256
    ):
        raise SQLitePublicationError("SQLite publication changed before conditional update", reason="unverified")
    connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=timeout_seconds)
    try:
        metadata = storage._database_metadata(connection)
    finally:
        connection.close()
    if metadata != (expected.database_uuid, expected.kind, expected.generation):
        raise SQLitePublicationError("SQLite publication changed before conditional update", reason="unverified")


@contextmanager
def sqlite_publication_writer(
    source_path: Path,
    publication_root: Path,
    *,
    kind: str,
    expected_schema_digest: str | None = None,
    expected_receipt: SQLitePublicationReceipt | None = None,
    timeout_seconds: float = 30.0,
    max_bytes: int = storage._MAX_DATABASE_BYTES,
) -> Generator[SQLitePublicationWriter, None, None]:
    """Yield one managed mutation transaction and publish its committed backup."""
    try:
        _validate_kind(kind)
        _validate_bound(timeout_seconds, max_bytes)
        if expected_schema_digest is not None and storage._SHA256_PATTERN.fullmatch(expected_schema_digest) is None:
            raise ValueError("Expected SQLite schema digest must be lowercase SHA-256")
        source = _lexical_source_path(source_path, publication_root)
        root = storage._ensure_private_directory(publication_root, create=True)
        lock_path = root / "publication.lock"
        deadline = time.monotonic() + timeout_seconds
        with storage._publication_lock(lock_path, timeout_seconds=timeout_seconds, create=True):
            storage._safe_existing_regular(lock_path)
            try:
                parent = source.parent
                if parent.resolve(strict=True) != parent or source.is_symlink():
                    raise SQLitePublicationError("Managed SQLite source path is redirected", reason="unverified")
                source_existed = source.exists()
                if source_existed:
                    storage._reject_artifact_inode_source(source.lstat(), root)
                    source_metadata = storage._source_metadata(source)
                else:
                    source_metadata = None
                existing_identity = _validate_existing_binding(root, source_metadata, kind=kind)
            except OSError as exc:
                raise SQLitePublicationError("Managed SQLite source is unavailable", reason="unverified") from exc
            if expected_receipt is not None:
                _validate_conditional_update(source, root, expected_receipt, kind=kind, timeout_seconds=timeout_seconds)
            storage._cleanup_stale_staging(storage._ensure_private_directory(root / "staging", create=True))
            storage._atomic_private_json(
                root / "state.json",
                {"kind": kind, "status": "updating", "version": storage._PUBLICATION_VERSION},
            )
            connection: sqlite3.Connection | None = None
            created_source = storage._create_private_source(source) if not source_existed else None
            try:
                connection = sqlite3.connect(source, isolation_level=None, timeout=timeout_seconds)
            except (OSError, sqlite3.DatabaseError) as exc:
                if connection is not None:
                    connection.close()
                if created_source is not None:
                    storage._remove_created_source(source, created_source)
                raise SQLitePublicationError("Managed SQLite source is unavailable", reason="unverified") from exc
            if connection is None:
                raise SQLitePublicationError("Managed SQLite source is unavailable", reason="unverified")
            writer = SQLitePublicationWriter(connection=SQLiteMutationConnection(connection))
            policy_denied = False
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                if source_existed:
                    try:
                        database_uuid, stored_kind, generation = storage._database_metadata(connection)
                    except storage._StorageFailure as exc:
                        raise SQLitePublicationError(
                            "Existing SQLite database requires explicit supported import",
                            reason="unverified",
                        ) from exc
                    if stored_kind != kind:
                        raise SQLitePublicationError("SQLite publication kind is unverified", reason="unverified")
                    if existing_identity is None or database_uuid != existing_identity.database_uuid:
                        raise SQLitePublicationError(
                            "SQLite publication identity continuity is unverified", reason="unverified"
                        )
                else:
                    database_uuid = str(uuid.uuid4())
                    generation = 0
                    connection.execute(
                        f"CREATE TABLE {storage._METADATA_TABLE} ("
                        "database_uuid TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                        "generation INTEGER NOT NULL CHECK (generation >= 0))"
                    )
                    connection.execute(
                        f"INSERT INTO {storage._METADATA_TABLE}(database_uuid, kind, generation) VALUES (?, ?, 0)",
                        (database_uuid, kind),
                    )
                    metadata = storage._source_metadata(source)
                    storage._write_identity_once(
                        root / "identity.json",
                        storage._PublicationIdentity(
                            kind=kind,
                            database_uuid=database_uuid,
                            source_device=metadata.st_dev,
                            source_inode=metadata.st_ino,
                        ),
                    )

                def _authorizer(action: int, argument1: str | None, argument2: str | None, *_rest: object) -> int:
                    nonlocal policy_denied
                    transaction_control = action == sqlite3.SQLITE_TRANSACTION
                    attachment = action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
                    journal_change = (
                        action == sqlite3.SQLITE_PRAGMA and argument1 == "journal_mode" and argument2 is not None
                    )
                    internal_metadata_change = (
                        action in storage._METADATA_AUTHORIZER_ACTIONS
                        and storage._METADATA_TABLE
                        in {
                            argument1,
                            argument2,
                        }
                    )
                    writable_schema = action == sqlite3.SQLITE_PRAGMA and argument1 == "writable_schema"
                    if (
                        transaction_control
                        or attachment
                        or journal_change
                        or internal_metadata_change
                        or writable_schema
                    ):
                        policy_denied = True
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(_authorizer)
                try:
                    yield writer
                except BaseException as exc:
                    connection.set_authorizer(None)
                    with suppress(sqlite3.DatabaseError):
                        connection.rollback()
                    if policy_denied and isinstance(exc, sqlite3.DatabaseError):
                        raise SQLitePublicationError(
                            "Caller attempted to control the publication transaction",
                            reason="unverified",
                        ) from exc
                    raise
                connection.set_authorizer(None)
                if policy_denied or not connection.in_transaction:
                    raise SQLitePublicationError(
                        "Caller attempted to control the publication transaction",
                        reason="unverified",
                    )
                if time.monotonic() >= deadline:
                    raise SQLitePublicationError(
                        "SQLite publication transaction exceeded its deadline", reason="resource"
                    )
                if expected_schema_digest is not None:
                    actual_schema_digest = storage._schema_digest(connection, deadline=deadline)
                    if actual_schema_digest != expected_schema_digest:
                        raise SQLitePublicationError("SQLite publication schema is unverified", reason="unverified")
                generation += 1
                connection.execute(f"UPDATE {storage._METADATA_TABLE} SET generation = ?", (generation,))
                connection.commit()
                artifact = storage._publish_backup(
                    connection,
                    root,
                    kind=kind,
                    database_uuid=database_uuid,
                    generation=generation,
                    deadline=deadline,
                    max_bytes=max_bytes,
                )
                receipt = SQLitePublicationReceipt(
                    kind=kind,
                    database_uuid=database_uuid,
                    generation=generation,
                    schema_digest=artifact.schema_digest,
                    artifact_path=artifact.artifact_path,
                    artifact_sha256=artifact.artifact_sha256,
                    artifact_size=artifact.artifact_size,
                )
                receipt_bytes = storage._atomic_private_json(root / "receipt.json", _receipt_payload(receipt))
                receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
                storage._remove_superseded_artifacts(
                    storage._ensure_private_directory(root / "artifacts", create=False),
                    current_name=Path(receipt.artifact_path).name,
                )
                storage._atomic_private_json(root / "state.json", _ready_state(receipt, receipt_sha256))
                writer.receipt = receipt
            except BaseException:
                with suppress(sqlite3.DatabaseError):
                    connection.set_authorizer(None)
                with suppress(sqlite3.DatabaseError):
                    connection.rollback()
                raise
            finally:
                connection.close()
    except storage._StorageFailure as failure:
        raise SQLitePublicationError(str(failure), reason=failure.reason) from failure.__cause__


def _parse_ready_state(payload: dict[str, object], *, expected_kind: str) -> tuple[int, str]:
    if payload == {"version": storage._PUBLICATION_VERSION, "status": "updating", "kind": expected_kind}:
        raise SQLitePublicationError("SQLite publication is incomplete", reason="unverified")
    expected = {"version", "status", "kind", "generation", "receipt_sha256"}
    if set(payload) != expected:
        raise SQLitePublicationError("SQLite publication state is unverified", reason="unverified")
    if (
        not isinstance(payload["version"], int)
        or isinstance(payload["version"], bool)
        or payload["version"] != storage._PUBLICATION_VERSION
        or payload["kind"] != expected_kind
    ):
        raise SQLitePublicationError("SQLite publication state is unverified", reason="unverified")
    if payload["status"] != "ready":
        raise SQLitePublicationError("SQLite publication is incomplete", reason="unverified")
    generation = payload["generation"]
    receipt_sha256 = payload["receipt_sha256"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise SQLitePublicationError("SQLite publication state is unverified", reason="unverified")
    if not isinstance(receipt_sha256, str) or storage._SHA256_PATTERN.fullmatch(receipt_sha256) is None:
        raise SQLitePublicationError("SQLite publication state is unverified", reason="unverified")
    return generation, receipt_sha256


def _parse_receipt(payload: dict[str, object]) -> SQLitePublicationReceipt:
    if set(payload) != {"version", "kind", "database_uuid", "generation", "schema_digest", "artifact"}:
        raise SQLitePublicationError("SQLite publication receipt is unverified", reason="unverified")
    if (
        not isinstance(payload["version"], int)
        or isinstance(payload["version"], bool)
        or payload["version"] != storage._PUBLICATION_VERSION
    ):
        raise SQLitePublicationError("SQLite publication receipt is unverified", reason="unverified")
    kind = payload["kind"]
    generation = payload["generation"]
    schema_digest = payload["schema_digest"]
    artifact = payload["artifact"]
    if not isinstance(kind, str) or storage._KIND_PATTERN.fullmatch(kind) is None:
        raise SQLitePublicationError("SQLite publication receipt is unverified", reason="unverified")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise SQLitePublicationError("SQLite publication receipt is unverified", reason="unverified")
    if not isinstance(schema_digest, str) or storage._SHA256_PATTERN.fullmatch(schema_digest) is None:
        raise SQLitePublicationError("SQLite publication receipt is unverified", reason="unverified")
    if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256", "size"}:
        raise SQLitePublicationError("SQLite publication receipt is unverified", reason="unverified")
    artifact_path = artifact["path"]
    artifact_sha256 = artifact["sha256"]
    artifact_size = artifact["size"]
    if not isinstance(artifact_path, str) or not isinstance(artifact_sha256, str):
        raise SQLitePublicationError("SQLite publication receipt is unverified", reason="unverified")
    parsed_path = PurePosixPath(artifact_path)
    if (
        parsed_path.is_absolute()
        or parsed_path.parts != ("artifacts", f"{artifact_sha256}.sqlite")
        or storage._SHA256_PATTERN.fullmatch(artifact_sha256) is None
    ):
        raise SQLitePublicationError("SQLite publication receipt path is unverified", reason="unverified")
    if not isinstance(artifact_size, int) or isinstance(artifact_size, bool) or artifact_size <= 0:
        raise SQLitePublicationError("SQLite publication receipt is unverified", reason="unverified")
    return SQLitePublicationReceipt(
        kind=kind,
        database_uuid=storage._validate_uuid(payload["database_uuid"]),
        generation=generation,
        schema_digest=schema_digest,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        artifact_size=artifact_size,
    )


def open_published_sqlite(
    publication_root: Path,
    *,
    expected_kind: str,
    expected_schema_digest: str,
    timeout_seconds: float = 5.0,
    max_bytes: int = storage._MAX_DATABASE_BYTES,
) -> sqlite3.Connection:
    """Open one fully bound committed publication, never the live source."""
    _validate_kind(expected_kind)
    _validate_bound(timeout_seconds, max_bytes)
    if storage._SHA256_PATTERN.fullmatch(expected_schema_digest) is None:
        raise ValueError("Expected SQLite schema digest must be lowercase SHA-256")
    deadline = time.monotonic() + timeout_seconds
    connection: sqlite3.Connection | None = None
    try:
        root = storage._ensure_private_directory(publication_root, create=False)
        with storage._publication_lock(root / "publication.lock", timeout_seconds=timeout_seconds, create=False):
            storage._safe_existing_regular(root / "publication.lock")
            state, _state_bytes = storage._strict_json(root / "state.json")
            generation, receipt_sha256 = _parse_ready_state(state, expected_kind=expected_kind)
            receipt_payload, receipt_bytes = storage._strict_json(root / "receipt.json")
            if hashlib.sha256(receipt_bytes).hexdigest() != receipt_sha256:
                raise SQLitePublicationError("SQLite publication receipt binding is unverified", reason="unverified")
            receipt = _parse_receipt(receipt_payload)
            identity = storage._read_identity(root / "identity.json")
            if (
                receipt.kind != expected_kind
                or receipt.generation != generation
                or receipt.schema_digest != expected_schema_digest
                or identity.kind != receipt.kind
                or identity.database_uuid != receipt.database_uuid
            ):
                raise SQLitePublicationError("SQLite publication generation is unverified", reason="unverified")
            artifact = root / Path(receipt.artifact_path)
            metadata = storage._safe_existing_regular(artifact)
            if metadata.st_size != receipt.artifact_size or metadata.st_size > max_bytes:
                raise SQLitePublicationError("SQLite publication artifact is unverified", reason="unverified")
            storage._reject_sidecars(artifact)
            artifact_sha256, artifact_size = storage._hash_file(artifact, max_bytes=max_bytes, deadline=deadline)
            if (artifact_sha256, artifact_size) != (receipt.artifact_sha256, receipt.artifact_size):
                raise SQLitePublicationError("SQLite publication artifact is unverified", reason="unverified")
            uri = f"{artifact.as_uri()}?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=timeout_seconds)
            connection.row_factory = sqlite3.Row
            storage._validate_sqlite(
                connection,
                expected_uuid=receipt.database_uuid,
                expected_kind=receipt.kind,
                expected_generation=receipt.generation,
                expected_schema_digest=receipt.schema_digest,
                deadline=deadline,
            )
            if connection is None:
                raise SQLitePublicationError("SQLite publication artifact is unverified", reason="unverified")
            return connection
    except BaseException as exc:
        if connection is not None:
            try:
                connection.close()
            except BaseException:  # noqa: BLE001 - Cleanup must not replace the original exception.
                exc.add_note("SQLite publication connection cleanup failed")
        if isinstance(exc, storage._StorageFailure):
            error = SQLitePublicationError(str(exc), reason=exc.reason)
            for note in getattr(exc, "__notes__", ()):
                error.add_note(note)
            raise error from exc.__cause__
        if isinstance(exc, sqlite3.DatabaseError):
            raise SQLitePublicationError("SQLite publication artifact is unverified", reason="unverified") from exc
        raise
