"""Kernel-enforced storage integrity and bounded capture lifecycle contracts."""

import errno
import hashlib
import io
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import release_controller_snapshot as snapshot

pytestmark = pytest.mark.unit


def _expect(data: bytes, mode: int = 0o400) -> snapshot.FileExpectation:
    if mode == 0o500:
        return snapshot.FileExpectation(len(data), hashlib.sha256(data).hexdigest(), 0o500)
    return snapshot.FileExpectation(len(data), hashlib.sha256(data).hexdigest(), 0o400)


def _budget(maximum: int = 1024 * 1024) -> snapshot.SnapshotBudget:
    return snapshot.SnapshotBudget(maximum, maximum, time.monotonic() + 10)


@pytest.fixture
def kernel_sealing() -> None:
    """Distinguish actual missing kernel support from test or implementation failures."""
    for mode in (0o400, 0o500):
        try:
            with snapshot.capture_stream(io.BytesIO(b""), _expect(b"", mode), budget=_budget()) as result:
                assert isinstance(result, snapshot.SealedFileSnapshot)
        except OSError as error:
            if error.errno in (errno.EINVAL, errno.ENOSYS):
                pytest.skip(f"kernel lacks required executable-mode memfd sealing: {error}")
            raise


@pytest.mark.parametrize("mode", [0o400, 0o500])
def test_exact_snapshot_denies_reopened_writes_growth_shrink_and_exec_changes(kernel_sealing: None, mode: int) -> None:
    data = b"selected bytes"
    expected = _expect(data, mode)
    with snapshot.capture_stream(io.BytesIO(data), expected, budget=_budget()) as result:
        assert result.expectation == expected
        assert os.pread(result.descriptor, len(data), 0) == data
        assert not os.get_inheritable(result.descriptor)
        # Ordinary write permission can change; byte and executable-bit seals persist.
        os.fchmod(result.descriptor, mode | 0o200)
        other = os.open(f"/proc/self/fd/{result.descriptor}", os.O_RDWR | os.O_CLOEXEC)
        try:
            for operation in (
                lambda: os.pwrite(other, b"x", 0),
                lambda: os.ftruncate(other, len(data) - 1),
                lambda: os.ftruncate(other, len(data) + 1),
                lambda: os.fchmod(other, mode ^ 0o100),
            ):
                with pytest.raises(OSError, match="Operation not permitted") as denied:
                    operation()
                assert denied.value.errno == errno.EPERM
        finally:
            os.close(other)
        os.fchmod(result.descriptor, mode)
        duplicate = os.dup(result.descriptor)
        try:
            os.lseek(duplicate, 4, os.SEEK_SET)
            snapshot.validate_snapshot_descriptor(
                duplicate, expected, deadline=_budget().deadline, identity=result.identity
            )
            assert os.lseek(duplicate, 0, os.SEEK_CUR) == 4
        finally:
            os.close(duplicate)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(result.descriptor)


def test_source_capture_preserves_offset_and_survives_source_mutation(tmp_path: Path, kernel_sealing: None) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"old bytes")
    with path.open("rb") as stream:
        stream.seek(3)
        with snapshot.capture_regular_file(stream.fileno(), _expect(b"old bytes"), budget=_budget()) as result:
            assert result.expectation == _expect(b"old bytes")
            assert stream.tell() == 3
            path.write_bytes(b"new bytes")
            assert os.pread(result.descriptor, 9, 0) == b"old bytes"
        assert stream.tell() == 3


@pytest.mark.parametrize("data, expected", [(b"extra", b"extr"), (b"short", b"shorter"), (b"wrong", b"right")])
def test_stream_rejects_exact_size_or_hash_mismatch(data: bytes, expected: bytes, kernel_sealing: None) -> None:
    with (
        pytest.raises(ValueError, match=r"digest or size|size mismatch"),
        snapshot.capture_stream(io.BytesIO(data), _expect(expected), budget=_budget()),
    ):
        pytest.fail("mismatching stream yielded")


