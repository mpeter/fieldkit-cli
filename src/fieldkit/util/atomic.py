"""fieldkit.util.atomic — Atomic file-write primitives.

Provides building blocks for safe concurrent writes:

- ``exclusive_path_lock(path)`` — format-independent advisory lock for a
  cooperating read/check/write transaction targeting one path.

- ``locked_json_update(path)`` — context manager that reads a JSON file,
  yields a mutable dict for callers to update, then writes the result
  atomically using an advisory flock + os.replace().  Safe across
  concurrent processes on the same Linux host.

- ``atomic_yaml_write(path, data)`` — single-writer atomic YAML write via
  a temp-file + os.replace().  No lock needed for single-writer patterns
  (e.g. config updates from interactive CLI commands).

- ``assert_nonzero_write(path)`` — post-write guard that raises
  ``EmptyOutputError`` and deletes the file if it was written as 0 bytes.
  Call immediately after any write operation to catch silent truncation.

Both write primitives guarantee readers always see either the old file or
the new file — never a partially-written file — because os.replace() is
atomic on POSIX systems.

Note: fcntl.flock is POSIX-only (Linux/macOS).  This module is not
compatible with Windows.
"""

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Callable, Generator, MutableMapping
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from typing import Any, Literal

import yaml
from ruamel.yaml import YAML

from fieldkit.errors import EmptyOutputError


def prepare_runtime_lock_path(
    path: Path, runtime_root: Path, namespace: Literal["pursuit", "meeting-note", "tasks"]
) -> Path:
    """Prepare a contained runtime lock directory and return its canonical name.

    Configured root aliases are resolved once. Descendants may not redirect;
    the directory namespace must remain stable during the caller's transaction.
    This does not defend against a hostile same-user process racing renames.
    """
    if namespace not in {"pursuit", "meeting-note", "tasks"}:
        raise ValueError("Invalid runtime lock namespace")
    root = runtime_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = root
    for component in ("locks", namespace):
        directory = directory / component
        if directory.resolve() != directory:
            raise ValueError("Runtime lock directory redirects its configured root")
        directory.mkdir(exist_ok=True)
        if directory.resolve(strict=True) != directory:
            raise ValueError("Runtime lock directory redirects its configured root")
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return directory / f"{digest}.lock"


_LOCK_POLL_SECONDS = 0.05


class PathLockTimeoutError(TimeoutError):
    """The requested path lock remained owned by another writer."""


def _acquire_path_lock(fd: int, timeout_seconds: float | None) -> None:
    """Acquire immediately, wait to a deadline, or use legacy blocking mode."""
    if timeout_seconds is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PathLockTimeoutError("Timed out acquiring path lock") from None
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))


def atomic_text_write(path: Path, content: str, *, mode: int | None = None) -> None:
    """Replace text using a same-directory temporary file; parent must exist.

    Callers own synchronization and destination authorization. Readers observe
    either the old file or the complete replacement, never partial text. The
    default new-file mode is 0600; callers may explicitly preserve a prior mode.
    """
    _publish_text(path, content, exclusive=False, mode=mode)


def atomic_text_create(path: Path, content: str) -> None:
    """Publish complete text only if the destination does not already exist.

    The same-directory hard link is the exclusive publication point. Existing
    files and symlinks raise FileExistsError without being replaced. The parent
    must exist; callers own path authorization and conflict handling.
    """
    _publish_text(path, content, exclusive=True)


