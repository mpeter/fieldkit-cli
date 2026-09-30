"""Bounded kernel-sealed storage; expectations and callers confer no authority."""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import re
import resource
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO, Literal

# Linux UAPI values also needed on Python releases without these exported names.
_MFD_NOEXEC_SEAL = 0x0008
_MFD_EXEC = 0x0010
_F_SEAL_EXEC = 0x0020
_REQUIRED_SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL | _F_SEAL_EXEC
_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileExpectation:
    """Exact selected bytes and output permissions, independent of source mode."""

    size_bytes: int
    sha256: str
    mode: Literal[0o400, 0o500]


@dataclass(frozen=True)
class SnapshotIdentity:
    """Original anonymous file identity, retained independently of descriptor numbers."""

    device: int
    inode: int


@dataclass(frozen=True)
class SealedFileSnapshot:
    """Borrowed reader valid until its capture context exits; no authority implied."""

    descriptor: int
    expectation: FileExpectation
    identity: SnapshotIdentity


@dataclass
class SnapshotBudget:
    """Explicit caller-controlled bounds; failed allocations also consume budget.

    The enclosing deployment validates these bounds against its reviewed ceilings.
    Consumption never decreases, including after a capture's context exits.
    """

    maximum_file_bytes: int
    maximum_total_bytes: int
    deadline: float
    consumed_bytes: int = 0

    def check_deadline(self) -> None:
        if not math.isfinite(self.deadline) or time.monotonic() >= self.deadline:
            raise ValueError("snapshot capture deadline exceeded or invalid")

    def reserve(self, size_bytes: int) -> None:
        self.check_deadline()
        if any(
            type(value) is not int or value < 0
            for value in (self.maximum_file_bytes, self.maximum_total_bytes, self.consumed_bytes, size_bytes)
        ):
            raise ValueError("snapshot byte budget is invalid")
        if size_bytes > self.maximum_file_bytes or self.consumed_bytes + size_bytes > self.maximum_total_bytes:
            raise ValueError("snapshot byte budget exceeded")
        self.consumed_bytes += size_bytes


def preflight_snapshot_descriptors(
    *,
    source_descriptors: int,
    retained_readers: int,
    sealing_writers: int,
    overlapping_readers: int,
    runner_duplicates: int,
    infrastructure_descriptors: int,
) -> None:
    """Check additional peak descriptors against the actual soft limit, without raising it.

    Counts describe additional allocations beyond currently open descriptors.
    Include traversal sources, reader/writer overlap and runner infrastructure.
    This check is not a reservation against other threads' descriptor allocations.
    """
    counts = (
        source_descriptors,
        retained_readers,
        sealing_writers,
        overlapping_readers,
        runner_duplicates,
        infrastructure_descriptors,
    )
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("snapshot descriptor budget is invalid")
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit == resource.RLIM_INFINITY:
        return
    if soft_limit < 0:
        raise ValueError("snapshot descriptor limit is invalid")
    # The scandir descriptor is closed before allocation; exclude it from the count.
    with os.scandir("/proc/self/fd") as entries:
        occupied = sum(1 for _ in entries) - 1
    if occupied + sum(counts) > soft_limit:
        raise ValueError("snapshot descriptor budget exceeds RLIMIT_NOFILE")


def _check_expectation(expected: FileExpectation) -> None:
    if type(expected.size_bytes) is not int or expected.size_bytes < 0:
        raise ValueError("snapshot expected size is invalid")
    if not isinstance(expected.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected.sha256) is None:
        raise ValueError("snapshot expected digest is invalid")
    if type(expected.mode) is not int or expected.mode not in (0o400, 0o500):
        raise ValueError("snapshot expected mode is invalid")


