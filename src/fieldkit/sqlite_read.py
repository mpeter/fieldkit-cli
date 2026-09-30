"""Stable, non-mutating SQLite snapshots for read-only commands."""

import errno
import json
import os
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from fieldkit.errors import SQLiteSnapshotError
from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process
from fieldkit.util.json_decode import unique_json_object

_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024 * 1024
_WORKER_TERMINATION_SECONDS = 2.0


class _SnapshotConnection(sqlite3.Connection):
    """SQLite connection that removes its private snapshot when closed."""

    _snapshot_directory: tempfile.TemporaryDirectory[str] | None = None
    _snapshot_fd: int | None = None

    def close(self) -> None:
        directory = self._snapshot_directory
        snapshot_fd = self._snapshot_fd
        self._snapshot_directory = None
        self._snapshot_fd = None
        try:
            super().close()
        finally:
            try:
                if snapshot_fd is not None:
                    os.close(snapshot_fd)
            finally:
                if directory is not None:
                    directory.cleanup()


@dataclass(frozen=True)
class _PinnedSource:
    path: Path
    file_fd: int
    directory_fd: int


def _open_real_directory(path: Path) -> int:
    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_source(path: Path) -> _PinnedSource:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError("SQLite database was not found") from None
    except OSError as exc:
        raise OSError(exc.errno, "SQLite database path could not be resolved") from None
    except RuntimeError:
        raise SQLiteSnapshotError("SQLite database path could not be resolved", reason="unverified") from None
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = _open_real_directory(resolved.parent)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        no_atime = getattr(os, "O_NOATIME", 0)
        try:
            file_fd = os.open(resolved.name, flags | no_atime, dir_fd=directory_fd)
        except OSError as exc:
            if not no_atime or exc.errno != errno.EPERM:
                raise
            file_fd = os.open(resolved.name, flags, dir_fd=directory_fd)
        opened = os.fstat(file_fd)
        current = os.stat(resolved.name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or opened.st_dev != current.st_dev or opened.st_ino != current.st_ino:
            raise ValueError("SQLite database must be a regular file")
        return _PinnedSource(resolved, file_fd, directory_fd)
    except BaseException as exc:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)
        if isinstance(exc, OSError):
            raise OSError(exc.errno, "SQLite database could not be opened") from None
        raise


SnapshotStatus = Literal["ready", "active", "journal", "oversized", "cleanup", "invalid"]


def _parse_snapshot_status(returncode: int, stdout: str) -> SnapshotStatus:
    """Refuse malformed, ambiguous, or exit-inconsistent worker outcomes."""
    try:
        result = json.loads(stdout, object_pairs_hook=unique_json_object)
    except (ValueError, TypeError):
        raise SQLiteSnapshotError("SQLite snapshot worker result is invalid", reason="unverified") from None
    if not isinstance(result, dict) or set(result) != {"status"}:
        raise SQLiteSnapshotError("SQLite snapshot worker result is invalid", reason="unverified")
    status = result["status"]
    if (
        status not in ("ready", "active", "journal", "oversized", "cleanup", "invalid")
        or type(returncode) is not int
        or returncode != (0 if status == "ready" else 3)
    ):
        raise SQLiteSnapshotError("SQLite snapshot worker result is invalid", reason="unverified")
    return cast("SnapshotStatus", status)


def _snapshot_failure(status: Literal["active", "journal", "oversized", "cleanup", "invalid"]) -> SQLiteSnapshotError:
    if status == "active":
        return SQLiteSnapshotError(
            "SQLite database is active; retry when writers stop.",
            reason="active",
        )
    if status == "journal":
        return SQLiteSnapshotError(
            "SQLite rollback journal requires recovery by the write owner.",
            reason="journal",
        )
    if status == "oversized":
        return SQLiteSnapshotError(
            f"SQLite database exceeds the {_MAX_SNAPSHOT_BYTES}-byte snapshot limit",
            reason="unverified",
        )
    if status == "cleanup":
        return SQLiteSnapshotError("SQLite snapshot cleanup did not complete", reason="unverified")
    return SQLiteSnapshotError("Could not create a verified SQLite snapshot", reason="unverified")


def _run_snapshot_worker(argv: list[str], *, timeout_seconds: float, pass_fds: tuple[int, ...] = ()) -> SnapshotStatus:
    """Run the no-child worker with a parent-liveness lease and bounded cleanup."""
    if os.name != "posix":
        raise SQLiteSnapshotError("SQLite snapshot worker requires POSIX descriptor isolation", reason="unverified")
    parent_read_fd, parent_write_fd = os.pipe()
    os.set_blocking(parent_read_fd, False)
    environment = dict(os.environ)
    environment["FIELDKIT_SQLITE_PARENT_FD"] = str(parent_read_fd)
    try:
        result = run_bounded_process(
            argv,
            timeout=timeout_seconds,
            stdout_limit=128,
            stderr_limit=1024,
            cleanup_timeout=_WORKER_TERMINATION_SECONDS,
            env=environment,
            pass_fds=(*pass_fds, parent_read_fd),
        )
    except BoundedProcessError as exc:
        message = (
            f"SQLite snapshot did not finish within {timeout_seconds:g} seconds."
            if exc.reason == "timeout"
            else "SQLite snapshot worker could not produce a bounded verified result"
        )
        raise SQLiteSnapshotError(message, reason="unverified") from exc
    finally:
        os.close(parent_read_fd)
        os.close(parent_write_fd)
    return _parse_snapshot_status(result.returncode, result.stdout)


