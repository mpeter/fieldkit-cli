"""Contracts for locked runtime wheelhouse collection."""

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import _release_bundle_evidence, release_wheelhouse

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mutation", ["trailing-bytes", "member-comment", "extra-field", "archive-comment"])
def test_archive_refuses_bytes_or_metadata_absent_from_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    wheelhouse = _valid_wheelhouse(tmp_path)
    destination = tmp_path / "archive.zip"
    if mutation == "trailing-bytes":
        fsync = os.fsync

        def append_after_fsync(descriptor: int) -> None:
            fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_END)
            os.write(descriptor, b"unproven bytes")

        monkeypatch.setattr(os, "fsync", append_after_fsync)
    else:
        writestr = zipfile.ZipFile.writestr

        def taint_member(output: zipfile.ZipFile, info: zipfile.ZipInfo, content: bytes) -> None:
            if mutation == "member-comment":
                info.comment = b"unproven comment"
            elif mutation == "extra-field":
                info.extra = b"\xfe\xca\x01\x00x"
            else:
                output.comment = b"unproven archive comment"
            writestr(output, info, content)

        monkeypatch.setattr(zipfile.ZipFile, "writestr", taint_member)
    before = set(Path("/proc/self/fd").iterdir())
    with pytest.raises(ValueError, match=r"bytes|metadata|comment"):
        release_wheelhouse.archive(wheelhouse, destination)
    assert destination.is_file()
    assert set(Path("/proc/self/fd").iterdir()) == before


