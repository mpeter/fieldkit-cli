"""Runtime observations remain independent of QA tools and implicit target markers."""

import io
import json
import os
import subprocess
import sys
import tomllib
import venv
from importlib.metadata import PathDistribution
from pathlib import Path

import pytest
from packaging.markers import default_environment

from scripts import runtime_license_inventory as inventory

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "attack",
    [
        "record-traversal",
        "record-absolute",
        "record-backslash",
        "record-symlink-parent",
        "metadata-symlink",
        "distribution-symlink",
    ],
)
def test_runtime_inventory_never_reads_outside_installed_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    location = site / "example-1.0.dist-info"
    location.mkdir()
    headers = "Metadata-Version: 2.4\nName: example\nVersion: 1.0\nLicense-Expression: MIT\nLicense-File: LICENSE\n"
    (location / "METADATA").write_text(headers, encoding="utf-8")
    (outside / "LICENSE").write_text("Example license text", encoding="utf-8")
    record = "../outside/LICENSE"
    if attack == "record-absolute":
        record = str(outside / "LICENSE")
    elif attack == "record-backslash":
        record = "..\\outside\\LICENSE"
    elif attack == "record-symlink-parent":
        (site / "external").symlink_to(outside, target_is_directory=True)
        record = "external/LICENSE"
    elif attack == "metadata-symlink":
        (outside / "METADATA").write_text(headers, encoding="utf-8")
        (location / "METADATA").unlink()
        (location / "METADATA").symlink_to(outside / "METADATA")
    elif attack == "distribution-symlink":
        (location / "METADATA").unlink()
        location.rmdir()
        (outside / "METADATA").write_text(headers, encoding="utf-8")
        location.symlink_to(outside, target_is_directory=True)
    (location / "RECORD").write_text(f"{record},,\n", encoding="utf-8")
    original_open = os.fdopen
    outside_identities = {(path.stat().st_dev, path.stat().st_ino) for path in outside.iterdir() if path.is_file()}
    outside_reads: list[int] = []

    def open_stream(descriptor: int, mode: str, *, closefd: bool = True) -> io.BufferedReader:
        observed = os.fstat(descriptor)
        if (observed.st_dev, observed.st_ino) in outside_identities:
            outside_reads.append(descriptor)
        stream = original_open(descriptor, mode, closefd=closefd)
        assert isinstance(stream, io.BufferedReader)
        return stream

    monkeypatch.setattr("scripts.runtime_license_inventory.os.fdopen", open_stream)
    try:
        observed = inventory.resolved_package_metadata([str(site)])
    except (OSError, ValueError):
        observed = []

    assert observed == []
    assert outside_reads == []


_REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("field", ["Name", "Version", "License-Expression", "License"])
def test_ambiguous_headers_cannot_become_excepted_unknown_observations(tmp_path: Path, field: str) -> None:
    site = tmp_path / "site-packages"
    location = site / "google_crc32c-1.8.0.dist-info"
    location.mkdir(parents=True)
    headers = "Metadata-Version: 2.4\nName: google-crc32c\nVersion: 1.8.0\nLicense-Expression: MIT\nLicense: MIT\n"
    value = {"Name": "other", "Version": "2.0", "License-Expression": "GPL-3.0-only", "License": "GPL-3.0-only"}[field]
    (location / "METADATA").write_text(headers + f"{field}: {value}\n", encoding="utf-8")
    policy = json.loads((_REPO / "docs/release-readiness/dependency-security-policy.json").read_text(encoding="utf-8"))
    assert any(entry["package_url"] == "pkg:pypi/google-crc32c@1.8.0" for entry in policy["license_exceptions"])

    with pytest.raises(ValueError, match="ambiguous"):
        inventory.observation_bytes([str(site)])


