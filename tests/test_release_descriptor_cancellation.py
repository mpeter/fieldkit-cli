"""Cancellation releases owned descriptors without converting the exception."""

import errno
import os
from pathlib import Path

import pytest

from scripts import release_approval_archive, release_approval_input, release_filesystem

pytestmark = pytest.mark.unit


def test_member_directory_cancellation_closes_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_fd = release_filesystem.open_directory(tmp_path)
    duplicate_fds: list[int] = []
    original_dup = os.dup
    cancellation = KeyboardInterrupt("fixture cancellation")

    def duplicate(descriptor: int) -> int:
        result = original_dup(descriptor)
        duplicate_fds.append(result)
        return result

    def cancel(parent_fd: int, name: str) -> int:
        raise cancellation

    monkeypatch.setattr("scripts.release_approval_input.os.dup", duplicate)
    monkeypatch.setattr(release_filesystem, "open_directory_at", cancel)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            release_approval_input._open_member_directory(root_fd, "evidence")
        assert raised.value is cancellation
        assert len(duplicate_fds) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(duplicate_fds[0])
        assert closed.value.errno == errno.EBADF
        assert os.fstat(root_fd).st_ino == tmp_path.stat().st_ino
    finally:
        os.close(root_fd)
        for descriptor in duplicate_fds:
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def test_archive_metadata_cancellation_closes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = tmp_path / "fixture.zip"
    archive.write_bytes(b"fixture")
    opened_fds: list[int] = []
    original_open = os.open
    original_fstat = os.fstat
    cancellation = KeyboardInterrupt("fixture cancellation")

    def open_file(path: str | bytes | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        result = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == archive.name:
            opened_fds.append(result)
        return result

    def metadata(descriptor: int) -> os.stat_result:
        if descriptor in opened_fds:
            raise cancellation
        return original_fstat(descriptor)

    monkeypatch.setattr("scripts.release_approval_archive.os.open", open_file)
    monkeypatch.setattr("scripts.release_approval_archive.os.fstat", metadata)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            release_approval_archive._open_archive(archive)
        assert raised.value is cancellation
        assert len(opened_fds) == 1
        with pytest.raises(OSError) as closed:
            original_fstat(opened_fds[0])
        assert closed.value.errno == errno.EBADF
    finally:
        for descriptor in opened_fds:
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