def _descriptor_sqlite_path(fd: int) -> Path:
    if sys.platform.startswith("linux"):
        path = Path(f"/proc/self/fd/{fd}")
    elif sys.platform == "darwin":
        path = Path(f"/dev/fd/{fd}")
    else:
        raise SQLiteSnapshotError("SQLite descriptor reads are unsupported on this platform", reason="unverified")
    try:
        descriptor = os.fstat(fd)
        routed = path.stat()
    except OSError as exc:
        raise SQLiteSnapshotError("SQLite descriptor route is unavailable", reason="unverified") from exc
    if descriptor.st_dev != routed.st_dev or descriptor.st_ino != routed.st_ino:
        raise SQLiteSnapshotError("SQLite descriptor route does not retain snapshot identity", reason="unverified")
    return path


def _same_file_at(directory_fd: int, name: str, file_fd: int) -> bool:
    opened = os.fstat(file_fd)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    return opened.st_dev == current.st_dev and opened.st_ino == current.st_ino


def open_sqlite_read_only(path: Path, *, timeout_seconds: float = 5.0) -> sqlite3.Connection:
    """Return a query-only connection to a verified private database snapshot.

    A separate process takes a non-blocking POSIX shared lock over the complete
    source database before copying it. This avoids SQLite's process-scoped lock
    semantics and guarantees that fieldkit does not write the original
    database, journal, WAL, or shared-memory files. Active databases and hot
    rollback journals are refused instead of guessed at. Sources with multiple
    hard links or a WAL-mode SQLite header are unsupported because an alternate
    pathname can place live sidecars outside the source basename checks. On
    Linux, the parent requests ``O_NOATIME``; where that flag is unavailable or
    the kernel denies it, an ordinary source read may update
    filesystem-managed access time.

    Symlinks are accepted only when their final target is an existing regular
    file. The private snapshot is created in a mode-0700 temporary directory,
    opened as immutable and read-only, and then unlinked before this function
    returns. The empty directory is removed when the returned connection
    closes. The snapshot may use private scratch space outside the source
    workspace; its source-byte budget is 8 GiB and its wall-clock budget is
    ``timeout_seconds``.
    """
    source = _open_source(path)
    try:
        directory = tempfile.TemporaryDirectory(prefix="fieldkit-sqlite-read-")
        snapshot_path = Path(directory.name) / "snapshot.db"
        try:
            snapshot_directory_fd = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except BaseException:
            directory.cleanup()
            raise
        try:
            snapshot_fd = os.open(
                snapshot_path.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=snapshot_directory_fd,
            )
            try:
                os.fchmod(snapshot_fd, 0o600)
            except BaseException:
                os.close(snapshot_fd)
                raise
        except BaseException:
            os.close(snapshot_directory_fd)
            directory.cleanup()
            raise
    except BaseException:
        os.close(source.file_fd)
        os.close(source.directory_fd)
        raise
    argv = [
        sys.executable,
        "-I",
        "-m",
        "fieldkit.commands._sqlite_snapshot",
        str(source.file_fd),
        str(source.directory_fd),
        source.path.name,
        str(snapshot_fd),
        str(snapshot_directory_fd),
        snapshot_path.name,
        str(_MAX_SNAPSHOT_BYTES),
    ]
    try:
        try:
            status = _run_snapshot_worker(
                argv,
                timeout_seconds=timeout_seconds,
                pass_fds=(source.file_fd, source.directory_fd, snapshot_fd, snapshot_directory_fd),
            )
        except OSError as exc:
            raise SQLiteSnapshotError("SQLite snapshot worker could not start", reason="unverified") from exc
    except BaseException:
        os.close(snapshot_fd)
        os.close(snapshot_directory_fd)
        directory.cleanup()
        raise
    finally:
        os.close(source.file_fd)
        os.close(source.directory_fd)
    if status != "ready":
        try:
            raise _snapshot_failure(status)
        finally:
            os.close(snapshot_fd)
            os.close(snapshot_directory_fd)
            directory.cleanup()

    conn: _SnapshotConnection | None = None
    try:
        descriptor_path = _descriptor_sqlite_path(snapshot_fd)
        uri = f"{descriptor_path.as_uri()}?mode=ro&immutable=1"
        try:
            raw_conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=timeout_seconds,
                factory=_SnapshotConnection,
            )
        except sqlite3.Error as exc:
            raise SQLiteSnapshotError(
                "Verified SQLite snapshot could not be opened through its descriptor",
                reason="unverified",
            ) from exc
        if not isinstance(raw_conn, _SnapshotConnection):
            raw_conn.close()
            raise TypeError("SQLite did not create the requested snapshot connection type")
        conn = raw_conn
        conn._snapshot_directory = directory
        conn._snapshot_fd = snapshot_fd
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
        if not _same_file_at(snapshot_directory_fd, snapshot_path.name, snapshot_fd):
            raise SQLiteSnapshotError("Verified SQLite snapshot identity changed", reason="unverified")
        try:
            os.unlink(snapshot_path.name, dir_fd=snapshot_directory_fd)
        except OSError as exc:
            raise SQLiteSnapshotError(
                "Verified SQLite snapshot could not be unlinked",
                reason="unverified",
            ) from exc
    except BaseException:
        if conn is None:
            os.close(snapshot_fd)
            directory.cleanup()
        else:
            conn.close()
        raise
    finally:
        os.close(snapshot_directory_fd)
    return conn
