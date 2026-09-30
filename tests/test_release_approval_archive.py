"""Contracts for safe extraction of retained release-input artifacts."""

import io
import os
import stat
import zipfile
from pathlib import Path

import pytest

from scripts import release_approval_archive

pytestmark = pytest.mark.unit


def test_archive_cli_omits_operating_system_error_details(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "input.zip"
    name = "private-name-sentinel" + "x" * 256
    _archive(archive, {name: b"retained"})
    assert (
        release_approval_archive.main(
            ["--archive", str(archive), "--destination", str(tmp_path / "private-destination-sentinel")]
        )
        == 2
    )
    assert capsys.readouterr().err == "Release approval archive: ERROR: approval archive filesystem operation failed\n"


def test_archive_cli_success_omits_destination_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"retained.json": b"retained"})
    destination = tmp_path / "private-destination-sentinel"
    assert release_approval_archive.main(["--archive", str(archive), "--destination", str(destination)]) == 0
    assert capsys.readouterr().out == "Release approval archive: PASS\n"
    assert (destination / "retained.json").read_bytes() == b"retained"


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_archive_cli_preserves_unexpected_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exception: type[BaseException]
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise exception("unexpected")

    monkeypatch.setattr(release_approval_archive, "extract", fail)
    with pytest.raises(exception, match="unexpected"):
        release_approval_archive.main(
            ["--archive", str(tmp_path / "input.zip"), "--destination", str(tmp_path / "input")]
        )


@pytest.mark.parametrize(
    ("name", "category"),
    [("../private-name-sentinel\x1b[31m", "unsafe"), ("d//private-name-sentinel\x1b[31m", "noncanonical")],
)
def test_archive_path_diagnostics_omit_untrusted_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, category: str
) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {name: b"retained"})
    diagnostic = f"approval archive member path is {category}"
    with pytest.raises(ValueError) as captured:
        release_approval_archive.extract(archive, tmp_path / "input")
    assert str(captured.value) == diagnostic
    assert release_approval_archive.main(["--archive", str(archive), "--destination", str(tmp_path / "input")]) == 2
    assert capsys.readouterr().err == f"Release approval archive: ERROR: {diagnostic}\n"


@pytest.mark.parametrize(("data", "category"), [(b"", "truncated"), (b"retained-extra", "oversized")])
def test_archive_stream_diagnostics_omit_untrusted_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data: bytes, category: str
) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"private-name-sentinel\x1b[31m": b"retained"})
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *_args, **_kwargs: io.BytesIO(data))
    diagnostic = f"approval archive member is {category}"
    with pytest.raises(ValueError) as captured:
        release_approval_archive.extract(archive, tmp_path / "input")
    assert str(captured.value) == diagnostic
    assert (
        release_approval_archive.main(["--archive", str(archive), "--destination", str(tmp_path / "other-input")]) == 2
    )
    assert capsys.readouterr().err == f"Release approval archive: ERROR: {diagnostic}\n"


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


def test_rejects_an_archive_reached_through_a_symlinked_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    archive = actual / "input.zip"
    _archive(archive, {"approval-manifest.json": b"{}"})
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="real path"):
        release_approval_archive.extract(linked / "input.zip", tmp_path / "input")


def test_rejects_a_destination_beneath_a_symlinked_ancestor(tmp_path: Path) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"approval-manifest.json": b"{}"})
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="real path"):
        release_approval_archive.extract(archive, linked / "input")


