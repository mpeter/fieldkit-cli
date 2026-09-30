"""Bounded ELF64 data inspection and exact dependency graph, without native execution."""

from __future__ import annotations

import os
import posixpath
import struct
from dataclasses import dataclass
from pathlib import PurePosixPath

from scripts.release_controller_runtime_manifest import RuntimeManifest, runtime_directory

_MAX_HEADERS = 128
_MAX_DYNAMIC = 4096
_MAX_STRING_TABLE = 1024 * 1024


@dataclass(frozen=True)
class ElfMetadata:
    interpreter: str | None
    needed: tuple[str, ...]
    soname: str | None
    search_paths: tuple[str, ...]


def _read(descriptor: int, offset: int, size: int, file_size: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > file_size:
        raise ValueError("ELF range exceeds file bounds")
    data = os.pread(descriptor, size, offset)
    if len(data) != size:
        raise ValueError("ELF data is truncated")
    return data


def inspect_elf(descriptor: int) -> ElfMetadata:
    size = os.fstat(descriptor).st_size
    header = _read(descriptor, 0, 64, size)
    if header[:7] != b"\x7fELF\x02\x01\x01" or header[7] not in (0, 3) or header[8] != 0:
        raise ValueError("ELF native ABI is unsupported")
    fields = struct.unpack("<HHIQQQIHHHHHH", header[16:])
    kind, machine, version, _, phoff, _, _, ehsize, phsize, phcount, *_ = fields
    if (
        kind not in (2, 3)
        or machine != 62
        or version != 1
        or ehsize != 64
        or phsize != 56
        or not 0 < phcount <= _MAX_HEADERS
    ):
        raise ValueError("ELF header is unsupported")
    headers = [
        struct.unpack("<IIQQQQQQ", _read(descriptor, phoff + index * phsize, phsize, size)) for index in range(phcount)
    ]
    loads: list[tuple[int, int, int]] = []
    dynamic: tuple[int, int] | None = None
    interpreter: str | None = None
    for tag, _, offset, address, _, filesz, memsz, _ in headers:
        if filesz > memsz or offset + filesz > size:
            raise ValueError("ELF segment exceeds bounds")
        if tag == 1:
            loads.append((address, filesz, offset))
        elif tag == 2:
            if dynamic is not None or filesz % 16 or filesz > _MAX_DYNAMIC * 16:
                raise ValueError("ELF dynamic table is invalid")
            dynamic = (offset, filesz)
        elif tag == 3:
            if interpreter is not None or not 2 <= filesz <= 4096:
                raise ValueError("ELF interpreter is invalid")
            raw = _read(descriptor, offset, filesz, size)
            if raw[-1:] != b"\0" or b"\0" in raw[:-1]:
                raise ValueError("ELF interpreter is invalid")
            try:
                interpreter = raw[:-1].decode("ascii")
            except UnicodeError:
                raise ValueError("ELF interpreter is invalid") from None
    if not loads:
        raise ValueError("ELF load graph is empty")
    if dynamic is None:
        return ElfMetadata(interpreter, (), None, ())
    tags: dict[int, list[int]] = {}
    offset, length = dynamic
    terminated = False
    for index in range(length // 16):
        tag, value = struct.unpack("<qQ", _read(descriptor, offset + index * 16, 16, size))
        if tag == 0:
            terminated = True
            break
        tags.setdefault(tag, []).append(value)
    if not terminated:
        raise ValueError("ELF dynamic table is unterminated")
    for tag in (5, 10, 14, 15, 29):
        if len(tags.get(tag, ())) > 1:
            raise ValueError("ELF dynamic singleton is ambiguous")
    if 5 not in tags or 10 not in tags or not 0 < tags[10][0] <= _MAX_STRING_TABLE:
        raise ValueError("ELF dynamic strings are missing or too large")
    address, length = tags[5][0], tags[10][0]
    candidates = [
        offset + address - base
        for base, extent, offset in loads
        if base <= address and address + length <= base + extent
    ]
    if len(candidates) != 1:
        raise ValueError("ELF virtual string mapping is ambiguous")
    strings = _read(descriptor, candidates[0], length, size)

    def string(index: int) -> str:
        if index >= len(strings):
            raise ValueError("ELF string index exceeds bounds")
        end = strings.find(b"\0", index)
        if end < 0 or end - index > 4096:
            raise ValueError("ELF string is unterminated or exceeds bounds")
        try:
            return strings[index:end].decode("ascii")
        except UnicodeError:
            raise ValueError("ELF string is not ASCII") from None

    needed = tuple(string(value) for value in tags.get(1, ()))
    soname = string(tags[14][0]) if 14 in tags else None
    if any(
        not name or "/" in name or "\\" in name or name in (".", "..")
        for name in (*needed, *((soname,) if soname else ()))
    ):
        raise ValueError("ELF dependency name is invalid")
    if len(set(needed)) != len(needed):
        raise ValueError("ELF dependencies are duplicated")
    if 15 in tags and 29 in tags:
        raise ValueError("ELF simultaneous RPATH/RUNPATH is unsupported")
    search = tags.get(29, tags.get(15, []))
    paths = tuple(string(search[0]).split(":")) if search else ()
    return ElfMetadata(interpreter, needed, soname, paths)


def validate_elf_graph(manifest: RuntimeManifest, descriptors: dict[str, int]) -> tuple[tuple[str, ElfMetadata], ...]:
    native = {file.destination: file for file in manifest.files if file.expectation.mode == 0o500}
    metadata = {path: inspect_elf(descriptors[path]) for path in native}
    sonames: dict[str, str] = {}
    for path, elf in metadata.items():
        if native[path].stage == "initial" and elf.soname is not None:
            fingerprint = native[path].expectation.sha256
            if elf.soname in sonames and sonames[elf.soname] != fingerprint:
                raise ValueError("ELF SONAME aliases have conflicting bytes")
            sonames[elf.soname] = fingerprint
    for path, elf in metadata.items():
        if native[path].stage == "host":
            continue
        if elf.interpreter is not None and (
            elf.interpreter not in native
            or native[elf.interpreter].role != "loader"
            or native[elf.interpreter].stage != "initial"
        ):
            raise ValueError("ELF interpreter is outside exact native closure")
        directories: list[str] = []
        for item in elf.search_paths:
            expanded = item.replace("${ORIGIN}", str(PurePosixPath(path).parent)).replace(
                "$ORIGIN", str(PurePosixPath(path).parent)
            )
            if "$" in expanded or not expanded.startswith("/"):
                raise ValueError("ELF search path is unsupported")
            directories.append(runtime_directory(posixpath.normpath(expanded)))
        directories.extend(manifest.library_directories)
        for name in elf.needed:
            candidates = {directory + "/" + name for directory in directories if directory + "/" + name in native}
            if not candidates:
                raise ValueError("ELF dependency is missing")
            fingerprints = {native[candidate].expectation.sha256 for candidate in candidates}
            if len(fingerprints) != 1:
                raise ValueError("ELF dependency resolution is ambiguous")
            if any(native[candidate].stage == "host" for candidate in candidates) and native[path].stage == "initial":
                raise ValueError("initial ELF depends on a host-only grant")
            for candidate in candidates:
                if metadata[candidate].soname not in (None, name):
                    raise ValueError("ELF dependency SONAME disagrees with alias")
    return tuple(sorted(metadata.items()))
