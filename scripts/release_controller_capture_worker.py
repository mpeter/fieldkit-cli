"""Trusted host-only capture process with bounded descriptor protocol and hard deadline."""

from __future__ import annotations

import array
import hashlib
import io
import json
import os
import resource
import select
import signal
import socket
import stat
import struct
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, cast

from scripts import release_filesystem
from scripts.json_policy import load_json_bytes
from scripts.release_controller_bootstrap import SelectionBoundControllerCapture
from scripts.release_controller_elf import ElfMetadata, validate_elf_graph
from scripts.release_controller_execution_input import (
    MAX_HELD_FILES,
    ExecutionAuthorityState,
    HeldControllerExecution,
    MountedSnapshot,
)
from scripts.release_controller_runtime_manifest import (
    MAX_RUNTIME_ZIP_BYTES,
    MAX_RUNTIME_ZIP_MEMBERS,
    NATIVE_ROLES,
    RuntimeManifest,
    bounded_int,
    canonical_path,
    digest,
    object_fields,
    validate_file_graph,
)
from scripts.release_controller_snapshot import (
    FileExpectation,
    SealedFileSnapshot,
    SnapshotBudget,
    SnapshotIdentity,
    capture_regular_file,
    capture_stream,
    validate_snapshot_descriptor,
)

_PACKET_BYTES = 8192
_CLEANUP_SECONDS = 2.0
_REQUIRED_INPUT = frozenset(
    {
        "approval-manifest.json",
        "private-candidate-report.json",
        "public-candidate/report.json",
        "public-candidate/bundle/SHA256SUMS",
        "evidence/ledger.json",
        "cutover-record.json",
    }
)


