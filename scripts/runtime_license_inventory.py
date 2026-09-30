#!/usr/bin/env python3
"""Observe runtime distribution licenses and PEP 508 markers without QA dependencies."""

from __future__ import annotations

import csv
import io
import json
import os
import platform
import re
import stat
import sys
import sysconfig
from email.message import Message
from importlib import metadata
from pathlib import Path

MARKER_KEYS = frozenset(
    {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_full_version",
        "python_version",
        "sys_platform",
    }
)
MAX_OBSERVATION_BYTES = 2 * 1024 * 1024
OBSERVATIONS_NAME = "runtime-license-observations.json"
PLATFORM_REQUIREMENTS_NAME = "platform-all-extras-requirements.txt"
_PYPI_NAME = re.compile(r"[-_.]+")


def package_url(name: str, version: str) -> str:
    """Return the canonical exact-version PyPI package URL for metadata evidence."""
    return f"pkg:pypi/{_PYPI_NAME.sub('-', name).casefold()}@{version}"


def normalize_license_expression(expression: str) -> str:
    """Preserve the declared expression while normalizing absent metadata."""
    return expression.strip() or "UNKNOWN"


_LICENSE_FIELD_EXPRESSIONS = {
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "3-clause bsd license": "BSD-3-Clause",
    "mit": "MIT",
    "mit license": "MIT",
    "isc": "ISC",
    "apache-2.0 and mit": "Apache-2.0 AND MIT",
    "apache-2.0": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause or apache-2.0": "BSD-3-Clause OR Apache-2.0",
    "mit or apache-2.0": "MIT OR Apache-2.0",
    "mpl-2.0 and mit": "MPL-2.0 AND MIT",
}

_LICENSE_CLASSIFIER_EXPRESSIONS = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}


