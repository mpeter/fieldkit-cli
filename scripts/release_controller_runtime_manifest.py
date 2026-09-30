"""Closed version-one runtime declarations; parsing never executes selected code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, cast

from scripts.json_policy import load_json_bytes
from scripts.release_controller_snapshot import FileExpectation

MAX_MANIFEST_BYTES = 256 * 1024
MAX_RUNTIME_FILES = 256
MAX_RUNTIME_BYTES = 256 * 1024 * 1024
MAX_RUNTIME_FILE_BYTES = 128 * 1024 * 1024
MAX_RUNTIME_ZIP_MEMBERS = 4096
MAX_RUNTIME_ZIP_BYTES = 128 * 1024 * 1024
NativeRole = Literal["interpreter", "loader", "library", "extension", "tool"]
Role = Literal["interpreter", "loader", "library", "extension", "tool", "python_zip", "resource", "lock", "python"]
NATIVE_ROLES = frozenset({"interpreter", "loader", "library", "extension", "tool"})
_ROLES = NATIVE_ROLES | {"python_zip", "resource", "lock", "python"}


def object_fields(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields or any(type(key) is not str for key in value):
        raise ValueError("closed manifest fields are invalid")
    return cast(dict[str, object], value)


def digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("manifest digest is invalid")
    return value


def text(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[\x20-\x7e]{1,1024}", value) is None:
        raise ValueError("manifest text is invalid")
    return value


def canonical_path(value: object, *, absolute: bool = False) -> str:
    value = text(value)
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() != absolute or path.as_posix() != value or len(path.parts) > 32:
        raise ValueError("manifest path must be canonical")
    if any(part in {".", "..", ""} or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None for part in path.parts[absolute:]):
        raise ValueError("manifest path is invalid")
    return value


def runtime_path(value: object) -> str:
    result = canonical_path(value, absolute=True)
    path = PurePosixPath(result)
    if not any(path.is_relative_to(root) and str(path) != root for root in ("/runtime", "/lib", "/lib64")):
        raise ValueError("destination is outside closed runtime roots")
    return result


def runtime_directory(value: object) -> str:
    result = canonical_path(value, absolute=True)
    if result not in ("/runtime", "/lib", "/lib64"):
        runtime_path(result)
    return result


def bounded_int(value: object, maximum: int, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("manifest integer exceeds bounds or is invalid")
    return value


@dataclass(frozen=True)
class RuntimeFile:
    source: str
    destination: str
    expectation: FileExpectation
    role: Role
    stage: Literal["initial", "host"]


@dataclass(frozen=True)
class RuntimePackage:
    name: str
    root: str
    kind: Literal["regular", "namespace"]


@dataclass(frozen=True)
class RuntimeTool:
    name: str
    destination: str
    version: str


@dataclass(frozen=True)
class RuntimeManifest:
    files: tuple[RuntimeFile, ...]
    interpreter: str
    python_version: str
    prefix: str
    stdlib_directory: str
    python_zip: str
    extension_root: str
    package_root: str
    packages: tuple[RuntimePackage, ...]
    library_directories: tuple[str, ...]
    tools: tuple[RuntimeTool, ...]
    lock: str
    bytecode_magic: str
    sourceless_modules: tuple[tuple[str, FileExpectation], ...]

    @property
    def import_paths(self) -> tuple[str, ...]:
        roots = (self.extension_root, self.package_root)
        return (
            self.python_zip,
            *(
                root
                for root in roots
                if any(
                    file.stage == "initial" and PurePosixPath(file.destination).is_relative_to(root)
                    for file in self.files
                )
            ),
        )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_RUNTIME_FILES:
        raise ValueError("manifest list is invalid")
    result = tuple(runtime_directory(item) for item in value)
    if tuple(sorted(set(result))) != result:
        raise ValueError("manifest list must be sorted and unique")
    return result


def validate_file_graph(destinations: tuple[str, ...]) -> tuple[str, ...]:
    """Return synthesized parents, rejecting duplicate files and file/parent conflicts."""
    if len(destinations) > 1024 or len(set(destinations)) != len(destinations):
        raise ValueError("mount graph file capacity or duplicate violation")
    files = set(destinations)
    parents = {str(parent) for value in destinations for parent in PurePosixPath(value).parents if str(parent) != "/"}
    if files & parents or len(parents) > 4096:
        raise ValueError("mount graph file/parent conflict or parent capacity violation")
    if any(
        any(PurePosixPath(value).is_relative_to(root) for root in ("/proc", "/dev", "/scratch"))
        for value in destinations
    ):
        raise ValueError("mount graph collides with a reserved root")
    return tuple(sorted(parents, key=lambda value: (len(PurePosixPath(value).parts), value)))


def parse_runtime_manifest(data: bytes) -> RuntimeManifest:
    if type(data) is not bytes or not 0 < len(data) <= MAX_MANIFEST_BYTES:
        raise ValueError("runtime manifest bytes exceed bound")
    raw = object_fields(
        load_json_bytes(data),
        {"schema_version", "kind", "abi", "python", "files", "packages", "library_directories", "tools", "lock"},
    )
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or raw["kind"] != "fieldkit.runtime-manifest"
        or raw["abi"] != "linux-x86_64-elf64-le"
    ):
        raise ValueError("runtime manifest version, kind or ABI is unsupported")
    rows = raw["files"]
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_RUNTIME_FILES:
        raise ValueError("runtime file count exceeds bounds")
    files: list[RuntimeFile] = []
    for row in rows:
        file_row = object_fields(row, {"source", "destination", "size_bytes", "sha256", "mode", "role", "stage"})
        role, stage = file_row["role"], file_row["stage"]
        if not isinstance(role, str) or role not in _ROLES or stage not in ("initial", "host"):
            raise ValueError("runtime file role or stage is invalid")
        mode = file_row["mode"]
        expected_mode = 0o500 if role in NATIVE_ROLES else 0o400
        if type(mode) is not int or mode != expected_mode:
            raise ValueError("runtime selected mode disagrees with role")
        files.append(
            RuntimeFile(
                canonical_path(file_row["source"]),
                runtime_path(file_row["destination"]),
                FileExpectation(
                    bounded_int(file_row["size_bytes"], MAX_RUNTIME_FILE_BYTES),
                    digest(file_row["sha256"]),
                    cast(Literal[0o400, 0o500], mode),
                ),
                cast(Role, role),
                stage,
            )
        )
    sources = tuple(file.source for file in files)
    if sources != tuple(sorted(set(sources))) or sum(file.expectation.size_bytes for file in files) > MAX_RUNTIME_BYTES:
        raise ValueError("runtime source closure is not sorted, unique and bounded")
    validate_file_graph(sources)
    validate_file_graph(tuple(file.destination for file in files))
    python = object_fields(
        raw["python"],
        {
            "interpreter",
            "version",
            "prefix",
            "zip",
            "extension_root",
            "package_root",
            "bytecode_magic",
            "sourceless_modules",
            "stdlib_directory",
        },
    )
    version = text(python["version"])
    if re.fullmatch(r"3\.[0-9]{1,2}\.[0-9]{1,3}", version) is None:
        raise ValueError("runtime Python version is invalid")
    interpreter, prefix, archive, extension, package = (
        runtime_directory(python[name]) if name == "prefix" else runtime_path(python[name])
        for name in ("interpreter", "prefix", "zip", "extension_root", "package_root")
    )
    if any(
        not PurePosixPath(value).is_relative_to("/runtime")
        for value in (interpreter, prefix, archive, extension, package)
    ):
        raise ValueError("Python layout must remain within runtime")
    stdlib_directory = runtime_path(python["stdlib_directory"])
    if stdlib_directory not in (prefix + "/lib", prefix + "/lib64"):
        raise ValueError("runtime stdlib directory must be a reviewed prefix landmark")
    major, minor, _ = version.split(".")
    if (
        archive != f"{stdlib_directory}/python{major}{minor}.zip"
        or extension != f"{stdlib_directory}/python{major}.{minor}/lib-dynload"
    ):
        raise ValueError("Python relocated landmarks disagree with version")
    by_destination = {file.destination: file for file in files}
    if (
        interpreter not in by_destination
        or by_destination[interpreter].role != "interpreter"
        or archive not in by_destination
        or by_destination[archive].role != "python_zip"
    ):
        raise ValueError("runtime interpreter or Python ZIP is missing")
    packages_raw = raw["packages"]
    if not isinstance(packages_raw, list) or len(packages_raw) > 256:
        raise ValueError("runtime packages exceed bounds")
    packages: list[RuntimePackage] = []
    for row in packages_raw:
        item = object_fields(row, {"name", "root", "kind"})
        name, root, kind = text(item["name"]), runtime_path(item["root"]), item["kind"]
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            or kind not in ("regular", "namespace")
            or root != f"{package}/{name}"
        ):
            raise ValueError("native package layout is invalid")
        if not any(PurePosixPath(file.destination).is_relative_to(root) for file in files):
            raise ValueError("declared package tree is empty")
        if f"{root}/__init__.pyc" in by_destination or (f"{root}/__init__.py" in by_destination) != (kind == "regular"):
            raise ValueError("regular/namespace package declaration mismatch")
        packages.append(RuntimePackage(name, root, kind))
    if tuple(item.name for item in packages) != tuple(sorted({item.name for item in packages})):
        raise ValueError("runtime package names must be sorted and unique")
    for file in files:
        if file.destination.endswith((".pth", "sitecustomize.py", "usercustomize.py")):
            raise ValueError("runtime site hooks are forbidden")
        if file.role == "extension" and not (
            PurePosixPath(file.destination).is_relative_to(extension)
            or any(PurePosixPath(file.destination).is_relative_to(item.root) for item in packages)
        ):
            raise ValueError("native extension has no colocated package or stdlib root")
    tools_raw = raw["tools"]
    if not isinstance(tools_raw, list) or len(tools_raw) != 4:
        raise ValueError("runtime tools must declare the four selected identities")
    tools: list[RuntimeTool] = []
    for row in tools_raw:
        tool = object_fields(row, {"name", "destination", "version"})
        name, destination = text(tool["name"]), runtime_path(tool["destination"])
        if (
            name not in {"python", "uv", "git", "ssh_keygen"}
            or destination not in by_destination
            or by_destination[destination].role not in {"tool", "interpreter"}
        ):
            raise ValueError("runtime tool declaration is invalid")
        tools.append(RuntimeTool(name, destination, text(tool["version"])))
    if (
        tuple(tool.name for tool in tools) != ("git", "python", "ssh_keygen", "uv")
        or len({tool.destination for tool in tools}) != 4
    ):
        raise ValueError("runtime tools must be sorted, unique and exact")
    if next(tool for tool in tools if tool.name == "python").destination != interpreter:
        raise ValueError("selected Python tool disagrees with interpreter")
    lock = runtime_path(raw["lock"])
    if lock not in by_destination or by_destination[lock].role != "lock":
        raise ValueError("selected lock is absent")
    magic = text(python["bytecode_magic"])
    if not version.startswith("3.11.") or magic != "a70d0d0a":
        raise ValueError("runtime bytecode version/magic profile is unsupported")
    raw_modules = python["sourceless_modules"]
    if not isinstance(raw_modules, list) or len(raw_modules) > MAX_RUNTIME_ZIP_MEMBERS:
        raise ValueError("sourceless module declarations exceed bounds")
    modules: list[tuple[str, FileExpectation]] = []
    for row in raw_modules:
        item = object_fields(row, {"path", "size_bytes", "sha256"})
        name = canonical_path(item["path"])
        if not name.endswith(".pyc") or "__pycache__" in PurePosixPath(name).parts:
            raise ValueError("sourceless module path is invalid")
        modules.append(
            (
                name,
                FileExpectation(
                    bounded_int(item["size_bytes"], MAX_RUNTIME_ZIP_BYTES, minimum=16), digest(item["sha256"]), 0o400
                ),
            )
        )
    if tuple(name for name, _ in modules) != tuple(sorted({name for name, _ in modules})):
        raise ValueError("sourceless modules must be sorted and unique")
    return RuntimeManifest(
        tuple(files),
        interpreter,
        version,
        prefix,
        stdlib_directory,
        archive,
        extension,
        package,
        tuple(packages),
        _strings(raw["library_directories"]),
        tuple(tools),
        lock,
        magic,
        tuple(modules),
    )
