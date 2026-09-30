"""Stable text snapshots reject unsafe inputs without leaking descriptors."""

import os
from pathlib import Path

import pytest

from fieldkit.util.text_snapshot import read_text_snapshot

pytestmark = pytest.mark.unit


def test_snapshot_binds_exact_utf8_to_file_identity(tmp_path: Path) -> None:
    path = tmp_path / "text.md"
    path.write_text("A meeting\n", encoding="utf-8")
    snapshot = read_text_snapshot(path, max_bytes=100)
    assert snapshot.content == "A meeting\n"
    info = path.stat()
    assert snapshot.identity == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_snapshot_limit_is_rejected_before_io(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="Invalid text snapshot byte limit"):
        read_text_snapshot(tmp_path / "absent.md", max_bytes=limit)
    assert list(tmp_path.iterdir()) == []


def test_snapshot_refuses_change_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "text.md"
    path.write_text("Original", encoding="utf-8")
    original_stat = os.fstat

    def race(fd: int) -> os.stat_result:
        info = original_stat(fd)
        path.write_text("Concurrent edit", encoding="utf-8")
        return info

    monkeypatch.setattr("fieldkit.util.text_snapshot.os.fstat", race)
    with pytest.raises(ValueError, match="Cannot read stable regular text file"):
        read_text_snapshot(path, max_bytes=100)
    assert path.read_text(encoding="utf-8") == "Concurrent edit"


def test_directory_open_failure_closes_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_open = os.open
    opened: list[int] = []

    def record(path: Path, flags: int) -> int:
        fd = original_open(path, flags)
        opened.append(fd)
        return fd

    monkeypatch.setattr("fieldkit.util.text_snapshot.os.open", record)
    with pytest.raises(ValueError, match="Cannot read stable regular text file"):
        read_text_snapshot(tmp_path, max_bytes=100)
    assert len(opened) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])