def test_extract_keeps_reading_the_open_archive_if_its_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"approval-manifest.json": b"original"})
    pinned = tmp_path / "original.zip"
    original_check = release_approval_archive._check_archive

    def check_archive(source: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        members = original_check(source)
        archive.rename(pinned)
        _archive(archive, {"approval-manifest.json": b"substituted"})
        return members

    monkeypatch.setattr(release_approval_archive, "_check_archive", check_archive)

    destination = tmp_path / "input"
    release_approval_archive.extract(archive, destination)

    assert (destination / "approval-manifest.json").read_bytes() == b"original"


def test_extract_keeps_writing_to_the_open_parent_if_its_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"approval-manifest.json": b"original"})
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    pinned_parent = tmp_path / "pinned"
    attacker_parent = tmp_path / "attacker"
    original_create = release_approval_archive._create_staging_directory

    def create_staging(parent_fd: int, destination_name: str) -> tuple[str, int]:
        result = original_create(parent_fd, destination_name)
        actual_parent.rename(pinned_parent)
        attacker_parent.mkdir()
        actual_parent.symlink_to(attacker_parent, target_is_directory=True)
        return result

    monkeypatch.setattr(release_approval_archive, "_create_staging_directory", create_staging)

    release_approval_archive.extract(archive, actual_parent / "input")

    assert (pinned_parent / "input" / "approval-manifest.json").read_bytes() == b"original"
    assert list(attacker_parent.iterdir()) == []


def test_extract_never_replaces_a_destination_created_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"approval-manifest.json": b"original"})
    destination = tmp_path / "input"
    original_rename = release_approval_archive._rename_no_replace_at

    def race_destination(parent_fd: int, source: str, destination_name: str) -> None:
        os.mkdir(destination_name, dir_fd=parent_fd)
        original_rename(parent_fd, source, destination_name)

    monkeypatch.setattr(release_approval_archive, "_rename_no_replace_at", race_destination)

    with pytest.raises(ValueError, match="must not already exist"):
        release_approval_archive.extract(archive, destination)

    assert list(destination.iterdir()) == []
    retained = [path for path in tmp_path.iterdir() if path.name.startswith(".input-")]
    assert len(retained) == 1
    assert (retained[0] / "approval-manifest.json").read_bytes() == b"original"


def test_extract_rejects_staging_substitution_inside_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"approval-manifest.json": b"original"})
    original_rename = release_approval_archive._rename_no_replace_at
    retained = tmp_path / "retained-staging"
    destination = tmp_path / "input"

    def substitute(parent_fd: int, source: str, destination_name: str) -> None:
        os.rename(source, retained.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.mkdir(source, dir_fd=parent_fd)
        original_rename(parent_fd, source, destination_name)

    monkeypatch.setattr(release_approval_archive, "_rename_no_replace_at", substitute)

    with pytest.raises(ValueError, match="published approval archive identity"):
        release_approval_archive.extract(archive, destination)

    assert (retained / "approval-manifest.json").read_bytes() == b"original"
    assert list(destination.iterdir()) == []


def test_extract_rejects_destination_substitution_after_published_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"approval-manifest.json": b"original"})
    destination = tmp_path / "input"
    retained = tmp_path / "retained-input"
    original_open = release_approval_archive._open_directory_at

    def open_directory(parent_fd: int, name: str) -> int:
        descriptor = original_open(parent_fd, name)
        if name == destination.name:
            destination.rename(retained)
            destination.mkdir()
        return descriptor

    monkeypatch.setattr(release_approval_archive, "_open_directory_at", open_directory)

    with pytest.raises(ValueError, match="identity changed before completion"):
        release_approval_archive.extract(archive, destination)

    assert (retained / "approval-manifest.json").read_bytes() == b"original"
    assert list(destination.iterdir()) == []


def test_cleanup_preserves_a_directory_substituted_for_open_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "input.zip"
    _archive(archive, {"nested/evidence.json": b"original"})
    original_staging = tmp_path / "renamed-original-staging"

    def substitute_staging(parent_fd: int, source: str, destination_name: str) -> None:
        os.rename(source, original_staging.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.mkdir(source, dir_fd=parent_fd)
        replacement_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
        try:
            marker_fd = os.open("marker", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=replacement_fd)
            os.close(marker_fd)
        finally:
            os.close(replacement_fd)
        raise ValueError(f"publication interrupted before {destination_name}")

    monkeypatch.setattr(release_approval_archive, "_rename_no_replace_at", substitute_staging)

    with pytest.raises(ValueError, match="publication interrupted"):
        release_approval_archive.extract(archive, tmp_path / "input")

    replacement = next(path for path in tmp_path.iterdir() if path.name.startswith(".input-"))
    assert (replacement / "marker").read_bytes() == b""
    assert (original_staging / "nested/evidence.json").read_bytes() == b"original"
