"""Contracts for safe extraction of retained release-input artifacts."""

import stat
import zipfile
from pathlib import Path

import pytest

from scripts import release_approval_archive

pytestmark = pytest.mark.unit


def _archive(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as output:
        for name, data in members.items():
            output.writestr(name, data)


def test_extracts_a_bounded_regular_artifact_archive(tmp_path: Path) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"approval-manifest.json": b"{}", "evidence/ledger.json": b"{}"})

    destination = tmp_path / "input"
    release_approval_archive.extract(archive, destination)

    assert (destination / "approval-manifest.json").read_bytes() == b"{}"
    assert stat.S_IMODE((destination / "approval-manifest.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("name", ["../approval-manifest.json", "/approval-manifest.json", "evidence\\ledger.json"])
def test_rejects_an_escaping_or_noncanonical_member(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {name: b"{}"})

    with pytest.raises(ValueError, match="member path"):
        release_approval_archive.extract(archive, tmp_path / "input")


def test_rejects_a_symlink_member(tmp_path: Path) -> None:
    archive = tmp_path / "input.zip"
    link = zipfile.ZipInfo("approval-manifest.json")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(link, b"target")

    with pytest.raises(ValueError, match="unsupported file member"):
        release_approval_archive.extract(archive, tmp_path / "input")


def test_rejects_an_existing_destination(tmp_path: Path) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"approval-manifest.json": b"{}"})
    destination = tmp_path / "input"
    destination.mkdir()

    with pytest.raises(ValueError, match="must not already exist"):
        release_approval_archive.extract(archive, destination)
