"""Ownership and safety contracts for release filesystem primitives."""

import os
from pathlib import Path
from types import ModuleType

import pytest

from scripts import release_approval_archive, release_bundle, release_filesystem

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("consumer", [release_approval_archive, release_bundle])
def test_consumers_use_canonical_publication(monkeypatch: pytest.MonkeyPatch, consumer: ModuleType) -> None:
    calls: list[tuple[int, str, str]] = []

    def publish(parent_fd: int, source: str, destination: str) -> None:
        calls.append((parent_fd, source, destination))

    monkeypatch.setattr(release_filesystem, "rename_no_replace_at", publish)
    result = consumer._rename_no_replace_at(7, "staging", "published")
    assert result is None
    assert calls == [(7, "staging", "published")]


def test_exclusive_publication_preserves_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    parent = release_filesystem.open_real_directory(tmp_path)
    try:
        with pytest.raises(FileExistsError):
            release_filesystem.rename_no_replace_at(parent, source.name, destination.name)
        assert source.is_dir()
        assert destination.is_dir()
    finally:
        os.close(parent)


def test_directory_traversal_rejects_symlink_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        release_filesystem.open_real_directory(link)


def test_publication_uses_retained_parent_after_path_substitution(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "staging").mkdir()
    descriptor = release_filesystem.open_real_directory(parent)
    retained = tmp_path / "retained"
    parent.rename(retained)
    parent.mkdir()
    try:
        release_filesystem.rename_no_replace_at(descriptor, "staging", "published")
        assert (retained / "published").is_dir()
        assert list(parent.iterdir()) == []
    finally:
        os.close(descriptor)


def test_nonregular_input_is_rejected_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)
    descriptor = os.open(path, release_filesystem.file_flags())
    try:
        with pytest.raises(ValueError, match="regular file"):
            release_filesystem.read_regular_file(descriptor, maximum_bytes=7)
    finally:
        os.close(descriptor)


def test_regular_file_read_obeys_bound(tmp_path: Path) -> None:
    path = tmp_path / "input"
    path.write_bytes(b"bounded")
    descriptor = os.open(path, release_filesystem.file_flags())
    try:
        result = release_filesystem.read_regular_file(descriptor, maximum_bytes=7)
        assert result == b"bounded"
        with pytest.raises(ValueError, match="size limit"):
            release_filesystem.read_regular_file(descriptor, maximum_bytes=6)
    finally:
        os.close(descriptor)