class _BoundedDistribution(metadata.PathDistribution):
    """Retain stdlib parsing while bounding and confining every metadata read."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._metadata_root = path

    def read_text(self, filename: str | os.PathLike[str]) -> str | None:
        if not isinstance(filename, str) or filename not in {"METADATA", "RECORD"}:
            return None
        try:
            text = read_snapshot(self._metadata_root / filename).decode("utf-8")
        except FileNotFoundError:
            return None
        if filename == "RECORD":
            try:
                if any(len(row) != 3 or not row[0] for row in csv.reader(io.StringIO(text), strict=True)):
                    raise ValueError("installed distribution has malformed records")
            except csv.Error as exc:
                raise ValueError("installed distribution has malformed records") from exc
        return text


def _bounded_distribution(distribution: metadata.Distribution, roots: list[str] | None = None) -> _BoundedDistribution:
    # PathDistribution exposes no public directory accessor. This checked
    # adapter rejects unsupported objects instead of reading their properties.
    path = getattr(distribution, "_path", None)
    if not isinstance(path, Path):
        raise ValueError("installed metadata requires a filesystem distribution")
    root = Path(str(distribution.locate_file(""))).absolute()
    path = path.absolute()
    if path.parent != root or not path.name.endswith(".dist-info") or ".." in root.parts:
        raise ValueError("distribution metadata must remain inside its installed site")
    if roots is not None and root not in {Path(value).absolute() for value in roots}:
        raise ValueError("distribution metadata does not belong to the observed site")
    return _BoundedDistribution(path)


def _canonical_relative_path(path: str) -> bool:
    return (
        bool(path)
        and not any(character in path for character in ("\\", ":", "\x00", "\n", "\r"))
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _license_expression(distribution: metadata.Distribution) -> str:
    bounded = _bounded_distribution(distribution)
    return _metadata_license_expression(bounded, _checked_headers(bounded))


def _checked_headers(distribution: _BoundedDistribution) -> Message:
    headers = distribution.metadata
    if not isinstance(headers, Message):
        raise ValueError("installed metadata requires standard message headers")
    if headers.defects:
        raise ValueError("installed metadata contains malformed headers")
    return headers


def _metadata_license_expression(distribution: _BoundedDistribution, headers: Message) -> str:
    """Return one deterministic SPDX expression from installed package metadata.

    PEP 639 metadata is authoritative. Older distributions commonly expose a
    normalized License field or exactly one Trove classifier; ambiguous or
    unrecognized metadata deliberately remains UNKNOWN for policy review.
    """
    for key in ("License-Expression", "License"):
        if len(headers.get_all(key) or []) > 1:
            raise ValueError("installed distribution metadata contains ambiguous license declarations")
    declared_values = headers.get_all("License-File")
    declared_files = declared_values if isinstance(declared_values, list) else []
    declared_paths = {value for value in declared_files if isinstance(value, str) and value.strip()}
    if len(declared_paths) != len(declared_files) or any(not _canonical_relative_path(path) for path in declared_paths):
        raise ValueError("installed distribution declares unsafe license paths")
    inspected_paths: set[str] = set()
    for file in distribution.files or ():
        package_path = str(file).replace("\\", "/")
        matched = {path for path in declared_paths if package_path == path or package_path.endswith(f"/{path}")}
        if not matched:
            continue
        if not _canonical_relative_path(str(file)):
            raise ValueError("installed distribution records unsafe license paths")
        try:
            contents = read_snapshot(Path(str(distribution.locate_file(file)))).decode("utf-8")
        except (OSError, ValueError) as exc:
            raise ValueError("declared license file could not be inspected") from exc
        if not contents.strip():
            raise ValueError("declared license file must be nonempty")
        inspected_paths.update(matched)
    if inspected_paths != declared_paths:
        raise ValueError("declared license files are missing from the installed record")
    expression = headers.get("License-Expression")
    if isinstance(expression, str) and expression.strip():
        return expression
    license_field = headers.get("License")
    if isinstance(license_field, str):
        normalized = " ".join(license_field.casefold().split())
        if normalized in _LICENSE_FIELD_EXPRESSIONS:
            return _LICENSE_FIELD_EXPRESSIONS[normalized]
    classifier_values = headers.get_all("Classifier")
    classifiers = classifier_values if isinstance(classifier_values, list) else []
    mapped = {
        _LICENSE_CLASSIFIER_EXPRESSIONS[classifier]
        for classifier in classifiers
        if isinstance(classifier, str) and classifier in _LICENSE_CLASSIFIER_EXPRESSIONS
    }
    license_classifiers = [value for value in classifiers if isinstance(value, str) and value.startswith("License ::")]
    if len(mapped) == 1 and len(license_classifiers) == 1:
        return mapped.pop()
    return "UNKNOWN"


def resolved_package_metadata(paths: list[str] | None = None) -> list[dict[str, str]]:
    """Collect the installed distributions that the locked candidate environment resolved."""
    packages: list[dict[str, str]] = []
    observed_packages: set[tuple[str, str, str]] = set()
    distributions = metadata.distributions() if paths is None else metadata.distributions(path=paths)
    for distribution in distributions:
        bounded = _bounded_distribution(distribution, paths)
        headers = _checked_headers(bounded)
        if any(len(headers.get_all(key) or []) != 1 for key in ("Name", "Version")):
            raise ValueError("installed distribution metadata requires unambiguous identity")
        name = headers.get("Name")
        version = headers.get("Version")
        if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip():
            raise ValueError("installed distribution metadata must include a name and version")
        license_expression = _metadata_license_expression(bounded, headers)
        package = (name, version, license_expression)
        if package in observed_packages:
            continue
        observed_packages.add(package)
        packages.append(
            {
                "name": name,
                "version": version,
                "license_expression": license_expression,
            }
        )
    return packages


def marker_environment() -> dict[str, str]:
    """Capture interpreter and platform facts used by PEP 508 marker projection."""
    version = sys.implementation.version
    implementation_version = f"{version.major}.{version.minor}.{version.micro}"
    if version.releaselevel != "final":
        implementation_version += f"{version.releaselevel[0]}{version.serial}"
    return {
        "implementation_name": sys.implementation.name,
        "implementation_version": implementation_version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    }


def validate_marker_environment(raw: object) -> dict[str, str]:
    """Require a complete bounded marker mapping, avoiding implicit controller defaults."""
    if not isinstance(raw, dict) or set(raw) != MARKER_KEYS:
        raise ValueError("runtime marker environment must contain every PEP 508 marker")
    if not all(
        isinstance(value, str)
        and len(value) <= 1024
        and value.isascii()
        and not any(character in value for character in ("\n", "\r", "\x00", "/", "\\", "@"))
        for value in raw.values()
    ):
        raise ValueError("runtime marker environment contains unsafe or invalid values")
    return {str(key): str(value) for key, value in raw.items()}


def parse_observations(contents: bytes) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Validate one bounded runtime snapshot without loading policy or QA packages."""
    if len(contents) > MAX_OBSERVATION_BYTES:
        raise ValueError("runtime license observations exceed the size limit")
    try:
        raw: object = json.loads(contents, object_pairs_hook=_unique_object)
    except RecursionError:
        raise ValueError("runtime license observations contain excessively nested JSON") from None
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "packages", "marker_environment"}:
        raise ValueError("runtime license observations have an unsupported schema")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("runtime license observations have an unsupported schema")
    environment = validate_marker_environment(raw["marker_environment"])
    packages = raw["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValueError("runtime license observations require a nonempty package inventory")
    result: list[dict[str, str]] = []
    observed_urls: set[str] = set()
    for package in packages:
        if not isinstance(package, dict) or set(package) != {"name", "version", "license_expression"}:
            raise ValueError("runtime license observation package has an unsupported schema")
        if not all(isinstance(value, str) and len(value) <= 16384 for value in package.values()):
            raise ValueError("runtime license observation package values must be bounded strings")
        if any(character in package["license_expression"] for character in ("\n", "\r", "\x00", "/", "\\", "@")):
            raise ValueError("runtime license observation expression contains unsafe values")
        name, version = package["name"], package["version"]
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None
            or re.fullmatch(r"[A-Za-z0-9_.!+-]+", version) is None
        ):
            raise ValueError("runtime license observation package identity is invalid")
        url = package_url(name, version)
        if url in observed_urls:
            raise ValueError("runtime license observations contain duplicate package identities")
        observed_urls.add(url)
        result.append({str(key): str(value) for key, value in package.items()})
    return result, environment


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("runtime license observations contain duplicate object keys")
        result[key] = value
    return result