def test_stream_checks_eof_for_zero_sized_expectation(kernel_sealing: None) -> None:
    with (
        pytest.raises(ValueError, match="size mismatch"),
        snapshot.capture_stream(io.BytesIO(b"x"), _expect(b""), budget=_budget()),
    ):
        pytest.fail("nonempty stream yielded")


def test_aggregate_budget_remains_consumed_after_failure_and_context_exit(kernel_sealing: None) -> None:
    budget = snapshot.SnapshotBudget(4, 8, time.monotonic() + 10)
    with snapshot.capture_stream(io.BytesIO(b"good"), _expect(b"good"), budget=budget) as result:
        assert result.expectation == _expect(b"good")
    with (
        pytest.raises(ValueError, match="digest or size"),
        snapshot.capture_stream(io.BytesIO(b"bad!"), _expect(b"good"), budget=budget),
    ):
        pytest.fail("bad capture yielded")
    assert budget.consumed_bytes == 8
    with (
        pytest.raises(ValueError, match="budget exceeded"),
        snapshot.capture_stream(io.BytesIO(b"x"), _expect(b"x"), budget=budget),
    ):
        pytest.fail("over-budget capture yielded")


@pytest.mark.parametrize("failure", ["oversize", "sparse", "deadline", "invalid-mode"])
def test_rejects_before_memfd_allocation(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def allocation_forbidden(name: str, flags: int) -> int:
        pytest.fail("invalid source allocated a memfd")

    monkeypatch.setattr("scripts.release_controller_snapshot.os.memfd_create", allocation_forbidden)
    budget = _budget(4)
    expected = _expect(b"large")
    if failure == "deadline":
        budget.deadline = time.monotonic() - 1
    if failure == "invalid-mode":
        expected = snapshot.FileExpectation(0, hashlib.sha256(b"").hexdigest(), 0o400)
        object.__setattr__(expected, "mode", 0o600)
    if failure == "sparse":
        path = tmp_path / "sparse"
        with path.open("w+b") as stream:
            stream.truncate(100_000_000)
            expected = snapshot.FileExpectation(100_000_000, "0" * 64, 0o400)
            with (
                pytest.raises(ValueError, match="budget exceeded"),
                snapshot.capture_regular_file(stream.fileno(), expected, budget=budget),
            ):
                pytest.fail("oversize sparse source yielded")
        return
    with (
        pytest.raises(ValueError, match=r"budget exceeded|deadline|mode"),
        snapshot.capture_stream(io.BytesIO(b"large"), expected, budget=budget),
    ):
        pytest.fail("invalid capture yielded")


def test_source_mutation_during_capture_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel_sealing: None,
) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"original")
    pread = os.pread
    with path.open("rb") as source:

        def mutate(descriptor: int, size: int, offset: int) -> bytes:
            data = pread(descriptor, size, offset)
            if descriptor == source.fileno() and offset == 0:
                path.write_bytes(b"original")
            return data

        monkeypatch.setattr("scripts.release_controller_snapshot.os.pread", mutate)
        with (
            pytest.raises(ValueError, match="source changed"),
            snapshot.capture_regular_file(source.fileno(), _expect(b"original"), budget=_budget()),
        ):
            pytest.fail("mutating source yielded")


def test_short_writes_are_completed(monkeypatch: pytest.MonkeyPatch, kernel_sealing: None) -> None:
    write = os.write
    monkeypatch.setattr(
        "scripts.release_controller_snapshot.os.write", lambda descriptor, data: write(descriptor, data[:2])
    )
    with snapshot.capture_stream(io.BytesIO(b"complete"), _expect(b"complete"), budget=_budget()) as result:
        assert result.expectation == _expect(b"complete")
        assert os.pread(result.descriptor, 8, 0) == b"complete"