@pytest.mark.parametrize(
    "declared", ["../outside/LICENSE", "/outside/LICENSE", "external\\LICENSE", "external//LICENSE", "./LICENSE"]
)
def test_license_declarations_require_canonical_relative_paths(tmp_path: Path, declared: str) -> None:
    location = tmp_path / "example-1.0.dist-info"
    location.mkdir()
    (location / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: example\nVersion: 1.0\nLicense-Expression: MIT\nLicense-File: {declared}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe license paths"):
        inventory._license_expression(PathDistribution(location))


def test_unrelated_console_script_record_traversal_is_not_a_license_input(tmp_path: Path) -> None:
    location = tmp_path / "example-1.0.dist-info"
    location.mkdir()
    (location / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: example\nVersion: 1.0\nLicense-Expression: MIT\nLicense-File: LICENSE\n",
        encoding="utf-8",
    )
    (location / "RECORD").write_text("../../../bin/example,,\nexample-1.0.dist-info/LICENSE,,\n", encoding="utf-8")
    (location / "LICENSE").write_text("Example license notice", encoding="utf-8")

    observed = inventory._license_expression(PathDistribution(location))

    assert observed == "MIT"


def _observation() -> dict[str, object]:
    return {
        "schema_version": 1,
        "packages": [{"name": "Example_Base", "version": "2.0.0", "license_expression": "MIT"}],
        "marker_environment": {
            "implementation_name": "cpython",
            "implementation_version": "3.11.8",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "platform_release": "6.0.0",
            "platform_system": "Linux",
            "platform_version": "Example kernel",
            "python_full_version": "3.11.8",
            "python_version": "3.11",
            "sys_platform": "linux",
        },
    }


def test_stdlib_collector_matches_pep508_markers() -> None:
    environment = inventory.marker_environment()

    assert environment == default_environment()


def test_runtime_collector_runs_without_site_packages(tmp_path: Path) -> None:
    root = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-S",
            str(_REPO / "scripts" / "runtime_license_inventory.py"),
            "--environment-root",
            str(root),
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["packages"] == []


@pytest.mark.parametrize("startup", ["forged.pth", "sitecustomize.py"])
def test_runtime_collection_does_not_execute_installed_startup(tmp_path: Path, startup: str) -> None:
    root = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    sites = next(root.glob("lib/python*/site-packages")) if sys.platform != "win32" else root / "Lib/site-packages"
    location = sites / "example-1.0.dist-info"
    location.mkdir()
    (location / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: example\nVersion: 1.0\nLicense-Expression: GPL-3.0-only\n", encoding="utf-8"
    )
    sentinel = tmp_path / "startup-executed"
    (sites / startup).write_text(
        f"import pathlib; pathlib.Path({str(sentinel)!r}).touch(); pathlib.Path({str(location / 'METADATA')!r}).write_text('Name: example\\nVersion: 1.0\\nLicense-Expression: MIT\\n')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-S",
            str(_REPO / "scripts" / "runtime_license_inventory.py"),
            "--environment-root",
            str(root),
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["packages"] == [{"name": "example", "version": "1.0", "license_expression": "GPL-3.0-only"}]
    assert payload["marker_environment"]["python_version"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert not sentinel.exists()


def test_parse_observations_keeps_explicit_markers_and_runtime_packages() -> None:
    payload = _observation()

    packages, environment = inventory.parse_observations(json.dumps(payload).encode("utf-8"))

    assert packages == payload["packages"]
    assert environment == payload["marker_environment"]
    assert inventory.package_url(packages[0]["name"], packages[0]["version"]) == "pkg:pypi/example-base@2.0.0"


@pytest.mark.parametrize(
    "attack",
    [
        "missing-markers",
        "extra-marker",
        "unsafe-marker",
        "missing-package-field",
        "duplicate-package",
        "unsafe-expression",
        "boolean-version",
    ],
)
def test_parse_observations_rejects_ambiguous_or_unsafe_inputs(attack: str) -> None:
    payload = _observation()
    environment = payload["marker_environment"]
    packages = payload["packages"]
    assert isinstance(environment, dict) and isinstance(packages, list)
    if attack == "missing-markers":
        del environment["python_version"]
    elif attack == "extra-marker":
        environment["hostname"] = "private-host"
    elif attack == "unsafe-marker":
        environment["platform_version"] = "builder@private-host"
    elif attack == "missing-package-field":
        del packages[0]["license_expression"]
    elif attack == "duplicate-package":
        packages.append(packages[0])
    elif attack == "unsafe-expression":
        packages[0]["license_expression"] = (
            "LicenseRef-/home/example/private"  # pii-guard: ignore — synthetic negative fixture for home-path rejection
        )
    else:
        payload["schema_version"] = True

    with pytest.raises(ValueError, match="runtime"):
        inventory.parse_observations(json.dumps(payload).encode("utf-8"))


def test_parse_observations_rejects_duplicate_json_fields() -> None:
    contents = json.dumps(_observation()).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')

    with pytest.raises(ValueError, match="duplicate object keys"):
        inventory.parse_observations(contents.encode("utf-8"))


def test_parse_observations_translates_bounded_deep_json_to_fixed_error() -> None:
    contents = b'{"private-sentinel":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}"
    assert len(contents) < inventory.MAX_OBSERVATION_BYTES
    with pytest.raises(ValueError, match=r"^runtime license observations contain excessively nested JSON$") as caught:
        inventory.parse_observations(contents)
    assert "private-sentinel" not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "builder@private-host",
        "/home/example/private",  # pii-guard: ignore — synthetic negative fixture for home-path rejection
        "C:\\Users\\example",
        "example\nprivate",
        "example\x00private",
    ],  # pii-guard: ignore — synthetic negative fixture for home-path rejection
)
def test_marker_environment_rejects_private_paths_or_identity_indicators(value: str) -> None:
    environment = _observation()["marker_environment"]
    assert isinstance(environment, dict)
    environment["platform_version"] = value

    with pytest.raises(ValueError, match="unsafe or invalid"):
        inventory.validate_marker_environment(environment)


def test_snapshot_refuses_symlink_and_excessive_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(b"{}")
    alias = tmp_path / "alias.json"
    alias.symlink_to(source)

    with pytest.raises(OSError, match="symbolic links"):
        inventory.read_snapshot(alias)
    source.write_bytes(b" " * (inventory.MAX_OBSERVATION_BYTES + 1))
    with pytest.raises(ValueError, match="bounded regular file"):
        inventory.read_snapshot(source)


@pytest.mark.parametrize(
    "headers", ["License: BSD\n", "License: BSD License\n", "Classifier: License :: OSI Approved :: BSD License\n"]
)
def test_generic_bsd_metadata_does_not_assert_a_clause_count(tmp_path: Path, headers: str) -> None:
    location = tmp_path / "example-1.0.dist-info"
    location.mkdir()
    (location / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: example\nVersion: 1.0\n" + headers, encoding="utf-8"
    )

    assert inventory._license_expression(PathDistribution(location)) == "UNKNOWN"


@pytest.mark.parametrize("failure", ["invalid-utf8", "oversize", "missing", "empty", "symlink", "unreadable"])
@pytest.mark.parametrize("declaration", ["License-Expression: MIT", "License: MIT"])
def test_declared_license_failure_cannot_hide_behind_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, declaration: str
) -> None:
    location = tmp_path / "example-1.0.dist-info"
    location.mkdir()
    (location / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: example\nVersion: 1.0\n{declaration}\nLicense-File: MIT.txt\nLicense-File: OTHER.txt\n",
        encoding="utf-8",
    )
    (location / "RECORD").write_text(
        "example-1.0.dist-info/MIT.txt,,\nexample-1.0.dist-info/OTHER.txt,,\n", encoding="utf-8"
    )
    (location / "MIT.txt").write_text("Permission is hereby granted, free of charge", encoding="utf-8")
    other = location / "OTHER.txt"
    if failure == "invalid-utf8":
        other.write_bytes(b"\xff")
    elif failure == "oversize":
        other.write_bytes(b" " * (inventory.MAX_OBSERVATION_BYTES + 1))
    elif failure == "empty":
        other.write_bytes(b"")
    elif failure == "symlink":
        other.symlink_to(location / "MIT.txt")
    elif failure == "unreadable":
        other.write_text("Example notice", encoding="utf-8")
        original_read = inventory.read_snapshot

        def read(path: Path) -> bytes:
            if path == other:
                raise PermissionError("declared notice unavailable")
            return original_read(path)

        monkeypatch.setattr(inventory, "read_snapshot", read)

    with pytest.raises(ValueError, match="declared license"):
        inventory._license_expression(PathDistribution(location))