def read_snapshot(path: Path) -> bytes:
    """Capture a bounded file without following symlinks in any path component."""
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("license input path must be canonical")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_OBSERVATION_BYTES:
            raise ValueError("license input must be a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            contents = stream.read(MAX_OBSERVATION_BYTES + 1)
        after = os.fstat(descriptor)
        if len(contents) > MAX_OBSERVATION_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("license input changed during capture")
        return contents
    finally:
        os.close(descriptor)


def observation_bytes(paths: list[str] | None = None) -> bytes:
    """Serialize exactly the installed runtime inventory and its marker environment."""
    payload = {
        "schema_version": 1,
        "packages": resolved_package_metadata(paths),
        "marker_environment": validate_marker_environment(marker_environment()),
    }
    contents = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(contents) > MAX_OBSERVATION_BYTES:
        raise ValueError("runtime license observations exceed the size limit")
    return contents


def main() -> int:
    """Write one bounded observation on stdout; callers retain and hash these bytes."""
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--environment-root" or not sys.flags.no_site:
            raise ValueError("runtime collection requires an explicit environment without site startup")
        root = Path(sys.argv[2])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("runtime environment root must be an absolute directory")
        executable_root = Path(sys.executable).parent.parent
        if root != executable_root or not read_snapshot(root / "pyvenv.cfg").strip():
            raise ValueError("runtime environment must match the invoked virtual environment")
        paths = sorted(
            {
                sysconfig.get_path(name, vars={"base": str(root), "platbase": str(root)})
                for name in ("purelib", "platlib")
            }
        )
        if any(not Path(path).is_dir() or not Path(path).resolve().is_relative_to(root.resolve()) for path in paths):
            raise ValueError("runtime metadata paths must remain inside the virtual environment")
        paths = sorted({str(Path(path).resolve(strict=True)) for path in paths})
        sys.stdout.buffer.write(observation_bytes(paths))
    except (OSError, ValueError):
        sys.stderr.write("runtime license observation failed\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
