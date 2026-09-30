"""Worker ownership, descriptor and lifecycle contracts for SQLite snapshots."""

import multiprocessing
import os
import signal
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from fieldkit.errors import SQLiteSnapshotError

pytestmark = pytest.mark.unit


@contextmanager
def _worker_descriptors(source: Path, destination: Path) -> Iterator[tuple[int, int, int, int, int]]:
    source_directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
    source_fd = os.open(source.name, os.O_RDONLY, dir_fd=source_directory_fd)
    destination_directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(
        destination.name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=destination_directory_fd
    )
    parent_read_fd, parent_write_fd = os.pipe()
    os.set_blocking(parent_read_fd, False)
    try:
        yield source_fd, source_directory_fd, destination_fd, destination_directory_fd, parent_read_fd
    finally:
        for descriptor in (
            source_fd,
            source_directory_fd,
            destination_fd,
            destination_directory_fd,
            parent_read_fd,
            parent_write_fd,
        ):
            os.close(descriptor)


def _attempt_external_write(db_path: str, start: Any, outcome: Any) -> None:
    start.wait(10)
    writer = sqlite3.connect(db_path, timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE records SET value = value + 1")
    except sqlite3.OperationalError as exc:
        outcome.put("blocked" if "locked" in str(exc).lower() else f"error:{exc}")
    else:
        outcome.put("acquired")
        writer.rollback()
    finally:
        writer.close()


def test_snapshot_lock_is_retained_through_second_source_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    db_path = tmp_path / "source.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (1)")
    writer.commit()
    writer.close()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcome = context.Queue()
    process = context.Process(target=_attempt_external_write, args=(str(db_path), start, outcome))
    process.start()
    original_digest = worker._digest_fd
    calls = 0

    def instrumented_digest(fd: int, destination: Any = None, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            start.set()
            assert outcome.get(timeout=5) == "blocked"
        return original_digest(fd, destination, **kwargs)

    monkeypatch.setattr(worker, "_digest_fd", instrumented_digest)
    try:
        destination = tmp_path / "copy.db"
        with _worker_descriptors(db_path, destination) as descriptors:
            source_fd, source_directory_fd, destination_fd, destination_directory_fd, parent_fd = descriptors
            assert (
                worker._copy_snapshot(
                    source_fd,
                    source_directory_fd,
                    db_path.name,
                    destination_fd,
                    destination_directory_fd,
                    destination.name,
                    1024 * 1024,
                    parent_fd=parent_fd,
                )
                == "ready"
            )
    finally:
        start.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(2)
    assert process.exitcode == 0


def test_hardlink_added_during_verification_fails_final_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    db_path = tmp_path / "source.db"
    alias_path = tmp_path / "late-alias.db"
    destination = tmp_path / "copy.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.commit()
    writer.close()
    original_digest = worker._digest_fd
    calls = 0

    def add_alias_after_source_verification(fd: int, output: Any = None, **kwargs: Any) -> str:
        nonlocal calls
        result = original_digest(fd, output, **kwargs)
        calls += 1
        if calls == 3:
            os.link(db_path, alias_path)
        return result

    monkeypatch.setattr(worker, "_digest_fd", add_alias_after_source_verification)

    with _worker_descriptors(db_path, destination) as descriptors:
        source_fd, source_directory_fd, destination_fd, destination_directory_fd, parent_fd = descriptors
        with pytest.raises(RuntimeError, match="namespace"):
            worker._copy_snapshot(
                source_fd,
                source_directory_fd,
                db_path.name,
                destination_fd,
                destination_directory_fd,
                destination.name,
                1024 * 1024,
                parent_fd=parent_fd,
            )

    assert calls == 3
    assert alias_path.exists()


def test_worker_ready_rejects_output_path_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    writer = sqlite3.connect(source)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.commit()
    writer.close()
    original_copy = worker._copy_and_verify

    def replace_after_copy(source_fd: int, destination_fd: int, **kwargs: Any) -> None:
        original_copy(source_fd, destination_fd, **kwargs)
        destination.unlink()
        destination.write_bytes(b"substituted")

    monkeypatch.setattr(worker, "_copy_and_verify", replace_after_copy)

    with _worker_descriptors(source, destination) as descriptors:
        source_fd, source_directory_fd, destination_fd, destination_directory_fd, parent_fd = descriptors
        with pytest.raises(RuntimeError, match="destination changed"):
            worker._copy_snapshot(
                source_fd,
                source_directory_fd,
                source.name,
                destination_fd,
                destination_directory_fd,
                destination.name,
                1024 * 1024,
                parent_fd=parent_fd,
            )


def test_snapshot_byte_budget_rejects_stream_exceeding_declared_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    db_path = tmp_path / "growing.db"
    destination = tmp_path / "copy.db"
    db_path.write_bytes(b"a")
    original_os = os
    extended = False

    def read_with_extra_byte(fd: int, size: int) -> bytes:
        nonlocal extended
        chunk = original_os.read(fd, size)
        if not extended:
            extended = True
            chunk += b"b"
        return chunk

    class IsolatedOs:
        def __getattr__(self, name: str) -> Any:
            return getattr(original_os, name)

        def read(self, fd: int, size: int) -> bytes:
            return read_with_extra_byte(fd, size)

    isolated_os = IsolatedOs()
    monkeypatch.setattr(worker, "os", isolated_os)

    with _worker_descriptors(db_path, destination) as descriptors:
        source_fd, source_directory_fd, destination_fd, destination_directory_fd, parent_fd = descriptors
        with pytest.raises(ValueError):
            worker._copy_snapshot(
                source_fd,
                source_directory_fd,
                db_path.name,
                destination_fd,
                destination_directory_fd,
                destination.name,
                1,
                parent_fd=parent_fd,
            )

    assert destination.stat().st_size <= 1


def test_worker_timeout_terminates_and_reaps_process() -> None:
    from fieldkit.sqlite_read import _run_snapshot_worker

    with pytest.raises(SQLiteSnapshotError, match="did not finish"):
        _run_snapshot_worker(
            [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
            timeout_seconds=0.05,
        )


def test_worker_interrupt_kills_and_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit import sqlite_read

    run = MagicMock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr("fieldkit.sqlite_read.run_bounded_process", run)

    with pytest.raises(KeyboardInterrupt):
        sqlite_read._run_snapshot_worker([sys.executable, "-I", "-c", "pass"], timeout_seconds=1)

    assert run.call_count == 1
    for fd in run.call_args.kwargs["pass_fds"]:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)


def test_worker_does_not_recreate_missing_destination_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    source = tmp_path / "source.db"
    writer = sqlite3.connect(source)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.commit()
    writer.close()
    missing_parent = tmp_path / "removed"
    destination = missing_parent / "snapshot.db"
    parent_read_fd, parent_write_fd = os.pipe()
    os.set_blocking(parent_read_fd, False)
    monkeypatch.setenv("FIELDKIT_SQLITE_PARENT_FD", str(parent_read_fd))

    try:
        rc = worker.create_snapshot([str(source), str(destination), "1048576"])
    finally:
        os.close(parent_read_fd)
        os.close(parent_write_fd)

    assert rc == "invalid"
    assert not missing_parent.exists()


def test_worker_never_removes_a_destination_it_did_not_create(tmp_path: Path) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    destination = tmp_path / "existing.db"
    original = b"operator-owned bytes"
    destination.write_bytes(original)

    result = worker.create_snapshot([str(tmp_path / "missing.db"), str(destination), "1048576"])

    assert result == "invalid"
    assert destination.read_bytes() == original


@pytest.mark.parametrize(
    "invalid_contract",
    ["nonempty", "source", "source-name", "destination-name", "lease", "mode", "duplicate-fd"],
)
def test_worker_never_clears_an_output_before_accepting_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_contract: str
) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    source = tmp_path / "source.db"
    source.write_bytes(b"source")
    destination = tmp_path / "snapshot.db"
    destination.write_bytes(b"preserve" if invalid_contract == "nonempty" else b"")
    destination.chmod(0o644 if invalid_contract == "mode" else 0o600)
    source_directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    source_fd = os.open(source.name, os.O_RDONLY, dir_fd=source_directory_fd)
    destination_directory_fd = os.dup(source_directory_fd)
    destination_fd = os.open(destination.name, os.O_RDWR, dir_fd=destination_directory_fd)
    parent_read_fd, parent_write_fd = os.pipe()
    os.set_blocking(parent_read_fd, False)
    monkeypatch.setenv("FIELDKIT_SQLITE_PARENT_FD", str(parent_read_fd))
    clear_partial = MagicMock(wraps=worker._clear_partial)
    monkeypatch.setattr(worker, "_clear_partial", clear_partial)
    argv = [
        str(source_fd),
        str(source_directory_fd),
        source.name,
        str(destination_fd),
        str(destination_directory_fd),
        destination.name,
        "1048576",
    ]
    if invalid_contract == "source":
        argv[0] = str(os.dup(source_directory_fd))
    elif invalid_contract == "source-name":
        argv[2] = "other.db"
    elif invalid_contract == "destination-name":
        argv[5] = "other.db"
    elif invalid_contract == "lease":
        os.close(parent_write_fd)
        parent_write_fd = -1
    elif invalid_contract == "duplicate-fd":
        argv[4] = str(destination_fd)

    extra_source_fd = int(argv[0]) if int(argv[0]) != source_fd else None
    try:
        result = worker.create_snapshot(argv)
    finally:
        for descriptor in (
            source_fd,
            source_directory_fd,
            destination_fd,
            destination_directory_fd,
            parent_read_fd,
            parent_write_fd,
            extra_source_fd,
        ):
            if descriptor is not None and descriptor >= 0:
                os.close(descriptor)

    assert result == "invalid"
    assert clear_partial.call_count == 0
    assert destination.read_bytes() == (b"preserve" if invalid_contract == "nonempty" else b"")


