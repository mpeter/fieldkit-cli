"""Fail-closed contracts for the canonical SQLite read-only connection."""

import multiprocessing
import os
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from fieldkit.errors import SQLiteSnapshotError
from fieldkit.sqlite_read import open_sqlite_read_only

pytestmark = pytest.mark.unit


def _hold_external_rollback_transaction(db_path: str, ready: Any, release: Any) -> None:
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE records SET value = 2")
        ready.set()
        release.wait(10)
        writer.rollback()
    finally:
        writer.close()


def _hold_external_reader(db_path: str, ready: Any, release: Any) -> None:
    reader = sqlite3.connect(db_path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM records").fetchall()
        ready.set()
        release.wait(10)
    finally:
        reader.close()


def _snapshot(path: Path) -> tuple[bytes, int, set[str]]:
    return path.read_bytes(), path.stat().st_mtime_ns, {entry.name for entry in path.parent.iterdir()}


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    private_name = "private-customer-missing.db"
    db_path = tmp_path / private_name

    with pytest.raises(FileNotFoundError) as caught:
        open_sqlite_read_only(db_path)

    assert private_name not in str(caught.value)
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert private_name not in diagnostic
    assert str(tmp_path) not in diagnostic
    assert not db_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_non_file_path_is_rejected(tmp_path: Path) -> None:
    private_name = "private-customer-directory"
    db_path = tmp_path / private_name
    db_path.mkdir()

    with pytest.raises(ValueError, match="regular file") as caught:
        open_sqlite_read_only(db_path)

    assert private_name not in str(caught.value)


def test_symlink_loop_is_rejected_without_private_path(tmp_path: Path) -> None:
    private_name = "private-customer-loop.db"
    db_path = tmp_path / private_name
    db_path.symlink_to(private_name)

    with pytest.raises(SQLiteSnapshotError, match="path could not be resolved") as caught:
        open_sqlite_read_only(db_path)

    assert caught.value.reason == "unverified"
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert private_name not in diagnostic
    assert str(tmp_path) not in diagnostic
    assert db_path.is_symlink()
    assert db_path.readlink() == Path(private_name)


@pytest.mark.parametrize("operation", ["resolve", "open"])
def test_acquisition_oserror_traceback_hides_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    db_path = tmp_path / "private-customer-acquisition.db"
    db_path.touch()

    def refuse_path(*args: object, **kwargs: object) -> int:
        raise PermissionError(13, "access denied", str(db_path))

    if operation == "resolve":
        monkeypatch.setattr(Path, "resolve", refuse_path)
    else:
        monkeypatch.setattr("fieldkit.sqlite_read._open_real_directory", refuse_path)

    with pytest.raises(OSError, match="SQLite database") as caught:
        open_sqlite_read_only(db_path)

    assert caught.value.errno == 13
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert db_path.name not in diagnostic
    assert str(tmp_path) not in diagnostic
    assert db_path.read_bytes() == b""


def test_encoded_path_reads_without_mutating_database(tmp_path: Path) -> None:
    db_path = tmp_path / "cache #1?.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value TEXT)")
    writer.execute("INSERT INTO records VALUES ('present')")
    writer.commit()
    writer.close()
    before = _snapshot(db_path)

    conn = open_sqlite_read_only(db_path)
    try:
        snapshot_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        assert snapshot_path.parent != db_path.parent
        assert not snapshot_path.exists()
        row = conn.execute("SELECT value FROM records").fetchone()
        assert row["value"] == "present"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO records VALUES ('forbidden')")
    finally:
        conn.close()

    assert not snapshot_path.parent.exists()
    assert _snapshot(db_path) == before


def test_symlink_to_regular_database_is_allowed_without_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "actual.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (7)")
    writer.commit()
    writer.close()
    alias = tmp_path / "alias.db"
    alias.symlink_to(db_path)
    before = _snapshot(db_path)

    conn = open_sqlite_read_only(alias)
    try:
        row = conn.execute("SELECT value FROM records").fetchone()
        assert row["value"] == 7
    finally:
        conn.close()

    assert alias.is_symlink()
    assert _snapshot(db_path) == before


def test_source_ancestor_swap_after_parent_acquisition_cannot_redirect_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit import sqlite_read

    source_directory = tmp_path / "source"
    source_directory.mkdir()
    db_path = source_directory / "cache.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value TEXT)")
    writer.execute("INSERT INTO records VALUES ('original')")
    writer.commit()
    writer.close()
    replacement = tmp_path / "replacement.db"
    writer = sqlite3.connect(replacement)
    writer.execute("CREATE TABLE records (value TEXT)")
    writer.execute("INSERT INTO records VALUES ('substituted')")
    writer.commit()
    writer.close()
    original_run = sqlite_read._run_snapshot_worker

    def swap_then_run(argv: list[str], *, timeout_seconds: float, pass_fds: tuple[int, ...] = ()) -> str:
        source_directory.rename(tmp_path / "pinned")
        source_directory.mkdir()
        replacement.replace(db_path)
        return original_run(argv, timeout_seconds=timeout_seconds, pass_fds=pass_fds)

    monkeypatch.setattr(sqlite_read, "_run_snapshot_worker", swap_then_run)

    conn = open_sqlite_read_only(db_path)
    try:
        assert conn.execute("SELECT value FROM records").fetchone()[0] == "original"
    finally:
        conn.close()


