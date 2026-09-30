"""Compare a controller tree with independently supplied expectations, without executing it.

This preparatory check authenticates neither the expectations nor a release. Callers
must load this verifier from their trusted tooling, rather than the controller tree.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts import release_filesystem

MAX_CONTROLLER_MEMBERS = 256
MAX_CONTROLLER_BYTES = 16 * 1024 * 1024
_MAX_PATH_BYTES = 4096
_MAX_PATH_DEPTH = 32


@dataclass(frozen=True)
class ControllerMember:
    """One caller-supplied member expectation; no authority is implied."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ControllerCapture:
    """Validated bytes retained in memory; no selection authority is implied."""

    entrypoint: str
    members: tuple[tuple[str, bytes], ...]


def _path_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_BYTES:
        raise ValueError("controller member path is invalid")
    parts = tuple(value.split("/"))
    if len(parts) > _MAX_PATH_DEPTH or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("controller member path must be canonical and relative")
    if any(re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None for part in parts):
        raise ValueError("controller member path is invalid")
    return parts


def _metadata(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _walk(
    descriptor: int,
    prefix: str,
    members: dict[str, ControllerMember],
    directories: set[str],
    observed: dict[str, tuple[int, int, int, int, int, int]],
    *,
    captured: dict[str, bytes] | None,
) -> None:
    with os.scandir(descriptor) as entries:
        for entry in entries:
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            if path in observed:
                raise ValueError("controller scan contains a duplicate observed path")
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                if path not in directories:
                    raise ValueError("controller contains an unexpected directory")
                child = release_filesystem.open_directory_at(descriptor, entry.name)
                try:
                    if _metadata(os.fstat(child)) != _metadata(metadata):
                        raise ValueError("controller directory changed during verification")
                    observed[path] = _metadata(metadata)
                    _walk(child, path, members, directories, observed, captured=captured)
                    if _metadata(os.fstat(child)) != _metadata(metadata):
                        raise ValueError("controller directory changed during verification")
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("controller members must be regular files")
            if path not in members:
                raise ValueError("controller contains an unexpected member")
            expected = members[path]
            if metadata.st_size != expected.size:
                raise ValueError("controller member size mismatch")
            observed[path] = _metadata(metadata)
            if captured is not None:
                file_descriptor = os.open(entry.name, release_filesystem.file_flags(), dir_fd=descriptor)
                try:
                    if _metadata(os.fstat(file_descriptor)) != _metadata(metadata):
                        raise ValueError("controller member changed during verification")
                    data = release_filesystem.read_regular_file(file_descriptor, maximum_bytes=expected.size)
                    if _metadata(os.fstat(file_descriptor)) != _metadata(metadata):
                        raise ValueError("controller member changed during verification")
                    if len(data) != expected.size or hashlib.sha256(data).hexdigest() != expected.sha256:
                        raise ValueError("controller member digest or size mismatch")
                    captured[path] = data
                finally:
                    os.close(file_descriptor)


def capture_controller_closure(
    controller_root: Path,
    *,
    expected_entrypoint: str,
    expected_members: Sequence[ControllerMember],
) -> ControllerCapture:
    """Capture a bounded closed tree's validated bytes without executing code.

    ValueError rejects invalid expectations or a mismatching tree; OSError reports
    inaccessible paths. Both scans must pass before any capture is returned. Bytes
    come from the validated first-pass descriptors and remain independent of later
    path changes. This comparison authenticates neither expectations nor a release.
    """
    _path_parts(expected_entrypoint)
    if not 0 < len(expected_members) <= MAX_CONTROLLER_MEMBERS:
        raise ValueError("controller member count exceeds limit or is empty")
    members: dict[str, ControllerMember] = {}
    directories: set[str] = set()
    total = 0
    for member in expected_members:
        parts = _path_parts(member.path)
        if member.path in members:
            raise ValueError("controller expectations contain a duplicate member")
        if type(member.size) is not int or not 0 <= member.size <= MAX_CONTROLLER_BYTES:
            raise ValueError("controller member size exceeds limit or is invalid")
        if not isinstance(member.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", member.sha256) is None:
            raise ValueError("controller member digest is invalid")
        total += member.size
        if total > MAX_CONTROLLER_BYTES:
            raise ValueError("controller total bytes exceed limit")
        members[member.path] = member
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    if expected_entrypoint not in members:
        raise ValueError("controller entrypoint is absent from expectations")
    if directories.intersection(members):
        raise ValueError("controller member conflicts with a directory")
    descriptor = release_filesystem.open_real_directory(controller_root)
    try:
        root_metadata = _metadata(os.fstat(descriptor))
        before: dict[str, tuple[int, int, int, int, int, int]] = {}
        captured: dict[str, bytes] = {}
        _walk(descriptor, "", members, directories, before, captured=captured)
        if _metadata(os.fstat(descriptor)) != root_metadata:
            raise ValueError("controller root changed during verification")
        if set(before) != set(members) | directories:
            raise ValueError("controller is missing expected members")
        after: dict[str, tuple[int, int, int, int, int, int]] = {}
        _walk(descriptor, "", members, directories, after, captured=None)
        if before != after or _metadata(os.fstat(descriptor)) != root_metadata:
            raise ValueError("controller changed during verification")
    finally:
        os.close(descriptor)
    return ControllerCapture(expected_entrypoint, tuple(sorted(captured.items())))


def validate_controller_capture(capture: ControllerCapture) -> tuple[tuple[str, bytes], ...]:
    """Return canonical bounded retained bytes without granting selection authority."""
    if not isinstance(capture, ControllerCapture):
        raise ValueError("controller capture is invalid")
    _path_parts(capture.entrypoint)
    if type(capture.members) is not tuple or not 0 < len(capture.members) <= MAX_CONTROLLER_MEMBERS:
        raise ValueError("controller capture member count exceeds limit or is invalid")
    members: dict[str, bytes] = {}
    directories: set[str] = set()
    total = 0
    for row in capture.members:
        if type(row) is not tuple or len(row) != 2:
            raise ValueError("controller capture member is invalid")
        path, data = row
        parts = _path_parts(path)
        if path in members:
            raise ValueError("controller capture contains a duplicate member")
        if type(data) is not bytes:
            raise ValueError("controller capture member data must be immutable bytes")
        total += len(data)
        if total > MAX_CONTROLLER_BYTES:
            raise ValueError("controller capture total bytes exceed limit")
        members[path] = data
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    if capture.entrypoint not in members:
        raise ValueError("controller capture entrypoint is absent")
    if directories.intersection(members):
        raise ValueError("controller capture member conflicts with a directory")
    return tuple(sorted(members.items()))


def verify_controller_closure(
    controller_root: Path,
    *,
    expected_entrypoint: str,
    expected_members: Sequence[ControllerMember],
) -> None:
    """Verify a closed tree without returning release status or executing code.

    Verification observes retained descriptors, not a promise that mutable paths
    remain unchanged after return. Expectations must be supplied independently.
    """
    capture_controller_closure(
        controller_root, expected_entrypoint=expected_entrypoint, expected_members=expected_members
    )