def test_worker_clears_partial_copy_when_parent_lease_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    source = tmp_path / "source.db"
    source.write_bytes(b"a" * (2 * 1024 * 1024))
    destination = tmp_path / "snapshot.db"
    source_directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    source_fd = os.open(source.name, os.O_RDONLY, dir_fd=source_directory_fd)
    destination_directory_fd = os.dup(source_directory_fd)
    destination_fd = os.open(
        destination.name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=destination_directory_fd
    )
    parent_read_fd, parent_write_fd = os.pipe()
    os.set_blocking(parent_read_fd, False)
    monkeypatch.setenv("FIELDKIT_SQLITE_PARENT_FD", str(parent_read_fd))
    original_read = os.read
    source_read = False

    def read_then_close_parent(fd: int, size: int) -> bytes:
        nonlocal source_read
        chunk = original_read(fd, size)
        if fd != parent_read_fd and not source_read:
            source_read = True
            os.close(parent_write_fd)
        return chunk

    monkeypatch.setattr("fieldkit._sqlite_snapshot_worker.os.read", read_then_close_parent)
    try:
        rc = worker.create_snapshot(
            [
                str(source_fd),
                str(source_directory_fd),
                source.name,
                str(destination_fd),
                str(destination_directory_fd),
                destination.name,
                str(4 * 1024 * 1024),
            ]
        )
    finally:
        for descriptor in (source_fd, source_directory_fd, destination_fd, destination_directory_fd, parent_read_fd):
            os.close(descriptor)
        if not source_read:
            os.close(parent_write_fd)

    assert rc == "invalid"
    assert source_read is True
    assert destination.read_bytes() == b""