def test_broken_symlink_is_rejected_without_replacing_it(tmp_path: Path) -> None:
    private_name = "private-customer-broken.db"
    alias = tmp_path / private_name
    alias.symlink_to(tmp_path / "absent.db")

    with pytest.raises(FileNotFoundError) as caught:
        open_sqlite_read_only(alias)

    assert private_name not in str(caught.value)
    assert alias.is_symlink()


def test_live_wal_is_refused_without_mutating_shared_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "live.db"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (11)")
    writer.commit()
    original_paths = sorted(tmp_path.iterdir())
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in original_paths}
    assert {"live.db", "live.db-wal", "live.db-shm"} <= before.keys()

    try:
        with pytest.raises(SQLiteSnapshotError, match="active"):
            open_sqlite_read_only(db_path)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(tmp_path.iterdir())}
        assert after == before
    finally:
        writer.close()


def test_hardlink_alias_wal_writer_cannot_produce_a_stale_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    alias_path = tmp_path / "alias.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (1)")
    writer.commit()
    writer.close()
    os.link(db_path, alias_path)

    alias_writer = sqlite3.connect(alias_path)
    try:
        assert alias_writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        alias_writer.execute("PRAGMA wal_autocheckpoint=0")
        alias_writer.execute("UPDATE records SET value = 2")
        alias_writer.commit()
        assert Path(f"{alias_path}-wal").exists()
        assert not Path(f"{db_path}-wal").exists()
        alias_path.unlink()
        assert db_path.stat().st_nlink == 1

        with pytest.raises(SQLiteSnapshotError) as caught:
            open_sqlite_read_only(db_path)

        assert caught.value.reason == "unverified"
    finally:
        alias_writer.close()


def test_hardlinked_database_is_rejected_without_sidecars(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    alias_path = tmp_path / "alias.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.commit()
    writer.close()
    os.link(db_path, alias_path)

    assert db_path.stat().st_nlink == 2
    assert db_path.read_bytes()[18:20] == b"\x01\x01"

    with pytest.raises(SQLiteSnapshotError) as caught:
        open_sqlite_read_only(db_path)

    assert caught.value.reason == "unverified"


def test_clean_wal_mode_without_sidecars_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "wal-mode.db"
    writer = sqlite3.connect(db_path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (7)")
    writer.commit()
    writer.close()

    assert db_path.read_bytes()[18:20] == b"\x02\x02"
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()

    with pytest.raises(SQLiteSnapshotError) as caught:
        open_sqlite_read_only(db_path)

    assert caught.value.reason == "unverified"


def test_hot_rollback_journal_is_refused_without_reading_torn_pages(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (1)")
    writer.commit()
    writer.close()
    journal_path = Path(f"{db_path}-journal")
    journal_path.write_bytes(b"hot rollback state")
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(tmp_path.iterdir())}

    with pytest.raises(SQLiteSnapshotError, match="rollback journal"):
        open_sqlite_read_only(db_path)

    after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(tmp_path.iterdir())}
    assert after == before


def test_active_rollback_transaction_is_refused_without_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "active.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (1)")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE records SET value = 2")
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(tmp_path.iterdir())}

    try:
        with pytest.raises(SQLiteSnapshotError, match=r"active|rollback journal"):
            open_sqlite_read_only(db_path)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(tmp_path.iterdir())}
        assert after == before
    finally:
        writer.rollback()
        writer.close()


def test_external_writer_conflicts_with_sqlite_lock_range(tmp_path: Path) -> None:
    db_path = tmp_path / "external.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (1)")
    writer.commit()
    writer.close()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_external_rollback_transaction,
        args=(str(db_path), ready, release),
    )
    process.start()
    assert ready.wait(5), "external SQLite writer did not acquire its transaction"
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(tmp_path.iterdir())}

    try:
        with pytest.raises(SQLiteSnapshotError, match="active"):
            open_sqlite_read_only(db_path)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(tmp_path.iterdir())}
        assert after == before
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(2)
    assert process.exitcode == 0


def test_external_reader_can_coexist_with_snapshot_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (7)")
    writer.commit()
    writer.close()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_external_reader, args=(str(db_path), ready, release))
    process.start()
    assert ready.wait(5), "external SQLite reader did not acquire its transaction"

    try:
        conn = open_sqlite_read_only(db_path)
        try:
            assert conn.execute("SELECT value FROM records").fetchone()[0] == 7
        finally:
            conn.close()
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(2)
    assert process.exitcode == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses ordinary mode-bit checks")
