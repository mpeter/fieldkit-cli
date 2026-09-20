"""Collect and verify the runtime wheels needed for supported offline installs."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_DOWNLOAD_TIMEOUT_SECONDS = 300
_MANIFEST_NAME = "runtime-wheelhouse.json"
_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


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


def archive(wheelhouse: Path, destination: Path) -> str:
    """Write one reproducible archive containing only the verified wheelhouse."""
    manifest_path = wheelhouse / _MANIFEST_NAME
    if destination.exists() or destination.is_symlink():
        raise ValueError("wheelhouse archive output must not already exist")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("wheelhouse manifest has an unsupported schema")
    files = [manifest_path]
    for target in _TARGETS:
        files.extend(_wheel_files(wheelhouse / target.name))
    try:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED, strict_timestamps=True) as output:
            for path in files:
                name = path.name if path == manifest_path else str(path.relative_to(wheelhouse))
                info = zipfile.ZipInfo(name, date_time=_ARCHIVE_TIMESTAMP)
                info.external_attr = 0o100644 << 16
                output.writestr(info, path.read_bytes())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return _digest(destination)


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
    try:
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
        (wheelhouse / _MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return manifest
    except BaseException:
        for path in sorted(wheelhouse.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        wheelhouse.rmdir()
        raise