def _metadata(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_file(path: Path) -> int:
    parent = release_filesystem.open_real_directory(path.parent)
    try:
        descriptor = os.open(path.name, release_filesystem.file_flags(), dir_fd=parent)
    finally:
        os.close(parent)
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_size > value.st_blocks * 512:
            raise ValueError("capture source must be nonsparse regular data")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _scan(
    root: int, manifest: RuntimeManifest, budget: SnapshotBudget, stack: ExitStack, *, capture: bool
) -> tuple[dict[str, tuple[int, ...]], tuple[MountedSnapshot, ...]]:
    files = {file.source: file for file in manifest.files}
    parents = {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != "."}
    observed: dict[str, tuple[int, ...]] = {}
    retained: list[MountedSnapshot] = []

    def walk(descriptor: int, prefix: str) -> None:
        budget.check_deadline()
        with os.scandir(descriptor) as entries:
            for entry in entries:
                budget.check_deadline()
                name = f"{prefix}/{entry.name}" if prefix else entry.name
                canonical_path(name)
                if name in observed or len(observed) >= 8192 + len(files):
                    raise ValueError("runtime scan has duplicate paths or exceeds bound")
                metadata = entry.stat(follow_symlinks=False)
                observed[name] = _metadata(metadata)
                if stat.S_ISDIR(metadata.st_mode):
                    if name not in parents:
                        raise ValueError("runtime contains an unexpected directory")
                    child = release_filesystem.open_directory_at(descriptor, entry.name)
                    try:
                        if _metadata(os.fstat(child)) != observed[name]:
                            raise ValueError("runtime directory changed during capture")
                        walk(child, name)
                        if _metadata(os.fstat(child)) != observed[name]:
                            raise ValueError("runtime directory changed during capture")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(metadata.st_mode):
                    if (
                        name not in files
                        or metadata.st_size != files[name].expectation.size_bytes
                        or metadata.st_size > metadata.st_blocks * 512
                    ):
                        raise ValueError("runtime source closure or nonsparse size mismatch")
                    if capture:
                        source = os.open(entry.name, release_filesystem.file_flags(), dir_fd=descriptor)
                        try:
                            if _metadata(os.fstat(source)) != observed[name]:
                                raise ValueError("runtime source changed during capture")
                            file = files[name]
                            snapshot = stack.enter_context(
                                capture_regular_file(source, file.expectation, budget=budget)
                            )
                            retained.append(MountedSnapshot(file.destination, snapshot, file.stage, file.role))
                            if _metadata(os.fstat(source)) != observed[name]:
                                raise ValueError("runtime source changed during capture")
                        finally:
                            os.close(source)
                else:
                    raise ValueError("runtime members must be regular files/directories")

    walk(root, "")
    if set(observed) != set(files) | parents:
        raise ValueError("runtime source closure is missing expected files/parents")
    return observed, tuple(retained)


def _zip_preflight(stream: BinaryIO, *, maximum_members: int) -> None:
    """Bound central-directory allocation before ZipFile interprets it; reject ZIP64."""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    start = max(0, size - 65557)
    stream.seek(start)
    tail = stream.read(size - start)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or offset + 22 > len(tail):
        raise ValueError("ZIP end record is missing")
    _, disk, directory_disk, disk_count, count, directory_size, directory_offset, comment = struct.unpack(
        "<4s4H2IH", tail[offset : offset + 22]
    )
    if (
        disk
        or directory_disk
        or disk_count != count
        or not 0 < count <= maximum_members
        or directory_offset + directory_size != start + offset
        or offset + 22 + comment != len(tail)
        or directory_size > maximum_members * 4608
    ):
        raise ValueError("ZIP central directory exceeds closed bounds or is unsupported")
    stream.seek(0)


def inspect_zip(
    stream: BinaryIO, *, maximum_members: int, maximum_expanded: int, maximum_file: int, budget: SnapshotBudget
) -> tuple[tuple[str, FileExpectation], ...]:
    _zip_preflight(stream, maximum_members=maximum_members)
    paths: set[str] = set()
    directories: set[str] = set()
    result: list[tuple[str, FileExpectation]] = []
    total = 0
    with zipfile.ZipFile(stream) as archive:
        rows = archive.infolist()
        if len(rows) > maximum_members:
            raise ValueError("ZIP member count exceeds bound")
        for row in rows:
            budget.check_deadline()
            name = canonical_path(row.filename[:-1] if row.is_dir() else row.filename)
            if name in paths or name in directories:
                raise ValueError("ZIP duplicate or colliding member")
            mode = row.external_attr >> 16
            expected_kind = stat.S_IFDIR if row.is_dir() else stat.S_IFREG
            if (
                stat.S_IFMT(mode) not in (0, expected_kind)
                or row.flag_bits & 1
                or row.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            ):
                raise ValueError("ZIP member type/encryption/compression is unsupported")
            if row.is_dir():
                if row.file_size:
                    raise ValueError("ZIP directory has data")
                directories.add(name)
                continue
            paths.add(name)
            bounded_int(row.file_size, maximum_file)
            total += row.file_size
            if total > maximum_expanded:
                raise ValueError("ZIP declared expansion exceeds bound")
            checksum = hashlib.sha256()
            expanded = 0
            with archive.open(row) as member:
                while True:
                    budget.check_deadline()
                    chunk = member.read(min(65536, maximum_file - expanded + 1))
                    if not chunk:
                        break
                    expanded += len(chunk)
                    if expanded > row.file_size or expanded > maximum_file:
                        raise ValueError("ZIP actual expansion exceeds bounds")
                    checksum.update(chunk)
            if expanded != row.file_size:
                raise ValueError("ZIP declared/actual size mismatch")
            result.append((name, FileExpectation(expanded, checksum.hexdigest(), 0o400)))
    validate_file_graph(tuple("/input/" + name for name in paths))
    parents = {str(parent) for name in paths for parent in PurePosixPath(name).parents if str(parent) != "."}
    if not directories <= parents or directories & paths or not result:
        raise ValueError("ZIP directory/file graph is invalid")
    return tuple(sorted(result))


def _validate_runtime_zip(
    files: tuple[MountedSnapshot, ...], manifest: RuntimeManifest, budget: SnapshotBudget
) -> None:
    zip_file = next(file for file in files if file.destination == manifest.python_zip)
    with os.fdopen(os.dup(zip_file.snapshot.descriptor), "rb") as stream:
        entries = inspect_zip(
            stream,
            maximum_members=MAX_RUNTIME_ZIP_MEMBERS,
            maximum_expanded=MAX_RUNTIME_ZIP_BYTES,
            maximum_file=MAX_RUNTIME_ZIP_BYTES,
            budget=budget,
        )
    paths = {name for name, _ in entries}
    if not any(name in paths for name in ("encodings/__init__.py", "encodings/__init__.pyc")) or not any(
        name in paths for name in ("encodings/cp437.py", "encodings/cp437.pyc")
    ):
        raise ValueError("runtime ZIP lacks startup encodings package")
    for name in paths:
        if (
            name.endswith((".so", ".pth"))
            or "__pycache__" in PurePosixPath(name).parts
            or PurePosixPath(name).name
            in (
                "sitecustomize.py",
                "usercustomize.py",
                "sitecustomize.pyc",
                "usercustomize.pyc",
            )
        ):
            raise ValueError("runtime ZIP contains native content or site hooks")
    declared = dict(manifest.sourceless_modules)
    actual = {name: expectation for name, expectation in entries if name.endswith(".pyc")}
    if declared != actual or any(name.removesuffix(".pyc") + ".py" in paths for name in actual):
        raise ValueError("runtime sourceless bytecode closure disagrees with manifest")
    with os.fdopen(os.dup(zip_file.snapshot.descriptor), "rb") as stream, zipfile.ZipFile(stream) as archive:
        for name in actual:
            with archive.open(name) as bytecode_stream:
                header = bytecode_stream.read(16)
            if (
                len(header) != 16
                or header[:4] != bytes.fromhex(manifest.bytecode_magic)
                or int.from_bytes(header[4:8], "little") not in (0, 1, 3)
            ):
                raise ValueError("runtime sourceless bytecode header is invalid")
    for package in manifest.packages:
        matching = {
            path
            for path in paths
            if path in (package.name + ".py", package.name + ".pyc") or path.startswith(package.name + "/")
        }
        if matching and (
            package.kind == "regular"
            or package.name + "/__init__.py" in paths
            or package.name + "/__init__.pyc" in paths
            or package.name + ".py" in paths
            or package.name + ".pyc" in paths
        ):
            raise ValueError("runtime ZIP shadows/splits a colocated native package")
    for file in files:
        relative = file.destination.removeprefix(manifest.extension_root + "/")
        if relative != file.destination and file.role == "extension":
            module = relative.split(".", 1)[0]
            if any(
                name in paths
                for name in (module + ".py", module + ".pyc", module + "/__init__.py", module + "/__init__.pyc")
            ):
                raise ValueError("runtime ZIP shadows a native stdlib module")


def _capture(
    selection: SelectionBoundControllerCapture,
    manifest: RuntimeManifest,
    state: ExecutionAuthorityState,
    runtime_source: Path,
    input_archive: Path,
    stack: ExitStack,
    deadline: float,
) -> tuple[MountedSnapshot, ...]:
    policy = state.execution_policy
    budget = SnapshotBudget(
        maximum_file_bytes=max(policy.runtime_file_bytes, policy.archive_bytes, policy.input_file_bytes),
        maximum_total_bytes=policy.snapshot_total_bytes,
        deadline=deadline,
    )
    root = release_filesystem.open_real_directory(runtime_source)
    try:
        before_root = _metadata(os.fstat(root))
        observed, runtime = _scan(root, manifest, budget, stack, capture=True)
        after, _ = _scan(root, manifest, budget, stack, capture=False)
        if observed != after or before_root != _metadata(os.fstat(root)):
            raise ValueError("runtime changed during closed capture")
    finally:
        os.close(root)
    _validate_runtime_zip(runtime, manifest, budget)
    native_graph = dict(validate_elf_graph(manifest, {file.destination: file.snapshot.descriptor for file in runtime}))
    files = [replace(file, elf_metadata=native_graph.get(file.destination)) for file in runtime]
    controller_budget = SnapshotBudget(
        maximum_file_bytes=16 * 1024**2, maximum_total_bytes=16 * 1024**2, deadline=deadline
    )
    for path, data in selection.controller.members:
        expected = FileExpectation(len(data), hashlib.sha256(data).hexdigest(), 0o400)
        snapshot = stack.enter_context(capture_stream(io.BytesIO(data), expected, budget=controller_budget))
        files.append(MountedSnapshot("/controller/" + path, snapshot, "initial", "controller"))
    descriptor = _open_file(input_archive)
    try:
        expected = FileExpectation(state.input_binding.archive_size_bytes, state.input_binding.archive_sha256, 0o400)
        raw = stack.enter_context(capture_regular_file(descriptor, expected, budget=budget))
    finally:
        os.close(descriptor)
    files.append(MountedSnapshot("/retained/raw-input.zip", raw, "host", "raw_archive"))
    with os.fdopen(os.dup(raw.descriptor), "rb") as stream:
        entries = inspect_zip(
            stream,
            maximum_members=policy.input_files,
            maximum_expanded=policy.input_expanded_bytes,
            maximum_file=policy.input_file_bytes,
            budget=budget,
        )
        names = {name for name, _ in entries}
        if not names >= _REQUIRED_INPUT or any(
            name not in _REQUIRED_INPUT and not name.startswith(("evidence/", "public-candidate/bundle/"))
            for name in names
        ):
            raise ValueError("input ZIP lacks initial approval layout or contains unknown paths")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            for name, expected in entries:
                with archive.open(name) as member:
                    snapshot = stack.enter_context(capture_stream(cast(BinaryIO, member), expected, budget=budget))
                files.append(MountedSnapshot("/input/" + name, snapshot, "initial", "input"))
    validate_file_graph(tuple(file.destination for file in files if file.stage == "initial"))
    budget.check_deadline()
    return tuple(sorted(files, key=lambda file: file.destination))


def _elf_observation(value: object, *, native: bool) -> ElfMetadata | None:
    """Decode bounded worker observations as data, never parse native bytes here."""
    if not native:
        if value is not None:
            raise ValueError("capture worker data file has native metadata")
        return None
    row = object_fields(value, {"interpreter", "needed", "soname", "search_paths"})

    def string(item: object) -> str:
        if (
            not isinstance(item, str)
            or not 0 < len(item) <= 4096
            or any(not 32 <= ord(character) <= 126 for character in item)
        ):
            raise ValueError("capture worker native string is invalid")
        return item

    def strings(item: object) -> tuple[str, ...]:
        if not isinstance(item, list) or len(item) > 4096:
            raise ValueError("capture worker native list is invalid")
        return tuple(string(child) for child in item)

    return ElfMetadata(
        None if row["interpreter"] is None else string(row["interpreter"]),
        strings(row["needed"]),
        None if row["soname"] is None else string(row["soname"]),
        strings(row["search_paths"]),
    )


def _send(channel: socket.socket, file: MountedSnapshot | None) -> None:
    if file is None:
        payload = b'{"kind":"done"}'
        ancillary: list[tuple[int, int, bytes]] = []
    else:
        expected = file.snapshot.expectation
        payload = json.dumps(
            {
                "kind": "file",
                "destination": file.destination,
                "stage": file.stage,
                "role": file.role,
                "size_bytes": expected.size_bytes,
                "sha256": expected.sha256,
                "mode": expected.mode,
                "elf": None
                if file.elf_metadata is None
                else {
                    "interpreter": file.elf_metadata.interpreter,
                    "needed": file.elf_metadata.needed,
                    "soname": file.elf_metadata.soname,
                    "search_paths": file.elf_metadata.search_paths,
                },
            },
            separators=(",", ":"),
        ).encode("ascii")
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [file.snapshot.descriptor]).tobytes())]
    if len(payload) > _PACKET_BYTES or channel.sendmsg([payload], ancillary) != len(payload):
        raise ValueError("capture worker descriptor protocol send failed")