def test_mode_0444_database_is_readable(tmp_path: Path) -> None:
    db_path = tmp_path / "readonly.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (11)")
    writer.commit()
    writer.close()
    db_path.chmod(0o444)

    conn = open_sqlite_read_only(db_path)
    try:
        assert conn.execute("SELECT value FROM records").fetchone()[0] == 11
    finally:
        conn.close()


def test_parent_never_opens_replacement_after_worker_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit import sqlite_read

    source = tmp_path / "source.db"
    writer = sqlite3.connect(source)
    writer.execute("CREATE TABLE records (value TEXT)")
    writer.execute("INSERT INTO records VALUES ('original')")
    writer.commit()
    writer.close()
    replacement = tmp_path / "replacement.db"
    writer = sqlite3.connect(replacement)
    writer.execute("CREATE TABLE records (value TEXT)")
    writer.execute("INSERT INTO records VALUES ('substituted')")
    writer.commit()
    writer.close()
    original_run = sqlite_read._run_snapshot_worker

    def replace_then_return(argv: list[str], *, timeout_seconds: float, pass_fds: tuple[int, ...] = ()) -> str:
        status = original_run(argv, timeout_seconds=timeout_seconds, pass_fds=pass_fds)
        destination_fd = int(argv[-4])
        descriptor_root = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"
        destination = Path(f"{descriptor_root}/{destination_fd}").readlink()
        destination.unlink()
        replacement.replace(destination)
        return status

    monkeypatch.setattr(sqlite_read, "_run_snapshot_worker", replace_then_return)

    with pytest.raises(SQLiteSnapshotError, match="descriptor"):
        open_sqlite_read_only(source)


def test_snapshot_byte_budget_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit import sqlite_read

    db_path = tmp_path / "oversized.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.close()
    before = _snapshot(db_path)
    monkeypatch.setattr(sqlite_read, "_MAX_SNAPSHOT_BYTES", 1)

    with pytest.raises(SQLiteSnapshotError, match="snapshot limit"):
        open_sqlite_read_only(db_path)

    assert _snapshot(db_path) == before


@pytest.mark.skipif(not getattr(os, "O_NOATIME", 0), reason="O_NOATIME is unavailable")
def test_snapshot_preserves_source_access_time_when_kernel_supports_it(tmp_path: Path) -> None:
    db_path = tmp_path / "atime.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.execute("INSERT INTO records VALUES (1)")
    writer.commit()
    writer.close()
    old_timestamp_ns = 946_684_800_000_000_000
    os.utime(db_path, ns=(old_timestamp_ns, db_path.stat().st_mtime_ns))
    before = db_path.stat()

    conn = open_sqlite_read_only(db_path)
    try:
        row = conn.execute("SELECT value FROM records").fetchone()
        assert row["value"] == 1
    finally:
        conn.close()

    after = db_path.stat()
    assert after.st_atime_ns == before.st_atime_ns
    assert after.st_mtime_ns == before.st_mtime_ns


def test_snapshot_unlink_failure_is_typed_and_cleans_private_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "source.db"
    writer = sqlite3.connect(db_path)
    writer.execute("CREATE TABLE records (value INTEGER)")
    writer.commit()
    writer.close()
    original_unlink = os.unlink
    private_directory: Path | None = None

    def reject_snapshot_unlink(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal private_directory
        if path == "snapshot.db" and dir_fd is not None:
            descriptor_root = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"
            private_directory = Path(f"{descriptor_root}/{dir_fd}").readlink()
            raise PermissionError("fixture private path must not escape")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", reject_snapshot_unlink)

    with pytest.raises(SQLiteSnapshotError, match="could not be unlinked") as caught:
        open_sqlite_read_only(db_path)

    assert caught.value.reason == "unverified"
    assert private_directory is not None
    assert not private_directory.exists()


def test_connection_interrupt_explicitly_cleans_private_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit import sqlite_read

    db_path = tmp_path / "source.db"
    db_path.write_bytes(b"source")
    cleaned = False

    class OwnedDirectory:
        name = str(tmp_path / "private")

        def cleanup(self) -> None:
            nonlocal cleaned
            cleaned = True

    Path(OwnedDirectory.name).mkdir()
    monkeypatch.setattr("fieldkit.sqlite_read.tempfile.TemporaryDirectory", lambda **_kwargs: OwnedDirectory())
    monkeypatch.setattr(sqlite_read, "_run_snapshot_worker", lambda *_args, **_kwargs: "ready")
    monkeypatch.setattr("fieldkit.sqlite_read.sqlite3.connect", MagicMock(side_effect=KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        open_sqlite_read_only(db_path)

    assert cleaned is True
