"""Isolated worker that copies a quiescent SQLite database under a POSIX lock."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import signal
import stat
from pathlib import PurePath
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from fieldkit.sqlite_read import SnapshotStatus
_SQLITE_LOCK_START = 0x40000000
_SQLITE_LOCK_LENGTH = 512
_SQLITE_HEADER_VERSION_OFFSET = 18
_PARENT_FD_ENV = "FIELDKIT_SQLITE_PARENT_FD"


class _WorkerTerminationRequested(Exception):
    """Raised by the local SIGTERM handler so partial output is cleared."""


class _SnapshotTooLarge(ValueError):
    """The pinned source exceeded its declared snapshot byte budget."""


def _require_regular_fd(fd: int, subject: str) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{subject} is not a regular file")
    return metadata


def _require_parent(parent_fd: int) -> None:
    try:
        marker = os.read(parent_fd, 1)
    except BlockingIOError:
        return
    if marker == b"":
        raise RuntimeError("snapshot controller exited")
    raise RuntimeError("invalid snapshot controller channel")


def _digest_fd(
    fd: int,
    destination: BinaryIO | None = None,
    *,
    max_bytes: int,
    expected_bytes: int,
    parent_fd: int,
) -> str:
    """Hash one pinned descriptor without duplicating or closing it."""
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    _require_parent(parent_fd)
    while True:
        chunk = os.read(fd, min(1024 * 1024, max_bytes - total + 1))
        _require_parent(parent_fd)
        if not chunk:
            break
        if len(chunk) > max_bytes - total:
            raise _SnapshotTooLarge
        total += len(chunk)
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if total != expected_bytes:
        raise RuntimeError("source changed while copying")
    return digest.hexdigest()


def _copy_and_verify(
    source_fd: int,
    destination_fd: int,
    *,
    max_bytes: int,
    expected_bytes: int,
    parent_fd: int,
) -> None:
    os.lseek(destination_fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(destination_fd), "wb") as output:
        source_digest = _digest_fd(
            source_fd,
            output,
            max_bytes=max_bytes,
            expected_bytes=expected_bytes,
            parent_fd=parent_fd,
        )
        output.flush()
        os.fsync(output.fileno())
    copied_digest = _digest_fd(
        destination_fd,
        max_bytes=max_bytes,
        expected_bytes=expected_bytes,
        parent_fd=parent_fd,
    )
    verified_source_digest = _digest_fd(
        source_fd,
        max_bytes=max_bytes,
        expected_bytes=expected_bytes,
        parent_fd=parent_fd,
    )
    if source_digest != copied_digest or source_digest != verified_source_digest:
        raise RuntimeError("source changed while copying")


def _same_directory_entry(directory_fd: int, name: str, file_fd: int) -> bool:
    opened = os.fstat(file_fd)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    return opened.st_dev == current.st_dev and opened.st_ino == current.st_ino


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _uses_wal_journal_mode(fd: int) -> bool:
    """Return whether either SQLite header version byte selects WAL mode."""
    return 2 in os.pread(fd, 2, _SQLITE_HEADER_VERSION_OFFSET)


def _copy_snapshot(
    source_fd: int,
    source_directory_fd: int,
    source_name: str,
    destination_fd: int,
    destination_directory_fd: int,
    destination_name: str,
    max_snapshot_bytes: int,
    *,
    parent_fd: int,
) -> SnapshotStatus:
    source_metadata = _require_regular_fd(source_fd, "source")
    destination_metadata = _require_regular_fd(destination_fd, "destination")
    if (
        source_metadata.st_nlink != 1
        or destination_metadata.st_nlink != 1
        or destination_metadata.st_size != 0
        or stat.S_IMODE(destination_metadata.st_mode) != 0o600
    ):
        raise RuntimeError("snapshot descriptor identity is invalid")
    try:
        fcntl.lockf(
            source_fd,
            fcntl.LOCK_SH | fcntl.LOCK_NB,
            _SQLITE_LOCK_LENGTH,
            _SQLITE_LOCK_START,
            os.SEEK_SET,
        )
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return "active"
        raise

    if not _same_directory_entry(source_directory_fd, source_name, source_fd):
        raise RuntimeError("database path changed before snapshot")
    source_size = source_metadata.st_size
    if source_size > max_snapshot_bytes:
        raise _SnapshotTooLarge
    if _entry_exists(source_directory_fd, f"{source_name}-journal"):
        return "journal"
    if _entry_exists(source_directory_fd, f"{source_name}-wal") or _entry_exists(
        source_directory_fd, f"{source_name}-shm"
    ):
        return "active"
    if _uses_wal_journal_mode(source_fd):
        raise RuntimeError("WAL-mode databases require an external safe export")

    _copy_and_verify(
        source_fd,
        destination_fd,
        max_bytes=max_snapshot_bytes,
        expected_bytes=source_size,
        parent_fd=parent_fd,
    )

    if _entry_exists(source_directory_fd, f"{source_name}-journal"):
        return "journal"
    if _entry_exists(source_directory_fd, f"{source_name}-wal") or _entry_exists(
        source_directory_fd, f"{source_name}-shm"
    ):
        return "active"
    if not _same_directory_entry(source_directory_fd, source_name, source_fd):
        raise RuntimeError("database state changed during snapshot")
    if not _same_directory_entry(destination_directory_fd, destination_name, destination_fd):
        raise RuntimeError("snapshot destination changed during copy")
    final_metadata = os.fstat(source_fd)
    if final_metadata.st_nlink != 1 or _uses_wal_journal_mode(source_fd):
        raise RuntimeError("database namespace or journal mode changed during snapshot")
    if (
        final_metadata.st_dev,
        final_metadata.st_ino,
        final_metadata.st_nlink,
        final_metadata.st_size,
        final_metadata.st_mtime_ns,
        final_metadata.st_ctime_ns,
    ) != (
        source_metadata.st_dev,
        source_metadata.st_ino,
        source_metadata.st_nlink,
        source_metadata.st_size,
        source_metadata.st_mtime_ns,
        source_metadata.st_ctime_ns,
    ):
        raise RuntimeError("database state changed during snapshot")
    _require_parent(parent_fd)
    return "ready"


def _accept_destination_ownership(
    source_fd: int,
    source_directory_fd: int,
    source_name: str,
    destination_fd: int,
    destination_directory_fd: int,
    destination_name: str,
    parent_fd: int,
) -> None:
    """Validate the complete fresh-output contract before permitting cleanup."""
    source = _require_regular_fd(source_fd, "source")
    destination = _require_regular_fd(destination_fd, "destination")
    if not stat.S_ISDIR(os.fstat(source_directory_fd).st_mode) or not stat.S_ISDIR(
        os.fstat(destination_directory_fd).st_mode
    ):
        raise ValueError("snapshot parent is not a directory")
    if (
        source.st_nlink != 1
        or destination.st_nlink != 1
        or destination.st_size != 0
        or stat.S_IMODE(destination.st_mode) != 0o600
        or (source.st_dev, source.st_ino) == (destination.st_dev, destination.st_ino)
        or not _same_directory_entry(source_directory_fd, source_name, source_fd)
        or not _same_directory_entry(destination_directory_fd, destination_name, destination_fd)
    ):
        raise ValueError("snapshot descriptor identity is invalid")
    os.set_blocking(parent_fd, False)
    _require_parent(parent_fd)


def _clear_partial(destination_fd: int) -> bool:
    try:
        os.ftruncate(destination_fd, 0)
        os.fsync(destination_fd)
    except OSError:
        return False
    return True


def _termination_handler(_signum: int, _frame: object) -> None:
    raise _WorkerTerminationRequested


def create_snapshot(argv: list[str]) -> SnapshotStatus:
    """Return a payload-free snapshot outcome without exiting the process."""
    if len(argv) != 7:
        return "invalid"
    result: SnapshotStatus = "invalid"
    destination_fd: int | None = None
    owns_destination = False
    try:
        source_fd = int(argv[0])
        source_directory_fd = int(argv[1])
        source_name = argv[2]
        destination_fd = int(argv[3])
        destination_directory_fd = int(argv[4])
        destination_name = argv[5]
        max_snapshot_bytes = int(argv[6])
        parent_fd = int(os.environ[_PARENT_FD_ENV])
        if (
            min(source_fd, source_directory_fd, destination_fd, destination_directory_fd, parent_fd) <= 2
            or len({source_fd, source_directory_fd, destination_fd, destination_directory_fd, parent_fd}) != 5
            or PurePath(source_name).name != source_name
            or source_name in {"", ".", ".."}
            or PurePath(destination_name).name != destination_name
            or destination_name in {"", ".", ".."}
            or max_snapshot_bytes <= 0
        ):
            raise ValueError("snapshot descriptor contract is invalid")
        _accept_destination_ownership(
            source_fd,
            source_directory_fd,
            source_name,
            destination_fd,
            destination_directory_fd,
            destination_name,
            parent_fd,
        )
        owns_destination = True
        signal.signal(signal.SIGTERM, _termination_handler)
        result = _copy_snapshot(
            source_fd,
            source_directory_fd,
            source_name,
            destination_fd,
            destination_directory_fd,
            destination_name,
            max_snapshot_bytes,
            parent_fd=parent_fd,
        )
    except _SnapshotTooLarge:
        result = "oversized"
    except (KeyError, _WorkerTerminationRequested, OSError, RuntimeError, ValueError):
        result = "invalid"
    finally:
        if owns_destination and destination_fd is not None and result != "ready" and not _clear_partial(destination_fd):
            result = "cleanup"
    return result