def _worker(
    channel: socket.socket,
    selection: SelectionBoundControllerCapture,
    manifest: RuntimeManifest,
    state: ExecutionAuthorityState,
    runtime_source: Path,
    input_archive: Path,
    deadline: float,
) -> None:
    try:
        # Fork uses independently deployed host code; close inherited unrelated
        # descriptors and cap address space without increasing any existing limit.
        with os.scandir("/proc/self/fd") as entries:
            inherited = [int(entry.name) for entry in entries if entry.name.isdigit()]
        for descriptor in inherited:
            if descriptor > 2 and descriptor != channel.fileno():
                with suppress(OSError):
                    os.close(descriptor)
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = state.execution_policy.worker_address_space_bytes
        if soft != resource.RLIM_INFINITY:
            ceiling = min(ceiling, soft)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
        with ExitStack() as stack:
            files = _capture(selection, manifest, state, runtime_source, input_archive, stack, deadline)
            for file in files:
                _send(channel, file)
            _send(channel, None)
    except BaseException:  # noqa: BLE001 - process boundary rejects every failed capture
        with suppress(OSError):
            channel.send(b'{"kind":"error"}')
        os._exit(1)
    os._exit(0)


def _reap(pid: int, *, kill: bool) -> int:
    if kill:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while time.monotonic() < deadline:
        observed, status = os.waitpid(pid, os.WNOHANG)
        if observed == pid:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.005)
    raise RuntimeError("capture worker death could not be verified; no accepted execution")


