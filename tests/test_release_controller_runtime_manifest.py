"""Closed runtime graph mutations, without executing any selected bytes."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from copy import deepcopy

import pytest

from scripts import release_controller_runtime_manifest as manifest

pytestmark = pytest.mark.unit


def elf_bytes() -> bytes:
    identity = b"\x7fELF\x02\x01\x01" + bytes(9)
    header = struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, 120, 120, 4096)
    return identity + header + segment


def runtime_fixture() -> tuple[dict[str, object], dict[str, bytes]]:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("encodings/__init__.py", b"# startup fixture\n")
        archive.writestr("encodings/cp437.py", b"# codec fixture\n")
    contents = {
        "bin/python": elf_bytes(),
        "bin/git": elf_bytes(),
        "bin/ssh-keygen": elf_bytes(),
        "bin/uv": elf_bytes(),
        "lock/uv.lock": b"fixture lock\n",
        "lib/python311.zip": stream.getvalue(),
    }
    roles = {
        "bin/python": "interpreter",
        "bin/git": "tool",
        "bin/ssh-keygen": "tool",
        "bin/uv": "tool",
        "lock/uv.lock": "lock",
        "lib/python311.zip": "python_zip",
    }
    files = [
        {
            "source": path,
            "destination": "/runtime/" + path,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": 0o500 if roles[path] in {"tool", "interpreter"} else 0o400,
            "role": roles[path],
            "stage": "initial" if path in {"bin/python", "lib/python311.zip"} else "host",
        }
        for path, data in sorted(contents.items())
    ]
    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "fieldkit.runtime-manifest",
        "abi": "linux-x86_64-elf64-le",
        "python": {
            "interpreter": "/runtime/bin/python",
            "version": "3.11.16",
            "prefix": "/runtime",
            "stdlib_directory": "/runtime/lib",
            "zip": "/runtime/lib/python311.zip",
            "extension_root": "/runtime/lib/python3.11/lib-dynload",
            "package_root": "/runtime/site",
            "bytecode_magic": "a70d0d0a",
            "sourceless_modules": [],
        },
        "files": files,
        "packages": [],
        "library_directories": ["/lib64", "/runtime/lib"],
        "tools": [
            {
                "name": name,
                "destination": "/runtime/bin/" + ("ssh-keygen" if name == "ssh_keygen" else name),
                "version": "3.11.16" if name == "python" else "fixture-version",
            }
            for name in ("git", "python", "ssh_keygen", "uv")
        ],
        "lock": "/runtime/lock/uv.lock",
    }
    return result, contents


def encoded(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def test_manifest_direct_return_and_derived_roots() -> None:
    raw, _ = runtime_fixture()
    result = manifest.parse_runtime_manifest(encoded(raw))
    assert result.interpreter == "/runtime/bin/python"
    assert result.import_paths == ("/runtime/lib/python311.zip",)
    assert len(result.files) == 6


@pytest.mark.parametrize(
    "field,value",
    [("schema_version", True), ("schema_version", 2), ("abi", "aarch64"), ("extra", 1), ("lock", "/usr/uv.lock")],
)
def test_closed_version_manifest_rejects(field: str, value: object) -> None:
    raw, _ = runtime_fixture()
    raw[field] = value
    with pytest.raises(ValueError, match=r"manifest|destination"):
        manifest.parse_runtime_manifest(encoded(raw))


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", ".", "a/"])
def test_paths_reject_ambiguous_names(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        manifest.canonical_path(path)


@pytest.mark.parametrize(
    "destinations", [("/runtime/file", "/runtime/file"), ("/runtime/file", "/runtime/file/child"), ("/proc/evil",)]
)
def test_graph_collision(destinations: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="graph"):
        manifest.validate_file_graph(destinations)


def test_duplicate_json_keys() -> None:
    with pytest.raises(ValueError, match="JSON"):
        manifest.parse_runtime_manifest(b'{"schema_version":1,"schema_version":1}')


def test_source_duplicate_and_bool_size() -> None:
    original, _ = runtime_fixture()
    rows = original["files"]
    assert isinstance(rows, list)
    raw = deepcopy(original)
    changed = raw["files"]
    assert isinstance(changed, list)
    changed[0]["size_bytes"] = True
    with pytest.raises(ValueError, match="integer"):
        manifest.parse_runtime_manifest(encoded(raw))
    rows.append(rows[0])
    with pytest.raises(ValueError, match="source closure"):
        manifest.parse_runtime_manifest(encoded(original))


@pytest.mark.parametrize("kind", ["regular", "namespace"])
def test_package_declaration_rejects_on_disk_sourceless_initializer(kind: str) -> None:
    raw, contents = runtime_fixture()
    rows = raw["files"]
    assert isinstance(rows, list)
    data = bytes.fromhex("a70d0d0a") + bytes(12)
    rows.append(
        {
            "source": "site/namespace_fixture/__init__.pyc",
            "destination": "/runtime/site/namespace_fixture/__init__.pyc",
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": 0o400,
            "role": "python",
            "stage": "initial",
        }
    )
    rows.sort(key=lambda row: row["source"])
    raw["packages"] = [{"name": "namespace_fixture", "root": "/runtime/site/namespace_fixture", "kind": kind}]
    with pytest.raises(ValueError, match="namespace package declaration mismatch"):
        manifest.parse_runtime_manifest(encoded(raw))
    assert contents["bin/python"] == elf_bytes()
