"""Private storage implementation for managed SQLite publication."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from fieldkit.util.atomic import atomic_text_write
from fieldkit.util.json_decode import require_json_container_depth, unique_json_object

_PUBLICATION_VERSION = 1


_MAX_DATABASE_BYTES = 8 * 1024 * 1024 * 1024


_MIN_FREE_SPACE_RESERVE = 8 * 1024 * 1024


_MAX_JSON_BYTES = 64 * 1024


_MAX_JSON_DEPTH = 64


_MAX_SCHEMA_BYTES = 8 * 1024 * 1024


_MAX_SCHEMA_ROWS = 4_096


_METADATA_TABLE = "_fieldkit_publication"


_KIND_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


_METADATA_AUTHORIZER_ACTIONS = {
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_VTABLE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_REINDEX,
}


class _StorageFailure(RuntimeError):
    """A private storage operation failed before public error normalization."""

    def __init__(self, message: str, *, reason: Literal["active", "unverified", "resource"]) -> None:
        super().__init__(message)
        self.reason: Literal["active", "unverified", "resource"] = reason


@dataclass(frozen=True)
class _PublicationIdentity:
    kind: str
    database_uuid: str
    source_device: int
    source_inode: int


@dataclass(frozen=True, kw_only=True)
class _PublishedArtifact:
    """Verified immutable backup bytes, without publication identity fields."""

    schema_digest: str
    artifact_path: str
    artifact_sha256: str
    artifact_size: int


def _effective_user_id() -> int:
    return os.geteuid()


def _owned_by_effective_user(metadata: os.stat_result) -> bool:
    return metadata.st_uid == _effective_user_id()


def _ensure_private_directory(path: Path, *, create: bool) -> Path:
    absolute = path.absolute()
    existed = absolute.exists() or absolute.is_symlink()
    try:
        if create:
            absolute.mkdir(mode=0o700, exist_ok=True)
        metadata = absolute.lstat()
        if absolute.resolve(strict=True) != absolute:
            raise _StorageFailure("SQLite publication storage is redirected", reason="unverified")
    except OSError as exc:
        raise _StorageFailure("SQLite publication storage is unavailable", reason="unverified") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _StorageFailure("SQLite publication storage is redirected", reason="unverified")
    if not _owned_by_effective_user(metadata):
        raise _StorageFailure("SQLite publication storage is not owned by the effective user", reason="unverified")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise _StorageFailure("SQLite publication storage is not private", reason="unverified")
    if create and not existed:
        _fsync_directory(absolute.parent)
    return absolute


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _publication_lock(path: Path, *, timeout_seconds: float, create: bool) -> Generator[None, None, None]:
    """Take the canonical lock without letting read-only callers create it."""
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _StorageFailure("SQLite publication lock is unverified", reason="unverified") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not _owned_by_effective_user(metadata)
        ):
            raise _StorageFailure("SQLite publication lock is unverified", reason="unverified")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _StorageFailure("SQLite publication writer is active", reason="active") from None
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _safe_existing_regular(path: Path, *, expected_mode: int = 0o600) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise _StorageFailure("SQLite publication input is missing", reason="unverified") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _StorageFailure("SQLite publication input is unverified", reason="unverified")
    if not _owned_by_effective_user(metadata):
        raise _StorageFailure("SQLite publication input is not owned by the effective user", reason="unverified")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise _StorageFailure("SQLite publication input has unsafe permissions", reason="unverified")
    return metadata


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _atomic_private_json(path: Path, payload: Mapping[str, object]) -> bytes:
    if path.exists() or path.is_symlink():
        _safe_existing_regular(path)
    rendered = _json_bytes(payload)
    try:
        atomic_text_write(path, rendered.decode("utf-8"), mode=0o600)
        _safe_existing_regular(path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise _StorageFailure("SQLite publication metadata could not be persisted", reason="unverified") from exc
    return rendered


def _strict_json(path: Path) -> tuple[dict[str, object], bytes]:
    _safe_existing_regular(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_JSON_BYTES
            or not _owned_by_effective_user(metadata)
        ):
            raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified")
        raw = os.read(descriptor, _MAX_JSON_BYTES + 1)
        if len(raw) != metadata.st_size:
            raise _StorageFailure("SQLite publication metadata changed during validation", reason="unverified")
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid constant {value}")),
        )
    except RecursionError:
        raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified") from None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified") from exc
    if not isinstance(decoded, dict):
        raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified")
    try:
        require_json_container_depth(decoded, maximum_depth=_MAX_JSON_DEPTH)
    except ValueError:
        raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified") from None
    return cast(dict[str, object], decoded), raw


def _identity_payload(identity: _PublicationIdentity) -> dict[str, object]:
    return {
        "database_uuid": identity.database_uuid,
        "kind": identity.kind,
        "source": {"device": identity.source_device, "inode": identity.source_inode},
        "version": _PUBLICATION_VERSION,
    }


def _write_identity_once(path: Path, identity: _PublicationIdentity) -> None:
    rendered = _json_bytes(_identity_payload(identity))
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        written = 0
        while written < len(rendered):
            count = os.write(descriptor, rendered[written:])
            if count <= 0:
                raise OSError("short identity write")
            written += count
        os.fsync(descriptor)
    except OSError as exc:
        raise _StorageFailure("SQLite publication identity could not be persisted", reason="unverified") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _safe_existing_regular(path)
    _fsync_directory(path.parent)


def _parse_identity(payload: dict[str, object]) -> _PublicationIdentity:
    if set(payload) != {"version", "kind", "database_uuid", "source"}:
        raise _StorageFailure("SQLite publication identity is unverified", reason="unverified")
    kind = payload["kind"]
    source = payload["source"]
    if (
        payload["version"] != _PUBLICATION_VERSION
        or isinstance(payload["version"], bool)
        or not isinstance(payload["version"], int)
        or not isinstance(kind, str)
        or _KIND_PATTERN.fullmatch(kind) is None
        or not isinstance(source, dict)
        or set(source) != {"device", "inode"}
    ):
        raise _StorageFailure("SQLite publication identity is unverified", reason="unverified")
    device = source["device"]
    inode = source["inode"]
    if (
        not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode <= 0
    ):
        raise _StorageFailure("SQLite publication identity is unverified", reason="unverified")
    return _PublicationIdentity(
        kind=kind,
        database_uuid=_validate_uuid(payload["database_uuid"]),
        source_device=device,
        source_inode=inode,
    )


def _read_identity(path: Path) -> _PublicationIdentity:
    payload, _raw = _strict_json(path)
    return _parse_identity(payload)


def _validate_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise _StorageFailure("SQLite publication identity is unverified", reason="unverified")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise _StorageFailure("SQLite publication identity is unverified", reason="unverified") from exc
    if str(parsed) != value or parsed.version != 4:
        raise _StorageFailure("SQLite publication identity is unverified", reason="unverified")
    return value


def _database_metadata(connection: sqlite3.Connection) -> tuple[str, str, int]:
    try:
        rows = connection.execute(
            f"SELECT database_uuid, kind, generation FROM {_METADATA_TABLE} ORDER BY database_uuid"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified") from exc
    if len(rows) != 1:
        raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified")
    database_uuid, kind, generation = rows[0]
    validated_uuid = _validate_uuid(database_uuid)
    if not isinstance(kind, str) or _KIND_PATTERN.fullmatch(kind) is None:
        raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise _StorageFailure("SQLite publication metadata is unverified", reason="unverified")
    return validated_uuid, kind, generation


def _schema_digest(connection: sqlite3.Connection, *, deadline: float) -> str:
    def _abort_after_deadline() -> int:
        return int(time.monotonic() >= deadline)

    connection.set_progress_handler(_abort_after_deadline, 1_000)
    canonical: list[list[str]] = []
    total_bytes = 0
    try:
        rows = connection.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name, sql"
        )
        for row in rows:
            if time.monotonic() >= deadline or len(canonical) >= _MAX_SCHEMA_ROWS:
                raise _StorageFailure("SQLite publication validation exceeded its bound", reason="resource")
            if len(row) != 4 or any(not isinstance(value, str) for value in row):
                raise _StorageFailure("SQLite publication schema is unverified", reason="unverified")
            values = [str(value) for value in row]
            total_bytes += sum(len(value.encode("utf-8")) for value in values)
            if total_bytes > _MAX_SCHEMA_BYTES:
                raise _StorageFailure("SQLite publication schema exceeded its bound", reason="resource")
            canonical.append(values)
        if time.monotonic() >= deadline:
            raise _StorageFailure("SQLite publication validation exceeded its bound", reason="resource")
    except sqlite3.DatabaseError as exc:
        raise _StorageFailure("SQLite publication schema is unverified", reason="unverified") from exc
    finally:
        connection.set_progress_handler(None, 0)
    return hashlib.sha256(_json_bytes({"schema": canonical})).hexdigest()


def _hash_file(path: Path, *, max_bytes: int, deadline: float) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    total = 0
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or initial.st_size <= 0
            or not _owned_by_effective_user(initial)
        ):
            raise _StorageFailure("SQLite publication artifact is unverified", reason="unverified")
        while True:
            if time.monotonic() >= deadline:
                raise _StorageFailure("SQLite publication validation exceeded its bound", reason="resource")
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _StorageFailure("SQLite publication artifact exceeded its byte bound", reason="resource")
            digest.update(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
    ) or total != initial.st_size:
        raise _StorageFailure("SQLite publication artifact changed during validation", reason="unverified")
    return digest.hexdigest(), total


def _reject_sidecars(path: Path) -> None:
    if any(
        Path(f"{path}{suffix}").exists() or Path(f"{path}{suffix}").is_symlink()
        for suffix in ("-journal", "-wal", "-shm")
    ):
        raise _StorageFailure("SQLite publication artifact has unresolved sidecars", reason="unverified")


def _validate_sqlite(
    connection: sqlite3.Connection,
    *,
    expected_uuid: str,
    expected_kind: str,
    expected_generation: int,
    expected_schema_digest: str | None,
    deadline: float,
) -> str:
    def _abort_after_deadline() -> int:
        return int(time.monotonic() >= deadline)

    connection.set_progress_handler(_abort_after_deadline, 1_000)
    try:
        connection.execute("PRAGMA query_only=ON")
        check = connection.execute("PRAGMA quick_check").fetchall()
        database_uuid, kind, generation = _database_metadata(connection)
    except sqlite3.DatabaseError as exc:
        if time.monotonic() >= deadline:
            raise _StorageFailure("SQLite publication validation exceeded its bound", reason="resource") from exc
        raise _StorageFailure("SQLite publication artifact is unverified", reason="unverified") from exc
    finally:
        connection.set_progress_handler(None, 0)
    if [tuple(row) for row in check] != [("ok",)]:
        raise _StorageFailure("SQLite publication artifact is unverified", reason="unverified")
    if (database_uuid, kind, generation) != (expected_uuid, expected_kind, expected_generation):
        raise _StorageFailure("SQLite publication generation is unverified", reason="unverified")
    digest = _schema_digest(connection, deadline=deadline)
    if expected_schema_digest is not None and digest != expected_schema_digest:
        raise _StorageFailure("SQLite publication schema is unverified", reason="unverified")
    return digest


def _publish_backup(
    source: sqlite3.Connection,
    publication_root: Path,
    *,
    kind: str,
    database_uuid: str,
    generation: int,
    deadline: float,
    max_bytes: int,
) -> _PublishedArtifact:
    staging_root = _ensure_private_directory(publication_root / "staging", create=True)
    artifacts_root = _ensure_private_directory(publication_root / "artifacts", create=True)
    staging_directory: Path | None = None
    destination: sqlite3.Connection | None = None
    try:
        staging_directory = Path(tempfile.mkdtemp(prefix="generation-", dir=staging_root))
        staging_directory.chmod(0o700)
        _ensure_private_directory(staging_directory, create=False)
        backup_path = staging_directory / "backup.sqlite"
        descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        free_bytes = shutil.disk_usage(staging_root).free
        source_size = max(1, int(source.execute("PRAGMA page_count").fetchone()[0])) * max(
            1, int(source.execute("PRAGMA page_size").fetchone()[0])
        )
        if source_size > max_bytes or free_bytes < source_size + _MIN_FREE_SPACE_RESERVE:
            raise _StorageFailure("SQLite publication lacks bounded storage capacity", reason="resource")
        backup_connection = sqlite3.connect(backup_path, isolation_level=None)
        destination = backup_connection

        def _progress(_status: int, remaining: int, total: int) -> None:
            if time.monotonic() >= deadline:
                raise _StorageFailure("SQLite publication backup exceeded its deadline", reason="resource")
            page_size = max(1, int(backup_connection.execute("PRAGMA page_size").fetchone()[0]))
            expected_size = total * page_size
            remaining_size = remaining * page_size
            if expected_size > max_bytes or backup_path.stat().st_size > max_bytes:
                raise _StorageFailure("SQLite publication backup exceeded its byte bound", reason="resource")
            if shutil.disk_usage(staging_root).free < remaining_size + _MIN_FREE_SPACE_RESERVE:
                raise _StorageFailure("SQLite publication lacks bounded storage capacity", reason="resource")

        source.backup(backup_connection, pages=256, progress=_progress, sleep=0.01)
        journal_mode = backup_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
            raise _StorageFailure("SQLite publication journal mode is unverified", reason="unverified")
        schema_digest = _validate_sqlite(
            backup_connection,
            expected_uuid=database_uuid,
            expected_kind=kind,
            expected_generation=generation,
            expected_schema_digest=None,
            deadline=deadline,
        )
        destination.close()
        destination = None
        _reject_sidecars(backup_path)
        file_descriptor = os.open(backup_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
        artifact_sha256, artifact_size = _hash_file(backup_path, max_bytes=max_bytes, deadline=deadline)
        artifact_name = f"{artifact_sha256}.sqlite"
        artifact_path = artifacts_root / artifact_name
        try:
            os.link(backup_path, artifact_path, follow_symlinks=False)
            backup_path.unlink()
            _safe_existing_regular(artifact_path)
            _fsync_directory(artifacts_root)
        except FileExistsError:
            _safe_existing_regular(artifact_path)
            existing_sha256, existing_size = _hash_file(artifact_path, max_bytes=max_bytes, deadline=deadline)
            if (existing_sha256, existing_size) != (artifact_sha256, artifact_size):
                raise _StorageFailure("SQLite publication artifact identity conflicts", reason="unverified") from None
        return _PublishedArtifact(
            schema_digest=schema_digest,
            artifact_path=f"artifacts/{artifact_name}",
            artifact_sha256=artifact_sha256,
            artifact_size=artifact_size,
        )
    except sqlite3.DatabaseError as exc:
        raise _StorageFailure("SQLite publication backup is unverified", reason="unverified") from exc
    except OSError as exc:
        raise _StorageFailure("SQLite publication artifact could not be persisted", reason="unverified") from exc
    finally:
        if destination is not None:
            destination.close()
        if staging_directory is not None:
            shutil.rmtree(staging_directory, ignore_errors=True)


def _remove_superseded_artifacts(artifacts_root: Path, *, current_name: str) -> None:
    """Unlink old immutable names; already-open reader descriptors remain valid."""
    expected_name = re.compile(r"[0-9a-f]{64}\.sqlite\Z")
    try:
        entries = list(artifacts_root.iterdir())
        for entry in entries:
            if entry.name == current_name:
                continue
            metadata = entry.lstat()
            if (
                expected_name.fullmatch(entry.name) is None
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or not _owned_by_effective_user(metadata)
            ):
                raise _StorageFailure("SQLite publication artifact storage is unverified", reason="unverified")
            entry.unlink()
        _fsync_directory(artifacts_root)
    except OSError as exc:
        raise _StorageFailure("SQLite publication artifacts could not be retired", reason="unverified") from exc


def _cleanup_stale_staging(staging_root: Path) -> None:
    """Remove at most one prior-crash staging tree before source mutation."""
    try:
        entries = list(staging_root.iterdir())
        if len(entries) > 1:
            raise _StorageFailure("SQLite publication staging is unverified", reason="unverified")
        for entry in entries:
            metadata = entry.lstat()
            if (
                not entry.name.startswith("generation-")
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or not _owned_by_effective_user(metadata)
            ):
                raise _StorageFailure("SQLite publication staging is unverified", reason="unverified")
            shutil.rmtree(entry)
        _fsync_directory(staging_root)
    except OSError as exc:
        raise _StorageFailure("SQLite publication staging could not be cleaned", reason="unverified") from exc


def _source_metadata(source: Path) -> os.stat_result:
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise _StorageFailure("Managed SQLite source is unavailable", reason="unverified") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not _owned_by_effective_user(metadata)
    ):
        raise _StorageFailure("Managed SQLite source is unverified", reason="unverified")
    return metadata


def _remove_created_source(source: Path, expected: os.stat_result) -> None:
    """Remove only the exact new source inode created by this writer."""
    try:
        metadata = source.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise _StorageFailure("Managed SQLite source cleanup is unverified", reason="unverified")
        source.unlink()
        _fsync_directory(source.parent)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _StorageFailure("Managed SQLite source cleanup failed", reason="unverified") from exc


def _create_private_source(source: Path) -> os.stat_result:
    """Create a new empty source privately before SQLite can open its pathname."""
    descriptor: int | None = None
    metadata: os.stat_result | None = None
    try:
        descriptor = os.open(
            source,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not _owned_by_effective_user(metadata)
        ):
            raise _StorageFailure("Managed SQLite source is unverified", reason="unverified")
        path_metadata = source.lstat()
        if (path_metadata.st_dev, path_metadata.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise _StorageFailure("Managed SQLite source is unverified", reason="unverified")
        os.fsync(descriptor)
    except (OSError, _StorageFailure) as exc:
        if metadata is not None:
            _remove_created_source(source, metadata)
        if isinstance(exc, _StorageFailure):
            raise
        raise _StorageFailure("Managed SQLite source could not be created privately", reason="unverified") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if metadata is None:
        raise _StorageFailure("Managed SQLite source could not be created privately", reason="unverified")
    try:
        _fsync_directory(source.parent)
    except OSError as exc:
        _remove_created_source(source, metadata)
        raise _StorageFailure("Managed SQLite source could not be persisted", reason="unverified") from exc
    return metadata


def _reject_artifact_inode_source(source_metadata: os.stat_result, root: Path) -> None:
    artifacts_root = root / "artifacts"
    if not artifacts_root.exists() and not artifacts_root.is_symlink():
        return
    artifacts = _ensure_private_directory(artifacts_root, create=False)
    try:
        for artifact in artifacts.iterdir():
            metadata = artifact.lstat()
            if (metadata.st_dev, metadata.st_ino) == (source_metadata.st_dev, source_metadata.st_ino):
                raise _StorageFailure(
                    "Managed SQLite source cannot be a publication artifact",
                    reason="unverified",
                )
    except OSError as exc:
        raise _StorageFailure("SQLite publication artifacts are unverified", reason="unverified") from exc