def _receive(channel: socket.socket, deadline: float) -> tuple[dict[str, object], int | None]:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not select.select([channel], [], [], remaining)[0]:
        raise ValueError("capture worker hard deadline exceeded")
    data, control, flags, _ = channel.recvmsg(_PACKET_BYTES, socket.CMSG_SPACE(4 * 4), socket.MSG_CMSG_CLOEXEC)
    descriptors: list[int] = []
    try:
        for level, kind, raw in control:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                raise ValueError("capture worker ancillary protocol is invalid")
            values = array.array("i")
            values.frombytes(raw[: len(raw) - len(raw) % values.itemsize])
            descriptors.extend(values)
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or not data or len(descriptors) > 1:
            raise ValueError("capture worker packet is truncated or invalid")
        value = load_json_bytes(data)
        if not isinstance(value, dict) or any(type(key) is not str for key in value):
            raise ValueError("capture worker metadata is invalid")
        return cast(dict[str, object], value), descriptors[0] if descriptors else None
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


@contextmanager
def capture_in_worker(
    selection: SelectionBoundControllerCapture,
    manifest: RuntimeManifest,
    state: ExecutionAuthorityState,
    runtime_source: Path,
    input_archive: Path,
    *,
    deadline: float,
) -> Iterator[tuple[MountedSnapshot, ...]]:
    """Kill/reap a blocked host capture, retaining no partial successful result."""
    if threading.active_count() != 1:
        raise ValueError("trusted fork capture requires a single-threaded host")
    with ExitStack() as stack:
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        stack.callback(parent.close)
        stack.callback(child.close)
        pid = os.fork()
        if pid == 0:
            parent.close()
            _worker(child, selection, manifest, state, runtime_source, input_archive, deadline)
        child.close()
        reaped = False
        files: list[MountedSnapshot] = []
        try:
            while True:
                packet, descriptor = _receive(parent, deadline)
                if descriptor is not None:
                    stack.callback(os.close, descriptor)
                if packet == {"kind": "done"} and descriptor is None:
                    break
                if packet == {"kind": "error"} and descriptor is None:
                    raise ValueError("trusted capture worker rejected execution input")
                row = object_fields(
                    packet, {"kind", "destination", "stage", "role", "size_bytes", "sha256", "mode", "elf"}
                )
                if (
                    row["kind"] != "file"
                    or descriptor is None
                    or len(files) >= MAX_HELD_FILES
                    or row["stage"] not in ("initial", "host")
                    or not isinstance(row["role"], str)
                ):
                    raise ValueError("capture worker file protocol is invalid")
                destination = canonical_path(row["destination"], absolute=True)
                mode = row["mode"]
                if type(mode) is not int or mode not in (0o400, 0o500):
                    raise ValueError("capture worker mode is invalid")
                expected = FileExpectation(
                    bounded_int(
                        row["size_bytes"],
                        max(
                            state.execution_policy.archive_bytes,
                            state.execution_policy.runtime_file_bytes,
                            state.execution_policy.input_file_bytes,
                            16 * 1024**2,
                        ),
                    ),
                    digest(row["sha256"]),
                    cast(Literal[0o400, 0o500], mode),
                )
                metadata = os.fstat(descriptor)
                identity = SnapshotIdentity(metadata.st_dev, metadata.st_ino)
                validate_snapshot_descriptor(descriptor, expected, deadline=deadline, identity=identity)
                files.append(
                    MountedSnapshot(
                        destination,
                        SealedFileSnapshot(descriptor, expected, identity),
                        row["stage"],
                        row["role"],
                        _elf_observation(row["elf"], native=row["role"] in NATIVE_ROLES),
                    )
                )
            status = _reap(pid, kill=False)
            reaped = True
            if status != 0:
                raise ValueError("capture worker did not exit successfully")
            expected_runtime = {file.destination: (file.expectation, file.stage, file.role) for file in manifest.files}
            expected_controller = {
                "/controller/" + path: (
                    FileExpectation(len(data), hashlib.sha256(data).hexdigest(), 0o400),
                    "initial",
                    "controller",
                )
                for path, data in selection.controller.members
            }
            binding = state.input_binding
            expected_static = {
                **expected_runtime,
                **expected_controller,
                "/retained/raw-input.zip": (
                    FileExpectation(binding.archive_size_bytes, binding.archive_sha256, 0o400),
                    "host",
                    "raw_archive",
                ),
            }
            actual = {file.destination: (file.snapshot.expectation, file.stage, file.role) for file in files}
            if (
                len(actual) != len(files)
                or any(actual.get(path) != value for path, value in expected_static.items())
                or any(
                    path not in expected_static
                    and (
                        not path.startswith("/input/")
                        or stage != "initial"
                        or role != "input"
                        or expectation.mode != 0o400
                    )
                    for path, (expectation, stage, role) in actual.items()
                )
            ):
                raise ValueError("capture worker returned mismatched file closure")
            if (
                len(actual) - len(expected_static) > state.execution_policy.input_files
                or sum(file.snapshot.expectation.size_bytes for file in files if file.role == "input")
                > state.execution_policy.input_expanded_bytes
            ):
                raise ValueError("capture worker returned excessive input closure")
            input_names = {file.destination.removeprefix("/input/") for file in files if file.role == "input"}
            if not input_names >= _REQUIRED_INPUT or any(
                name not in _REQUIRED_INPUT and not name.startswith(("evidence/", "public-candidate/bundle/"))
                for name in input_names
            ):
                raise ValueError("capture worker returned invalid initial input layout")
            if (
                sum(file.snapshot.expectation.size_bytes for file in files if file.role != "controller")
                > state.execution_policy.snapshot_total_bytes
            ):
                raise ValueError("capture worker aggregate snapshot limit exceeded")
            yield tuple(files)
        finally:
            if not reaped:
                _reap(pid, kill=True)


