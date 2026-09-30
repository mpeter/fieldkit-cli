"""Descriptor-based filesystem primitives shared by release consumers."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def file_flags() -> int:
    """Reject symlinks and avoid blocking on non-regular inputs."""
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)


def _checked_directory(descriptor: int) -> int:
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("path must be a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_directory(path: Path) -> int:
    """Retain one real directory, rejecting a symlink leaf."""
    return _checked_directory(os.open(path, _directory_flags()))


def open_directory_at(parent_fd: int, name: str) -> int:
    """Retain one real child of a pinned directory."""
    return _checked_directory(os.open(name, _directory_flags(), dir_fd=parent_fd))


def open_real_directory(path: Path) -> int:
    """Traverse real directory components from the filesystem root."""
    absolute = path.absolute()
    descriptor = open_directory(Path(absolute.anchor))
    try:
        for part in absolute.parts[1:]:
            child = open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_regular_file(descriptor: int, *, maximum_bytes: int) -> bytes:
    """Read a bounded regular file without taking descriptor ownership."""
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("input must be a regular file")
    if metadata.st_size > maximum_bytes:
        raise ValueError("input exceeds size limit")
    with os.fdopen(descriptor, "rb", closefd=False) as stream:
        data = stream.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise ValueError("input exceeds size limit")
    return data


def rename_no_replace_at(parent_fd: int, source: str, destination: str) -> None:
    """Publish atomically with fixed Linux/macOS nonreplacement policy."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        symbol, flag = "renameat2", 0x1
    elif sys.platform == "darwin":
        symbol, flag = "renameatx_np", 0x4
    else:
        raise ValueError("atomic publication is unsupported on this platform")
    try:
        rename = getattr(libc, symbol)
    except AttributeError as error:
        raise ValueError("atomic publication is unavailable") from error
    result = rename(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), flag)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