def _publish_text(path: Path, content: str, *, exclusive: bool, mode: int | None = None) -> None:
    """Write and fsync one same-directory temporary file before publication."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            if mode is not None:
                os.fchmod(tmp_fh.fileno(), mode)
            tmp_fh.write(content)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        if exclusive:
            os.link(tmp_path, path)
        else:
            tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


@contextmanager
def exclusive_path_lock(path: Path, *, timeout_seconds: float | None = None) -> Generator[None, None, None]:
    """Serialize cooperating writers for *path* using a persistent sidecar lock.

    The lock file is never removed because deleting it can split waiters across
    different inodes. Keep the complete read/check/write operation inside the
    context and use the same target path across cooperating writers.

    A zero timeout refuses contention immediately; a positive timeout bounds
    the wait. Omitting it retains blocking behavior for existing callers.
    """
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock"), timeout_seconds=timeout_seconds):
        yield


@contextmanager
def exclusive_file_lock(lock_path: Path, *, timeout_seconds: float | None = None) -> Generator[None, None, None]:
    """Acquire an explicitly named persistent lock inode without adding a suffix."""
    if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds < 0):
        raise ValueError("Invalid path lock timeout")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as lock_fh:
        if not stat.S_ISREG(os.fstat(lock_fh.fileno()).st_mode):
            raise OSError("Path lock must be a regular file")
        _acquire_path_lock(lock_fh.fileno(), timeout_seconds)
        try:
            yield
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


@contextmanager
def locked_json_update(path: Path) -> Generator[dict[str, Any], None, None]:
    """Read *path* as JSON, yield the dict for mutation, then write atomically.

    Uses an advisory exclusive flock on a ``.lock`` sidecar file so that
    concurrent callers are serialised.  The actual write uses a temp file +
    ``os.replace()`` to guarantee atomicity even if the process is killed
    between the write and the rename.

    The ``.lock`` sidecar is created alongside *path* and persists between
    calls (it is never deleted).  Its content is irrelevant; only its file
    descriptor is used for locking.

    Args:
        path: Target JSON file.  Need not exist yet — an empty dict is used
              as the starting value when the file is absent.

    Yields:
        A mutable ``dict`` representing the current file contents.  Mutate
        it inside the ``with`` block; the result is written on exit.

    Raises:
        OSError: If the lock file or target directory cannot be created, or
                 if the rename fails.

    Example::

        from fieldkit.config import get_fieldkit_data
        from fieldkit.util.atomic import locked_json_update

        status_file = get_fieldkit_data() / "watchers" / "status.json"
        with locked_json_update(status_file) as data:
            data["my-watcher"] = {"last_run": "2026-06-26T00:00:00Z"}
    """
    with exclusive_path_lock(path):
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        yield data
        atomic_text_write(path, json.dumps(data, default=str, indent=2, sort_keys=True))


def atomic_yaml_write(path: Path, data: dict[str, Any]) -> None:
    """Write *data* as YAML to *path* atomically via a temp-file rename.

    This is a single-writer primitive: it does NOT use a file lock.  Use it
    for config writes from interactive CLI commands where concurrent access
    is not expected.  If concurrent access is a concern, wrap the
    read-modify-write cycle with an additional ``fcntl.flock`` advisory lock.

    The write is atomic because ``os.replace()`` is guaranteed atomic on
    POSIX systems: readers always see either the old file or the new file,
    never a partially-written one.

    Args:
        path: Target YAML file path.  Parent directories are created if
              absent.
        data: Data to serialise as YAML.

    Raises:
        OSError: If the directory cannot be created or the rename fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_text_write(path, yaml.dump(data, default_flow_style=False, allow_unicode=True))


def atomic_round_trip_yaml_update(path: Path, update: Callable[[MutableMapping[str, Any]], None]) -> None:
    """Atomically mutate a mapping-form YAML file without discarding its presentation.

    Comments, mapping order, quotes, and scalar styles in an existing file are
    retained by ruamel.yaml's round-trip representation.  The callback runs
    before serialization, and the destination is replaced only after the
    complete updated document has been rendered successfully.  This is a
    single-writer primitive for interactive config updates; callers requiring
    concurrent mutation must provide a shared lock around the read/write cycle.

    Args:
        path: YAML file to update.  A missing or empty file starts as an empty mapping.
        update: Callback that mutates the loaded mapping in place.

    Raises:
        OSError: If reading or atomically replacing the file fails.
        TypeError: If the document root is not a mapping.
        ruamel.yaml.error.YAMLError: If parsing or serialization fails.
    """
    if path.is_symlink():
        raise OSError(f"Refusing to replace symlinked YAML file: {path}")

    round_trip = YAML(typ="rt")
    round_trip.preserve_quotes = True
    data = round_trip.load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if data is None:
        data = {}
    if not isinstance(data, MutableMapping):
        raise TypeError(f"YAML document must contain a mapping, got {type(data).__name__}")

    update(data)
    rendered = StringIO()
    round_trip.dump(data, rendered)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_text_write(path, rendered.getvalue())


def require_nonempty_output(content: str) -> None:
    """Reject an empty rendered body before any publication operations."""
    if not content.strip():
        raise EmptyOutputError()


def assert_nonzero_write(path: Path) -> None:
    """Raise EmptyOutputError and delete path if the file was written as 0 bytes.

    Intended to be called immediately after any atomic write operation to
    catch silent truncation bugs before the caller proceeds.

    Args:
        path: The file path that was just written.

    Raises:
        EmptyOutputError: If the file exists and has size 0. Cleanup failure
            is recorded explicitly rather than claiming the stub was deleted.
    """
    if path.stat().st_size == 0:
        try:
            path.unlink()
        except OSError:
            raise EmptyOutputError(cleanup_failed=True) from None
        raise EmptyOutputError()