def _metadata(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def validate_snapshot_descriptor(
    descriptor: int, expected: FileExpectation, *, deadline: float, identity: SnapshotIdentity
) -> None:
    """Revalidate an owned/duplicated reader without reopening a live source or changing its offset."""
    _check_expectation(expected)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 0:
        raise ValueError("snapshot must be an anonymous regular file")
    if (metadata.st_dev, metadata.st_ino) != (identity.device, identity.inode):
        raise ValueError("snapshot original identity mismatch")
    if metadata.st_size != expected.size_bytes:
        raise ValueError("snapshot regular-file size mismatch")
    if stat.S_IMODE(metadata.st_mode) != expected.mode:
        raise ValueError("snapshot mode mismatch")
    if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ValueError("snapshot descriptor must be read-only")
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & _REQUIRED_SEALS != _REQUIRED_SEALS:
        raise ValueError("snapshot required kernel seals are missing")
    digest = hashlib.sha256()
    offset = 0
    while offset < expected.size_bytes:
        if not math.isfinite(deadline) or time.monotonic() >= deadline:
            raise ValueError("snapshot validation deadline exceeded or invalid")
        chunk = os.pread(descriptor, min(_CHUNK_BYTES, expected.size_bytes - offset), offset)
        if not chunk:
            raise ValueError("snapshot size mismatch")
        digest.update(chunk)
        offset += len(chunk)
    if not math.isfinite(deadline) or time.monotonic() >= deadline:
        raise ValueError("snapshot validation deadline exceeded or invalid")
    if digest.hexdigest() != expected.sha256 or _metadata(os.fstat(descriptor)) != _metadata(metadata):
        raise ValueError("snapshot digest or metadata mismatch")


@contextmanager
def _seal_chunks(
    chunks: Iterator[bytes], expected: FileExpectation, budget: SnapshotBudget
) -> Iterator[SealedFileSnapshot]:
    _check_expectation(expected)
    budget.reserve(expected.size_bytes)
    if sys.platform != "linux" or not hasattr(os, "memfd_create"):
        raise OSError("snapshot requires Linux executable-mode memfd sealing")
    flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING | (_MFD_EXEC if expected.mode == 0o500 else _MFD_NOEXEC_SEAL)
    writer = os.memfd_create("controller-snapshot", flags)
    reader: int | None = None
    try:
        original = os.fstat(writer)
        identity = SnapshotIdentity(original.st_dev, original.st_ino)
        os.fchmod(writer, expected.mode)
        digest = hashlib.sha256()
        size = 0
        for chunk in chunks:
            budget.check_deadline()
            if type(chunk) is not bytes or len(chunk) > _CHUNK_BYTES:
                raise ValueError("snapshot stream returned an invalid chunk")
            size += len(chunk)
            if size > expected.size_bytes:
                raise ValueError("snapshot size mismatch")
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                budget.check_deadline()
                written = os.write(writer, chunk[offset:])
                if written <= 0:
                    raise OSError("snapshot write made no progress")
                offset += written
        if size != expected.size_bytes or digest.hexdigest() != expected.sha256:
            raise ValueError("snapshot digest or size mismatch")
        budget.check_deadline()
        fcntl.fcntl(writer, fcntl.F_ADD_SEALS, _REQUIRED_SEALS)
        reader = os.open(f"/proc/self/fd/{writer}", os.O_RDONLY | os.O_CLOEXEC)
        writer_metadata = _metadata(os.fstat(writer))
        if _metadata(os.fstat(reader)) != writer_metadata:
            raise ValueError("snapshot reader identity mismatch")
        os.close(writer)
        writer = -1
        validate_snapshot_descriptor(reader, expected, deadline=budget.deadline, identity=identity)
        yield SealedFileSnapshot(reader, expected, identity)
    finally:
        if reader is not None:
            os.close(reader)
        if writer != -1:
            os.close(writer)


@contextmanager
def capture_stream(
    stream: BinaryIO, expected: FileExpectation, *, budget: SnapshotBudget
) -> Iterator[SealedFileSnapshot]:
    """Capture a borrowed decoded stream with exact EOF, size and digest checks.

    Deadline checks detect elapsed time but cannot preempt blocking synchronous
    reads. The enclosing boundary must supervise blocking I/O independently.
    """

    def chunks() -> Iterator[bytes]:
        remaining = expected.size_bytes
        while True:
            budget.check_deadline()
            requested = min(_CHUNK_BYTES, remaining + 1)
            chunk = stream.read(requested)
            if type(chunk) is not bytes or len(chunk) > requested:
                raise ValueError("snapshot stream returned an invalid chunk")
            if not chunk:
                return
            yield chunk
            remaining -= len(chunk)

    with _seal_chunks(chunks(), expected, budget) as snapshot:
        yield snapshot


@contextmanager
def capture_regular_file(
    descriptor: int, expected: FileExpectation, *, budget: SnapshotBudget
) -> Iterator[SealedFileSnapshot]:
    """Capture a borrowed regular descriptor from offset zero; never reopen its source.

    Synchronous pread needs independent supervision when I/O can block.
    """
    _check_expectation(expected)
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size != expected.size_bytes:
        raise ValueError("snapshot source must be a regular file of the expected size")

    def chunks() -> Iterator[bytes]:
        offset = 0
        while True:
            budget.check_deadline()
            chunk = os.pread(descriptor, min(_CHUNK_BYTES, expected.size_bytes - offset + 1), offset)
            if not chunk:
                break
            yield chunk
            offset += len(chunk)
        if _metadata(os.fstat(descriptor)) != _metadata(before):
            raise ValueError("snapshot source changed during capture")

    with _seal_chunks(chunks(), expected, budget) as snapshot:
        if _metadata(os.fstat(descriptor)) != _metadata(before):
            raise ValueError("snapshot source changed during capture")
        yield snapshot