def validate_held_in_worker(execution: HeldControllerExecution) -> None:
    """Bound even a stalled retained ZIP/native inspection with kill/reap."""
    from scripts.release_controller_execution_input import validate_held_execution_closure

    if threading.active_count() != 1:
        raise ValueError("trusted validation fork requires a single-threaded host")
    with execution.descriptor_capacity.infrastructure(2), ExitStack() as stack:
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        stack.callback(parent.close)
        stack.callback(child.close)
        pid = os.fork()
        if pid == 0:
            parent.close()
            try:
                soft, hard = resource.getrlimit(resource.RLIMIT_AS)
                ceiling = execution.authority_state.execution_policy.worker_address_space_bytes
                if soft != resource.RLIM_INFINITY:
                    ceiling = min(ceiling, soft)
                resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
                validate_held_execution_closure(execution)
                _send(child, None)
            except BaseException:  # noqa: BLE001 - failed host validation releases no execution gate
                with suppress(OSError):
                    child.send(b'{"kind":"error"}')
                os._exit(1)
            os._exit(0)
        child.close()
        reaped = False
        try:
            packet, descriptor = _receive(parent, execution.capture_deadline)
            if descriptor is not None:
                os.close(descriptor)
            if packet != {"kind": "done"} or descriptor is not None:
                raise ValueError("held execution validation worker rejected authenticated closure")
            status = _reap(pid, kill=False)
            reaped = True
            if status != 0:
                raise ValueError("held execution validation worker failed")
        finally:
            if not reaped:
                _reap(pid, kill=True)
