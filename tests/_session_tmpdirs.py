"""Own pytest session temp directories with locks that prove process liveness."""

import os
import shutil
import stat
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import TextIO

try:
    import fcntl
except ImportError:  # Windows cannot safely prove ownership with this lock.
    fcntl = None  # type: ignore[assignment]

SESSION_TMPDIR_PREFIX = "fieldkit-ci-"
_CREATING_PREFIX = ".fieldkit-ci-creating-"
_LOCK_NAME = ".session-owner.lock"
_LOCK_MARKER = b"fieldkit-pytest-session-v1\n"


def create_session_tmpdir(parent: Path) -> tuple[Path, TextIO]:
    """Publish a session directory only after its ownership lock is held."""
    creating = Path(tempfile.mkdtemp(prefix=_CREATING_PREFIX, dir=parent))
    lock_path = creating / _LOCK_NAME
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        lock = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            lock.write(_LOCK_MARKER.decode("ascii"))
            lock.flush()
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_SH)
            session = parent / f"{SESSION_TMPDIR_PREFIX}{uuid.uuid4().hex}"
            creating.rename(session)
            return session, lock
        except BaseException:
            lock.close()
            raise
    except BaseException:
        shutil.rmtree(creating, ignore_errors=True)
        raise


def remove_stale_session_tmpdirs(parent: Path) -> list[Path]:
    """Remove only marked session dirs whose owner lock is no longer held.

    Legacy unmarked directories, symlinks, files, other users' directories, and
    live sessions are preserved. Platforms without file locks skip this sweep.
    """
    if fcntl is None:
        return []
    removed: list[Path] = []
    for path in parent.glob(f"{SESSION_TMPDIR_PREFIX}*"):
        try:
            status = path.lstat()
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
                continue
            descriptor = os.open(path / _LOCK_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            continue  # Old sessions have no ownership marker; leave them alone.
        except OSError as exc:
            warnings.warn(f"Could not inspect pytest session directory {path}: {exc}", RuntimeWarning, stacklevel=2)
            continue
        try:
            lock_status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_status.st_mode)
                or lock_status.st_uid != os.getuid()
                or lock_status.st_nlink != 1
                or os.read(descriptor, len(_LOCK_MARKER) + 1) != _LOCK_MARKER
            ):
                continue
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # A live pytest process still holds its shared lock.
            if path.lstat().st_ino != status.st_ino:
                continue
            try:
                shutil.rmtree(path)
            except OSError as exc:
                warnings.warn(
                    f"Could not remove stale pytest session directory {path}: {exc}", RuntimeWarning, stacklevel=2
                )
            else:
                removed.append(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            warnings.warn(f"Could not inspect pytest session directory {path}: {exc}", RuntimeWarning, stacklevel=2)
        finally:
            os.close(descriptor)
    return sorted(removed)