def test_worker_sigterm_handler_removes_partial_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    source = tmp_path / "source.db"
    source.write_bytes(b"source bytes")
    destination = tmp_path / "snapshot.db"
    source_directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    source_fd = os.open(source.name, os.O_RDONLY, dir_fd=source_directory_fd)
    destination_directory_fd = os.dup(source_directory_fd)
    destination_fd = os.open(
        destination.name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=destination_directory_fd
    )
    parent_read_fd, parent_write_fd = os.pipe()
    os.set_blocking(parent_read_fd, False)
    monkeypatch.setenv("FIELDKIT_SQLITE_PARENT_FD", str(parent_read_fd))
    original_handler = signal.getsignal(signal.SIGTERM)

    def terminate_during_copy(*_args: object, **_kwargs: object) -> str:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler did not interrupt the copy")

    monkeypatch.setattr(worker, "_digest_fd", terminate_during_copy)
    try:
        rc = worker.create_snapshot(
            [
                str(source_fd),
                str(source_directory_fd),
                source.name,
                str(destination_fd),
                str(destination_directory_fd),
                destination.name,
                "1048576",
            ]
        )
    finally:
        signal.signal(signal.SIGTERM, original_handler)
        for descriptor in (
            source_fd,
            source_directory_fd,
            destination_fd,
            destination_directory_fd,
            parent_read_fd,
            parent_write_fd,
        ):
            os.close(descriptor)

    assert rc == "invalid"
    assert destination.read_bytes() == b""


def test_worker_os_error_does_not_echo_private_source_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    private_name = "private-customer-cache.db"
    rc = worker.create_snapshot([str(tmp_path / private_name), str(tmp_path / "copy.db"), "1024"])

    stderr = capsys.readouterr().err
    assert rc == "invalid"
    assert private_name not in stderr
    assert len(stderr) <= 600


def test_partial_cleanup_failure_has_a_typed_nonpassing_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit import _sqlite_snapshot_worker as worker

    source = tmp_path / "source.db"
    source.write_bytes(b"source")
    destination = tmp_path / "snapshot.db"
    source_directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    source_fd = os.open(source.name, os.O_RDONLY, dir_fd=source_directory_fd)
    destination_directory_fd = os.dup(source_directory_fd)
    destination_fd = os.open(
        destination.name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=destination_directory_fd
    )
    parent_read_fd, parent_write_fd = os.pipe()
    os.set_blocking(parent_read_fd, False)
    monkeypatch.setenv("FIELDKIT_SQLITE_PARENT_FD", str(parent_read_fd))

    def reject_truncate(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("fixture private path must not escape")

    monkeypatch.setattr(os, "ftruncate", reject_truncate)
    try:
        result = worker.create_snapshot(
            [
                str(source_fd),
                str(source_directory_fd),
                source.name,
                str(destination_fd),
                str(destination_directory_fd),
                destination.name,
                "1",
            ]
        )
    finally:
        for descriptor in (
            source_fd,
            source_directory_fd,
            destination_fd,
            destination_directory_fd,
            parent_read_fd,
            parent_write_fd,
        ):
            os.close(descriptor)

    assert result == "cleanup"