@pytest.mark.parametrize("failure", ["identity", "write", "seal", "open-reader", "validate", "caller"])
def test_every_partial_failure_closes_allocated_descriptors(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    kernel_sealing: None,
) -> None:
    allocated: list[int] = []
    memfd, open_fd, fstat = os.memfd_create, os.open, os.fstat

    def allocate(name: str, flags: int) -> int:
        descriptor = memfd(name, flags)
        allocated.append(descriptor)
        return descriptor

    def reader(path: str, flags: int) -> int:
        if failure == "open-reader":
            raise OSError("injected reader failure")
        descriptor = open_fd(path, flags)
        allocated.append(descriptor)
        return descriptor

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr("scripts.release_controller_snapshot.os.memfd_create", allocate)
    monkeypatch.setattr("scripts.release_controller_snapshot.os.open", reader)
    if failure == "identity":
        monkeypatch.setattr("scripts.release_controller_snapshot.os.fstat", fail)
    elif failure == "write":
        monkeypatch.setattr("scripts.release_controller_snapshot.os.write", fail)
    elif failure == "seal":
        monkeypatch.setattr("scripts.release_controller_snapshot.fcntl.fcntl", fail)
    elif failure == "validate":
        monkeypatch.setattr(snapshot, "validate_snapshot_descriptor", fail)
    with (
        pytest.raises(OSError, match="injected"),
        snapshot.capture_stream(io.BytesIO(b"data"), _expect(b"data"), budget=_budget()) as result,
    ):
        assert result.expectation == _expect(b"data")
        if failure == "caller":
            raise OSError("injected caller failure")
    assert allocated
    for descriptor in allocated:
        with pytest.raises(OSError, match="Bad file descriptor"):
            fstat(descriptor)


@pytest.mark.parametrize("change", ["size", "digest", "mode"])
def test_duplicate_revalidation_rejects_wrong_expectations(change: str, kernel_sealing: None) -> None:
    with snapshot.capture_stream(io.BytesIO(b"data"), _expect(b"data"), budget=_budget()) as result:
        assert result.expectation == _expect(b"data")
        expected = {"size": _expect(b"longer"), "digest": _expect(b"fake"), "mode": _expect(b"data", 0o500)}[change]
        with pytest.raises(ValueError, match=r"size mismatch|digest|mode mismatch"):
            snapshot.validate_snapshot_descriptor(
                result.descriptor, expected, deadline=_budget().deadline, identity=result.identity
            )


def test_duplicate_revalidation_rejects_unsealed_file(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"data")
    path.chmod(0o400)
    with path.open("rb") as source:
        metadata = os.fstat(source.fileno())
        identity = snapshot.SnapshotIdentity(metadata.st_dev, metadata.st_ino)
        path.unlink()
        with pytest.raises((OSError, ValueError), match=r"Invalid argument|required kernel seals"):
            snapshot.validate_snapshot_descriptor(
                source.fileno(), _expect(b"data"), deadline=_budget().deadline, identity=identity
            )


def test_linked_regular_file_is_rejected_even_with_matching_identity(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"data")
    path.chmod(0o400)
    with path.open("rb") as source:
        metadata = os.fstat(source.fileno())
        identity = snapshot.SnapshotIdentity(metadata.st_dev, metadata.st_ino)
        with pytest.raises(ValueError, match="anonymous regular file"):
            snapshot.validate_snapshot_descriptor(
                source.fileno(), _expect(b"data"), deadline=_budget().deadline, identity=identity
            )


def test_reused_descriptor_rejects_different_sealed_file_with_identical_bytes(kernel_sealing: None) -> None:
    expected = _expect(b"data")
    with (
        snapshot.capture_stream(io.BytesIO(b"data"), expected, budget=_budget()) as original,
        snapshot.capture_stream(io.BytesIO(b"data"), expected, budget=_budget()) as replacement,
    ):
        assert original.expectation == replacement.expectation == expected
        assert original.identity != replacement.identity
        original_descriptor = original.descriptor
        os.close(original_descriptor)
        assert os.dup2(replacement.descriptor, original_descriptor, inheritable=False) == original_descriptor
        with pytest.raises(ValueError, match="original identity mismatch"):
            snapshot.validate_snapshot_descriptor(
                original_descriptor, expected, deadline=_budget().deadline, identity=original.identity
            )
        snapshot.validate_snapshot_descriptor(
            original_descriptor, expected, deadline=_budget().deadline, identity=replacement.identity
        )