def _valid_wheelhouse(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for target in release_wheelhouse.supported_targets():
        target_dir = wheelhouse / target.name
        target_dir.mkdir()
        (target_dir / "click-8.5.0-py3-none-any.whl").write_bytes(b"wheel")
    manifest = release_wheelhouse._manifest(release_wheelhouse.supported_targets(), wheelhouse)
    (wheelhouse / "runtime-wheelhouse.json").write_text(json.dumps(manifest), encoding="utf-8")
    return wheelhouse


@pytest.mark.parametrize(
    "mutation",
    [
        "boolean-version",
        "top-extra",
        "target-extra",
        "wrong-platform",
        "wrong-version",
        "target-order",
        "missing-target",
        "duplicate-wheel",
        "unsafe-wheel",
        "invalid-hash",
        "wheel-extra",
    ],
)
def test_canonical_manifest_validator_rejects_unsupported_contracts(tmp_path: Path, mutation: str) -> None:
    wheelhouse = _valid_wheelhouse(tmp_path)
    manifest = json.loads((wheelhouse / "runtime-wheelhouse.json").read_bytes())
    target = manifest["targets"][0]
    wheel = target["wheels"][0]
    if mutation == "boolean-version":
        manifest["schema_version"] = True
    elif mutation == "top-extra":
        manifest["extra"] = "extra"
    elif mutation == "target-extra":
        target["extra"] = "extra"
    elif mutation == "wrong-platform":
        target["platform"] = "other"
    elif mutation == "wrong-version":
        target["python_version"] = "9.9"
    elif mutation == "target-order":
        manifest["targets"].reverse()
    elif mutation == "missing-target":
        manifest["targets"].pop()
    elif mutation == "duplicate-wheel":
        target["wheels"].append(wheel)
    elif mutation == "unsafe-wheel":
        wheel["name"] = "../other.whl"
    elif mutation == "invalid-hash":
        wheel["sha256"] = "g" * 64
    else:
        wheel["extra"] = "extra"
    actual = {
        f"{target.name}/click-8.5.0-py3-none-any.whl": hashlib.sha256(b"wheel").hexdigest()
        for target in release_wheelhouse.supported_targets()
    }
    with pytest.raises(ValueError, match="manifest"):
        release_wheelhouse.validate_manifest(json.dumps(manifest).encode(), actual)


def test_archive_enforces_source_budget_before_creating_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheelhouse = _valid_wheelhouse(tmp_path)
    monkeypatch.setattr(_release_bundle_evidence, "MAX_WHEELHOUSE_BYTES", 16)
    with pytest.raises(ValueError, match="size limit"):
        release_wheelhouse.archive(wheelhouse, tmp_path / "archive.zip")
    assert not (tmp_path / "archive.zip").exists()


def test_archive_refuses_late_same_inode_digest_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheelhouse = _valid_wheelhouse(tmp_path)
    destination = tmp_path / "archive.zip"
    output_digest = release_wheelhouse._output_digest

    def replace_bytes_before_digest(descriptor: int) -> str:
        os.pwrite(descriptor, b"changed", 0)
        return output_digest(descriptor)

    monkeypatch.setattr(release_wheelhouse, "_output_digest", replace_bytes_before_digest)
    before = set(Path("/proc/self/fd").iterdir())
    with pytest.raises(ValueError, match="archive bytes changed"):
        release_wheelhouse.archive(wheelhouse, destination)
    assert destination.read_bytes().startswith(b"changed")
    assert set(Path("/proc/self/fd").iterdir()) == before


@pytest.mark.parametrize("mutation", ["minimal", "duplicate-json", "wrong-hash", "extra-file", "extra-wheel"])
def test_archive_rejects_unproven_source_before_output(tmp_path: Path, mutation: str) -> None:
    wheelhouse = _valid_wheelhouse(tmp_path)
    manifest = wheelhouse / "runtime-wheelhouse.json"
    if mutation == "minimal":
        manifest.write_text('{"schema_version":1}', encoding="utf-8")
    elif mutation == "duplicate-json":
        manifest.write_text('{"schema_version":1,' + manifest.read_text(encoding="utf-8")[1:], encoding="utf-8")
    elif mutation == "wrong-hash":
        wheel = next((wheelhouse / release_wheelhouse.supported_targets()[0].name).iterdir())
        wheel.write_bytes(b"changed")
    elif mutation == "extra-file":
        (wheelhouse / "extra").write_bytes(b"extra")
    else:
        (wheelhouse / release_wheelhouse.supported_targets()[0].name / "extra.whl").write_bytes(b"extra")
    destination = tmp_path / "archive.zip"
    expected_error = r"^invalid JSON input$" if mutation == "duplicate-json" else r"manifest|inventory"
    with pytest.raises(ValueError, match=expected_error):
        release_wheelhouse.archive(wheelhouse, destination)
    assert not destination.exists()


@pytest.mark.parametrize("mutation", ["output", "source-manifest", "source-wheel", "source-root", "output-parent"])
def test_archive_refuses_namespace_or_source_change_during_real_zip_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    wheelhouse = _valid_wheelhouse(tmp_path)
    parent = tmp_path / "output"
    parent.mkdir()
    destination = parent / "archive.zip"
    write_member = zipfile.ZipFile.writestr
    mutated = False

    def mutate_after_member(output: zipfile.ZipFile, info: zipfile.ZipInfo, content: bytes) -> None:
        nonlocal mutated
        write_member(output, info, content)
        if mutated:
            return
        mutated = True
        if mutation == "output":
            destination.rename(tmp_path / "retained.zip")
            destination.write_bytes(b"sentinel")
        elif mutation == "source-manifest":
            path = wheelhouse / "runtime-wheelhouse.json"
            content = path.read_bytes()
            path.unlink()
            path.write_bytes(content)
        elif mutation == "source-wheel":
            (wheelhouse / release_wheelhouse.supported_targets()[0].name / "click-8.5.0-py3-none-any.whl").write_bytes(
                b"changed"
            )
        elif mutation == "source-root":
            wheelhouse.rename(tmp_path / "retained-source")
            wheelhouse.mkdir()
            (wheelhouse / "sentinel").write_bytes(b"sentinel")
        else:
            parent.rename(tmp_path / "retained-output")
            parent.mkdir()
            (parent / "sentinel").write_bytes(b"sentinel")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", mutate_after_member)
    before = set(Path("/proc/self/fd").iterdir())
    with pytest.raises((OSError, ValueError), match=r"binding|changed|identity|inventory"):
        release_wheelhouse.archive(wheelhouse, destination)
    assert mutated
    assert set(Path("/proc/self/fd").iterdir()) == before
    if mutation == "output":
        assert destination.read_bytes() == b"sentinel"
    elif mutation == "output-parent":
        assert set(parent.iterdir()) == {parent / "sentinel"}


@pytest.mark.parametrize("failure", ["concurrent-create", "replacement", "partial"])
def test_archive_failure_retains_partial_and_concurrent_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    wheelhouse = _valid_wheelhouse(tmp_path)
    destination = tmp_path / "archive.zip"
    original = tmp_path / "retained-original.zip"
    zip_file = zipfile.ZipFile
    write_member = zipfile.ZipFile.writestr

    if failure == "concurrent-create":
        open_file = os.open

        def occupy_before_exclusive_create(
            file: str | Path,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if file == "archive.zip" and flags & os.O_CREAT:
                destination.write_bytes(b"unrelated")
            return open_file(file, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", occupy_before_exclusive_create)
    else:

        def fail_after_real_member(output: zipfile.ZipFile, info: zipfile.ZipInfo, content: bytes) -> None:
            write_member(output, info, content)
            if failure == "replacement":
                destination.rename(original)
                destination.write_bytes(b"unrelated")
            raise OSError("archive member failure")

        monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_after_real_member)

    with pytest.raises(OSError, match=r"File exists|archive member failure"):
        release_wheelhouse.archive(wheelhouse, destination)

    if failure == "partial":
        assert destination.is_file()
        with zip_file(destination) as retained:
            assert retained.namelist() == ["runtime-wheelhouse.json"]
    else:
        assert destination.read_bytes() == b"unrelated"
        if failure == "replacement":
            assert original.is_file()


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


def test_collector_retains_partial_output_when_a_target_cannot_supply_wheels(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("click==8.5.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "no compatible wheel")

    wheelhouse = tmp_path / "wheelhouse"
    with pytest.raises(ValueError, match="linux-x86_64-python311"):
        release_wheelhouse.collect(requirements, wheelhouse=wheelhouse, runner=runner)

    assert wheelhouse.is_dir()


def test_collector_failure_preserves_replacement_output(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("click==8.5.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    wheelhouse = tmp_path / "wheelhouse"
    original = tmp_path / "retained-original"

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        wheelhouse.rename(original)
        wheelhouse.mkdir()
        (wheelhouse / "sentinel").write_bytes(b"unrelated")
        return subprocess.CompletedProcess(command, 1, "", "no compatible wheel")

    with pytest.raises(ValueError, match="download failed"):
        release_wheelhouse.collect(requirements, wheelhouse=wheelhouse, runner=runner)

    assert (wheelhouse / "sentinel").read_bytes() == b"unrelated"
    assert original.is_dir()


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
    wheelhouse = _valid_wheelhouse(tmp_path)

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    assert release_wheelhouse.archive(wheelhouse, first) == release_wheelhouse.archive(wheelhouse, second)
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "runtime-wheelhouse.json",
            *[f"{target.name}/click-8.5.0-py3-none-any.whl" for target in release_wheelhouse.supported_targets()],
        ]
