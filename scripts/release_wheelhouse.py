"""Collect and verify the runtime wheels needed for supported offline installs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts import release_approval_archive, release_bundle, release_filesystem
else:
    import release_approval_archive
    import release_bundle
    import release_filesystem

from scripts import _release_bundle_evidence

_DOWNLOAD_TIMEOUT_SECONDS = 300
_MANIFEST_NAME = "runtime-wheelhouse.json"
_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MAX_WHEELHOUSE_MEMBERS = 4096


@dataclass(frozen=True)
class RuntimeTarget:
    """One supported interpreter and platform used for offline installation."""

    name: str
    python_version: str
    platform: str


_TARGETS = tuple(
    RuntimeTarget(f"{system}-python{python.replace('.', '')}", python, platform)
    for system, platform in (
        ("linux-x86_64", "manylinux_2_17_x86_64"),
        ("macos-arm64", "macosx_11_0_arm64"),
    )
    for python in ("3.11", "3.12", "3.13", "3.14")
)


def supported_targets() -> tuple[RuntimeTarget, ...]:
    """Return the complete checked-in supported offline-install target matrix."""
    return _TARGETS


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wheel_files(directory: Path) -> tuple[Path, ...]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("wheelhouse target directory is unavailable")
    files = tuple(sorted(directory.iterdir()))
    if not files or any(path.is_symlink() or not path.is_file() or path.suffix != ".whl" for path in files):
        raise ValueError("wheelhouse target must contain only wheel files")
    if len({path.name for path in files}) != len(files):
        raise ValueError("wheelhouse target contains duplicate wheel names")
    return files


def _manifest(targets: tuple[RuntimeTarget, ...], wheelhouse: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "targets": [
            {
                "name": target.name,
                "python_version": target.python_version,
                "platform": target.platform,
                "wheels": [
                    {"name": path.name, "sha256": _digest(path)} for path in _wheel_files(wheelhouse / target.name)
                ],
            }
            for target in targets
        ],
    }


def validate_manifest(data: bytes, actual: Mapping[str, str]) -> dict[str, object]:
    """Verify the supported matrix and exact wheel digests for either boundary."""
    if len(data) > _release_bundle_evidence._MAX_FILE_BYTES:
        raise ValueError("wheelhouse manifest exceeds the size limit")
    manifest = _release_bundle_evidence._json_object(data, "wheelhouse manifest")
    if (
        set(manifest) != {"schema_version", "targets"}
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise ValueError("wheelhouse manifest has an unsupported schema")
    targets = manifest["targets"]
    if not isinstance(targets, list) or len(targets) != len(_TARGETS):
        raise ValueError("wheelhouse manifest has an unsupported target matrix")
    if [target.get("name") if isinstance(target, dict) else None for target in targets] != [
        target.name for target in _TARGETS
    ]:
        raise ValueError("wheelhouse manifest has an unsupported target matrix")
    declared: dict[str, str] = {}
    for target, expected in zip(targets, _TARGETS, strict=True):
        if not isinstance(target, dict) or set(target) != {"name", "python_version", "platform", "wheels"}:
            raise ValueError("wheelhouse manifest target has an unsupported schema")
        if target["python_version"] != expected.python_version or target["platform"] != expected.platform:
            raise ValueError("wheelhouse manifest target does not match the supported matrix")
        wheels = target["wheels"]
        if not isinstance(wheels, list) or not wheels:
            raise ValueError("wheelhouse manifest target has no wheels")
        for wheel in wheels:
            if not isinstance(wheel, dict) or set(wheel) != {"name", "sha256"}:
                raise ValueError("wheelhouse manifest wheel has an unsupported schema")
            name = _release_bundle_evidence._basename(wheel["name"], "wheelhouse manifest wheel")
            release_approval_archive._member_path(name)
            digest = _release_bundle_evidence._digest_string(wheel["sha256"], "wheelhouse manifest wheel")
            relative = f"{expected.name}/{name}"
            if not name.endswith(".whl") or relative in declared:
                raise ValueError("wheelhouse manifest wheel is invalid")
            declared[relative] = digest
    if len(declared) + 1 > MAX_WHEELHOUSE_MEMBERS:
        raise ValueError("wheelhouse archive has an invalid member count")
    if declared != actual:
        raise ValueError("wheelhouse manifest does not match extracted wheels")
    return manifest


@dataclass(frozen=True)
class _CapturedMember:
    name: str
    parent_fd: int
    descriptor: int
    content: bytes


@dataclass(frozen=True)
class _CapturedTarget:
    name: str
    descriptor: int
    names: tuple[str, ...]


def _capture_member(
    parent_fd: int, name: str, relative: str, descriptors: ExitStack, *, maximum_bytes: int
) -> _CapturedMember:
    descriptor = os.open(name, release_filesystem.file_flags(), dir_fd=parent_fd)
    descriptors.callback(os.close, descriptor)
    if os.fstat(descriptor).st_nlink != 1:
        raise ValueError("wheelhouse source member identity is unverified")
    content = release_bundle._read_regular_file(descriptor, relative, maximum_bytes=maximum_bytes)
    return _CapturedMember(relative, parent_fd, descriptor, content)


def _require_directory_binding(path: Path, descriptor: int) -> None:
    current_fd = release_approval_archive._open_real_directory(path, create=False)
    try:
        current, expected = os.fstat(current_fd), os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("wheelhouse directory binding changed")
    finally:
        os.close(current_fd)


def _member_names(descriptor: int) -> tuple[str, ...]:
    return tuple(sorted(os.listdir(descriptor)))


def _verify_source(
    wheelhouse: Path, root_fd: int, targets: tuple[_CapturedTarget, ...], members: tuple[_CapturedMember, ...]
) -> None:
    _require_directory_binding(wheelhouse, root_fd)
    if set(_member_names(root_fd)) != {_MANIFEST_NAME, *(target.name for target in targets)}:
        raise ValueError("wheelhouse source inventory changed")
    for target in targets:
        current = os.stat(target.name, dir_fd=root_fd, follow_symlinks=False)
        expected = os.fstat(target.descriptor)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("wheelhouse target identity changed")
        if _member_names(target.descriptor) != target.names:
            raise ValueError("wheelhouse source inventory changed")
    for member in members:
        current = os.stat(Path(member.name).name, dir_fd=member.parent_fd, follow_symlinks=False)
        expected = os.fstat(member.descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or expected.st_nlink != 1
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise ValueError("wheelhouse source member identity changed")
        if expected.st_size != len(member.content):
            raise ValueError("wheelhouse source bytes changed")
        os.lseek(member.descriptor, 0, os.SEEK_SET)
        if (
            release_bundle._read_regular_file(member.descriptor, member.name, maximum_bytes=len(member.content))
            != member.content
        ):
            raise ValueError("wheelhouse source bytes changed")
    _require_directory_binding(wheelhouse, root_fd)


def _output_digest(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    return _release_bundle_evidence._digest(
        release_bundle._read_regular_file(
            descriptor, "wheelhouse archive", maximum_bytes=_release_bundle_evidence.MAX_WHEELHOUSE_BYTES
        )
    )


def _verify_output_binding(destination: Path, parent_fd: int, descriptor: int, digest: str) -> None:
    _require_directory_binding(destination.parent, parent_fd)
    current = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
    expected = os.fstat(descriptor)
    if (
        not stat.S_ISREG(current.st_mode)
        or expected.st_nlink != 1
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ValueError("wheelhouse archive identity changed")
    if _output_digest(descriptor) != digest:
        raise ValueError("wheelhouse archive bytes changed")
    current = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError("wheelhouse archive identity changed")
    _require_directory_binding(destination.parent, parent_fd)


def _verify_archive_bytes(data: bytes, members: tuple[_CapturedMember, ...]) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as produced:
        if produced.comment:
            raise ValueError("wheelhouse archive has an unproven comment")
        if produced.namelist() != [member.name for member in members]:
            raise ValueError("wheelhouse archive inventory changed")
        for info, member in zip(produced.infolist(), members, strict=True):
            if (
                info.comment
                or info.extra
                or info.compress_type != zipfile.ZIP_STORED
                or info.file_size != len(member.content)
                or info.compress_size != len(member.content)
                or info.date_time != _ARCHIVE_TIMESTAMP
                or info.external_attr != 0o100644 << 16
                or produced.read(info) != member.content
            ):
                raise ValueError("wheelhouse archive member bytes or metadata changed")


def _expected_archive(members: tuple[_CapturedMember, ...]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as expected:
        for member in members:
            info = zipfile.ZipInfo(member.name, date_time=_ARCHIVE_TIMESTAMP)
            info.external_attr = 0o100644 << 16
            with expected.open(info, "w") as output:
                output.write(member.content)
    data = stream.getvalue()
    if len(data) > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
        raise ValueError("wheelhouse archive exceeds the size limit")
    _verify_archive_bytes(data, members)
    return data


def archive(wheelhouse: Path, destination: Path) -> str:
    """Write one reproducible archive bound to proven source and output inodes."""
    with ExitStack() as descriptors:
        root_fd = release_approval_archive._open_real_directory(wheelhouse, create=False)
        descriptors.callback(os.close, root_fd)
        if set(_member_names(root_fd)) != {_MANIFEST_NAME, *(target.name for target in _TARGETS)}:
            raise ValueError("wheelhouse source inventory is invalid")
        manifest = _capture_member(
            root_fd, _MANIFEST_NAME, _MANIFEST_NAME, descriptors, maximum_bytes=_release_bundle_evidence._MAX_FILE_BYTES
        )
        members = [manifest]
        targets: list[_CapturedTarget] = []
        total_bytes = len(manifest.content)
        if total_bytes > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
            raise ValueError("wheelhouse source exceeds the size limit")
        for target in _TARGETS:
            target_fd = release_approval_archive._open_directory_at(root_fd, target.name)
            descriptors.callback(os.close, target_fd)
            names = _member_names(target_fd)
            targets.append(_CapturedTarget(target.name, target_fd, names))
            if not names or any(not name.endswith(".whl") for name in names):
                raise ValueError("wheelhouse target must contain only wheel files")
            for name in names:
                if len(members) >= MAX_WHEELHOUSE_MEMBERS:
                    raise ValueError("wheelhouse archive has an invalid member count")
                member = _capture_member(
                    target_fd,
                    name,
                    f"{target.name}/{name}",
                    descriptors,
                    maximum_bytes=_release_bundle_evidence.MAX_WHEELHOUSE_BYTES - total_bytes,
                )
                total_bytes += len(member.content)
                members.append(member)
        captured = tuple(members)
        retained_targets = tuple(targets)
        validate_manifest(
            manifest.content, {member.name: _release_bundle_evidence._digest(member.content) for member in captured[1:]}
        )
        _verify_source(wheelhouse, root_fd, retained_targets, captured)
        expected_archive = _expected_archive(captured)
        parent_fd = release_approval_archive._open_real_directory(destination.parent, create=False)
        descriptors.callback(os.close, parent_fd)
        output_fd = os.open(
            destination.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
        )
        descriptors.callback(os.close, output_fd)
        with (
            os.fdopen(os.dup(output_fd), "w+b") as stream,
            zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as output,
        ):
            for member in captured:
                info = zipfile.ZipInfo(member.name, date_time=_ARCHIVE_TIMESTAMP)
                info.external_attr = 0o100644 << 16
                output.writestr(info, member.content)
        os.fsync(output_fd)
        if os.fstat(output_fd).st_size > _release_bundle_evidence.MAX_WHEELHOUSE_BYTES:
            raise ValueError("wheelhouse archive exceeds the size limit")
        os.lseek(output_fd, 0, os.SEEK_SET)
        archive_bytes = release_bundle._read_regular_file(
            output_fd, "wheelhouse archive", maximum_bytes=_release_bundle_evidence.MAX_WHEELHOUSE_BYTES
        )
        _verify_archive_bytes(archive_bytes, captured)
        if len(archive_bytes) != len(expected_archive):
            raise ValueError("wheelhouse archive contains unproven leading or trailing bytes")
        if archive_bytes != expected_archive:
            raise ValueError("wheelhouse archive bytes differ from the deterministic reference")
        digest = _release_bundle_evidence._digest(expected_archive)
        _verify_source(wheelhouse, root_fd, retained_targets, captured)
        _verify_output_binding(destination, parent_fd, output_fd, digest)
        return digest


def collect(
    *requirements: Path,
    wheelhouse: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """Download exact runtime wheels for every supported offline-install target."""
    if not requirements:
        raise ValueError("wheelhouse collection requires at least one requirements file")
    if any(
        requirement.is_symlink() or not requirement.is_file() or not requirement.read_text(encoding="utf-8").strip()
        for requirement in requirements
    ):
        raise ValueError("wheelhouse requirements must be non-empty regular files")
    if wheelhouse.exists() or wheelhouse.is_symlink():
        raise ValueError("wheelhouse output must not already exist")
    wheelhouse.mkdir(parents=True)
    for target in _TARGETS:
        destination = wheelhouse / target.name
        result = runner(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--dest",
                str(destination),
                "--require-hashes",
                "--only-binary=:all:",
                "--platform",
                target.platform,
                "--python-version",
                target.python_version,
                *(argument for requirement in requirements for argument in ("--requirement", str(requirement))),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise ValueError(f"wheelhouse download failed for {target.name} with exit status {result.returncode}")
        _wheel_files(destination)
    manifest = _manifest(_TARGETS, wheelhouse)
    (wheelhouse / _MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