def test_descriptor_preflight_real_limit_and_explicit_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    kwargs = {
        "source_descriptors": 0,
        "retained_readers": 0,
        "sealing_writers": 0,
        "overlapping_readers": 0,
        "runner_duplicates": 0,
        "infrastructure_descriptors": 0,
    }
    snapshot.preflight_snapshot_descriptors(**kwargs)
    if soft != resource.RLIM_INFINITY:
        kwargs["runner_duplicates"] = soft
        with pytest.raises(ValueError, match="RLIMIT_NOFILE"):
            snapshot.preflight_snapshot_descriptors(**kwargs)
    monkeypatch.setattr(
        "scripts.release_controller_snapshot.resource.getrlimit",
        lambda kind: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    snapshot.preflight_snapshot_descriptors(**kwargs)
    kwargs["sealing_writers"] = -1
    with pytest.raises(ValueError, match="budget is invalid"):
        snapshot.preflight_snapshot_descriptors(**kwargs)


def test_midstream_deadline_failure(kernel_sealing: None, monkeypatch: pytest.MonkeyPatch) -> None:
    budget = _budget()

    class ExpiringStream(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            data = super().read(size)
            budget.deadline = time.monotonic() - 1
            return data

    with (
        pytest.raises(ValueError, match="deadline"),
        snapshot.capture_stream(ExpiringStream(b"data"), _expect(b"data"), budget=budget),
    ):
        pytest.fail("expired stream yielded")


@pytest.mark.parametrize("chunk", [None, "", bytearray(), False, b"too large"])
def test_invalid_stream_return_cannot_supply_empty_eof(
    chunk: object,
    monkeypatch: pytest.MonkeyPatch,
    kernel_sealing: None,
) -> None:
    stream = io.BytesIO(b"")
    monkeypatch.setattr(stream, "read", lambda size: chunk)
    with (
        pytest.raises(ValueError, match="invalid chunk"),
        snapshot.capture_stream(stream, _expect(b""), budget=_budget()),
    ):
        pytest.fail("invalid stream return yielded")


def test_unsupported_memfd_flags_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def unsupported(name: str, flags: int) -> int:
        raise OSError(errno.EINVAL, "unsupported executable-mode memfd")

    monkeypatch.setattr("scripts.release_controller_snapshot.os.memfd_create", unsupported)
    with (
        pytest.raises(OSError, match="unsupported executable-mode"),
        snapshot.capture_stream(io.BytesIO(b""), _expect(b""), budget=_budget()),
    ):
        pytest.fail("unsupported kernel yielded")


def test_writable_duplicate_is_rejected(kernel_sealing: None) -> None:
    with snapshot.capture_stream(io.BytesIO(b"data"), _expect(b"data"), budget=_budget()) as result:
        assert result.expectation == _expect(b"data")
        os.fchmod(result.descriptor, 0o600)
        writer = os.open(f"/proc/self/fd/{result.descriptor}", os.O_RDWR | os.O_CLOEXEC)
        os.fchmod(result.descriptor, 0o400)
        try:
            with pytest.raises(ValueError, match="read-only"):
                snapshot.validate_snapshot_descriptor(
                    writer, _expect(b"data"), deadline=_budget().deadline, identity=result.identity
                )
        finally:
            os.close(writer)


def test_descriptor_preflight_with_lowered_real_limit() -> None:
    code = """
import os
import resource
from scripts.release_controller_snapshot import preflight_snapshot_descriptors
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
with os.scandir('/proc/self/fd') as entries:
    occupied = sum(1 for _ in entries) - 1
resource.setrlimit(resource.RLIMIT_NOFILE, (occupied + 2, hard))
try:
    preflight_snapshot_descriptors(source_descriptors=1, retained_readers=1,
        sealing_writers=1, overlapping_readers=1, runner_duplicates=1,
        infrastructure_descriptors=1)
except ValueError as error:
    assert 'RLIMIT_NOFILE' in str(error)
else:
    raise AssertionError('lowered actual descriptor limit was ignored')
assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == occupied + 2
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode()
