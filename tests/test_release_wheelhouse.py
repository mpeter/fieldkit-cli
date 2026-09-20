"""Contracts for locked runtime wheelhouse collection."""

import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import release_wheelhouse

pytestmark = pytest.mark.unit


def test_collector_downloads_hash_checked_binary_wheels_for_every_supported_target(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("click==8.5.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        destination = Path(command[command.index("--dest") + 1])
        destination.mkdir()
        (destination / "click-8.5.0-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(command, 0, "", "")

    manifest = release_wheelhouse.collect(requirements, wheelhouse=tmp_path / "wheelhouse", runner=runner)

    targets = manifest["targets"]
    assert isinstance(targets, list)
    assert [target["name"] for target in targets if isinstance(target, dict)] == [
        target.name for target in release_wheelhouse.supported_targets()
    ]
    assert len(commands) == 8
    assert all("--require-hashes" in command and "--only-binary=:all:" in command for command in commands)
    assert all(
        (tmp_path / "wheelhouse" / target.name / "click-8.5.0-py3-none-any.whl").is_file()
        for target in release_wheelhouse.supported_targets()
    )


def test_collector_removes_partial_output_when_a_target_cannot_supply_wheels(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("click==8.5.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "no compatible wheel")

    wheelhouse = tmp_path / "wheelhouse"
    with pytest.raises(ValueError, match="linux-x86_64-python311"):
        release_wheelhouse.collect(requirements, wheelhouse=wheelhouse, runner=runner)

    assert wheelhouse.exists() is False


def test_collector_combines_multiple_locked_requirement_receipts(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-requirements.txt"
    build = tmp_path / "build-requirements.txt"
    for requirements in (runtime, build):
        requirements.write_text("click==8.5.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        destination = Path(command[command.index("--dest") + 1])
        destination.mkdir()
        (destination / "click-8.5.0-py3-none-any.whl").write_bytes(b"wheel")
        assert [Path(command[index + 1]) for index, value in enumerate(command) if value == "--requirement"] == [
            runtime,
            build,
        ]
        return subprocess.CompletedProcess(command, 0, "", "")

    release_wheelhouse.collect(runtime, build, wheelhouse=tmp_path / "wheelhouse", runner=runner)


def test_archive_is_reproducible_and_preserves_the_manifest_and_target_wheels(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for target in release_wheelhouse.supported_targets():
        target_dir = wheelhouse / target.name
        target_dir.mkdir()
        (target_dir / "click-8.5.0-py3-none-any.whl").write_bytes(b"wheel")
    (wheelhouse / "runtime-wheelhouse.json").write_text('{"schema_version": 1}\n', encoding="utf-8")

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    assert release_wheelhouse.archive(wheelhouse, first) == release_wheelhouse.archive(wheelhouse, second)
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "runtime-wheelhouse.json",
            *[f"{target.name}/click-8.5.0-py3-none-any.whl" for target in release_wheelhouse.supported_targets()],
        ]