@pytest.mark.parametrize("expression", ["MIT OR Apache-2.0", "Apache-2.0 WITH LLVM-exception", "MIT AND BSD-3-Clause"])
def test_authoritative_expression_preserves_operators_despite_multiple_notices(tmp_path: Path, expression: str) -> None:
    location = tmp_path / "example-1.0.dist-info"
    location.mkdir()
    (location / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: example\nVersion: 1.0\nLicense-Expression: {expression}\n"
        "License-File: LICENSE\nLicense-File: AUTHORS\n",
        encoding="utf-8",
    )
    (location / "RECORD").write_text(
        "example-1.0.dist-info/LICENSE,,\nexample-1.0.dist-info/AUTHORS,,\n", encoding="utf-8"
    )
    (location / "LICENSE").write_text("Permission is hereby granted, free of charge", encoding="utf-8")
    (location / "AUTHORS").write_text("Example project contributors", encoding="utf-8")

    observed = inventory._license_expression(PathDistribution(location))

    assert observed == expression


def test_familiar_mit_fragment_with_additional_restrictions_remains_unknown(tmp_path: Path) -> None:
    location = tmp_path / "example-1.0.dist-info"
    location.mkdir()
    (location / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: example\nVersion: 1.0\nLicense-File: LICENSE\n", encoding="utf-8"
    )
    (location / "RECORD").write_text("example-1.0.dist-info/LICENSE,,\n", encoding="utf-8")
    (location / "LICENSE").write_text(
        "Permission is hereby granted, free of charge. Commercial use is forbidden.", encoding="utf-8"
    )

    observed = inventory._license_expression(PathDistribution(location))

    assert observed == "UNKNOWN"


@pytest.mark.parametrize("name", ["boolean-py", "license-expression"])
def test_trusted_review_tools_match_locked_wheel_versions_and_hashes(name: str) -> None:
    lock = tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    lines = [
        line
        for line in (_REPO / "scripts" / "spdx-tool-requirements.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]

    assert len(lines) == 2
    line = next(line for line in lines if line.startswith(f"{name}=="))
    pin, hash_option = line.split()
    _, version = pin.split("==")
    assert packages[name]["version"] == version
    assert hash_option.removeprefix("--hash=") in {wheel["hash"] for wheel in packages[name]["wheels"]}
    assert "license-expression" in {item["name"] for item in packages["fieldkit-cli"]["dev-dependencies"]["dev"]}
    assert "license-expression" not in {item["name"] for item in packages["fieldkit-cli"].get("dependencies", [])}
