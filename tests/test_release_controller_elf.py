"""Native ABI and dependency closure tests inspect bytes without executing them."""

from __future__ import annotations

import hashlib
import os
import struct
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import release_controller_elf as elf
from scripts.release_controller_runtime_manifest import RuntimeFile, parse_runtime_manifest
from scripts.release_controller_snapshot import FileExpectation
from tests.test_release_controller_runtime_manifest import elf_bytes, encoded, runtime_fixture

pytestmark = pytest.mark.unit


def dynamic_elf(
    *,
    needed: tuple[str, ...] = (),
    interpreter: str | None = None,
    search: str | None = None,
    soname: str | None = None,
) -> bytes:
    result = bytearray(4096)
    strings = bytearray(b"\0")
    tags = [(5, 2048)]
    for tag, name in [
        *((1, name) for name in needed),
        *(([(14, soname)]) if soname else []),
        *(([(29, search)]) if search else []),
    ]:
        tags.append((tag, len(strings)))
        strings.extend(name.encode("ascii") + b"\0")
    tags.extend(((10, len(strings)), (0, 0)))
    count = 3 if interpreter else 2
    result[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    result[16:64] = struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 64, 0, 0, 64, 56, count, 0, 0, 0)
    result[64:120] = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, len(result), len(result), 4096)
    result[120:176] = struct.pack("<IIQQQQQQ", 2, 4, 1024, 1024, 0, 16 * len(tags), 16 * len(tags), 8)
    if interpreter:
        raw = interpreter.encode("ascii") + b"\0"
        result[176:232] = struct.pack("<IIQQQQQQ", 3, 4, 512, 512, 0, len(raw), len(raw), 1)
        result[512 : 512 + len(raw)] = raw
    for index, (tag, value) in enumerate(tags):
        result[1024 + index * 16 : 1040 + index * 16] = struct.pack("<qQ", tag, value)
    result[2048 : 2048 + len(strings)] = strings
    return bytes(result)


def test_dynamic_parser_direct_return(tmp_path: Path) -> None:
    path = tmp_path / "selected"
    path.write_bytes(
        dynamic_elf(
            needed=("libfixture.so",), interpreter="/lib64/ld-fixture.so", search="$ORIGIN/../lib", soname="libself.so"
        )
    )
    descriptor = os.open(path, os.O_RDONLY)
    try:
        result = elf.inspect_elf(descriptor)
        assert result.needed == ("libfixture.so",)
        assert result.interpreter == "/lib64/ld-fixture.so"
        assert result.search_paths == ("$ORIGIN/../lib",)
        assert result.soname == "libself.so"
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    "mutation",
    [
        "machine",
        "endianness",
        "short",
        "header_count",
        "string_index",
        "string_size",
        "unterminated",
        "duplicate_needed",
    ],
)
def test_elf_bad_header_dynamic_data(tmp_path: Path, mutation: str) -> None:
    data = bytearray(dynamic_elf(needed=("libfixture.so",)))
    if mutation == "machine":
        data[18:20] = struct.pack("<H", 183)
    elif mutation == "endianness":
        data[5] = 2
    elif mutation == "short":
        del data[32:]
    elif mutation == "header_count":
        data[56:58] = struct.pack("<H", 65535)
    elif mutation == "string_index":
        data[1048:1056] = struct.pack("<Q", 99999)
    elif mutation == "string_size":
        data[1064:1072] = struct.pack("<Q", 1024**3)
    elif mutation == "unterminated":
        data[1072:1080] = struct.pack("<q", 1)
    else:
        data = bytearray(dynamic_elf(needed=("libfixture.so", "libfixture.so")))
    path = tmp_path / "selected"
    path.write_bytes(data)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="ELF"):
            elf.inspect_elf(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("mutation", ["none", "missing", "interpreter", "escape", "ambiguous", "host_dependency"])
def test_graph_transitive_exact_dependencies(tmp_path: Path, mutation: str) -> None:
    raw, _ = runtime_fixture()
    manifest = parse_runtime_manifest(encoded(raw))
    python_data = dynamic_elf(needed=("libfixture.so",), search="$ORIGIN/../lib")
    if mutation == "interpreter":
        python_data = dynamic_elf(interpreter="/ambient/loader")
    elif mutation == "escape":
        python_data = dynamic_elf(needed=("libfixture.so",), search="$ORIGIN/../../../outside")
    files = list(manifest.files)
    data_by_destination = {file.destination: elf_bytes() for file in files if file.expectation.mode == 0o500}
    data_by_destination[manifest.interpreter] = python_data
    library = dynamic_elf(soname="libfixture.so")
    extra = RuntimeFile(
        "lib/libfixture.so",
        "/runtime/lib/libfixture.so",
        FileExpectation(len(library), hashlib.sha256(library).hexdigest(), 0o500),
        "library",
        "host" if mutation == "host_dependency" else "initial",
    )
    if mutation != "missing":
        files.append(extra)
        data_by_destination[extra.destination] = library
    if mutation == "ambiguous":
        alias_data = elf_bytes()
        alias = RuntimeFile(
            "lib64/libfixture.so",
            "/lib64/libfixture.so",
            FileExpectation(len(alias_data), hashlib.sha256(alias_data).hexdigest(), 0o500),
            "library",
            "initial",
        )
        files.append(alias)
        data_by_destination[alias.destination] = alias_data
    manifest = replace(manifest, files=tuple(files))
    with ExitStack() as stack:
        descriptors: dict[str, int] = {}
        for index, (destination, data) in enumerate(data_by_destination.items()):
            path = tmp_path / str(index)
            path.write_bytes(data)
            descriptor = os.open(path, os.O_RDONLY)
            stack.callback(os.close, descriptor)
            descriptors[destination] = descriptor
        if mutation == "none":
            result = elf.validate_elf_graph(manifest, descriptors)
            assert len(result) == 5
            assert dict(result)[manifest.interpreter].needed == ("libfixture.so",)
        else:
            with pytest.raises(ValueError, match=r"ELF|destination"):
                elf.validate_elf_graph(manifest, descriptors)
