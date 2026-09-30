"""Bounded, no-follow snapshots for ownership-sensitive local text updates."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@dataclass(frozen=True)
class TextSnapshot:
    """Text and metadata observed from the same stable regular file."""

    content: str
    info: os.stat_result

    @property
    def identity(self) -> tuple[int, int, int, int]:
        """Filesystem identity and change indicators for a later comparison."""
        return _identity(self.info)


def read_text_snapshot(path: Path, *, max_bytes: int) -> TextSnapshot:
    """Read bounded UTF-8 without following a leaf symlink or blocking on a FIFO.

    Missing files propagate FileNotFoundError. Other unsafe or unstable input
    produces a fixed ValueError without reflecting file content or paths.
    Ancestor directories must remain stable during this operation.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("Invalid text snapshot byte limit")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError:
        raise ValueError("Cannot read stable regular text file") from None
    try:
        with os.fdopen(fd, "rb", closefd=False) as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise ValueError("Cannot read stable regular text file")
            payload = source.read(max_bytes + 1)
            after = path.stat(follow_symlinks=False)
            if len(payload) > max_bytes or _identity(before) != _identity(after):
                raise ValueError("Cannot read stable regular text file")
        return TextSnapshot(payload.decode("utf-8"), before)
    except (OSError, UnicodeError):
        raise ValueError("Cannot read stable regular text file") from None
    finally:
        os.close(fd)
