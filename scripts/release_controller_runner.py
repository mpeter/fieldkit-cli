"""Preparatory initial-export C0 execution; callers authenticate capture and runtime.

Load this module from trusted bootstrap tooling, never from the selected input.
Containment results are observations, not authentication or release approval.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import math
import os
import platform
import select
import selectors
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts import process_supervision
from scripts.release_controller_closure import validate_controller_capture
from scripts.release_controller_execution_input import (
    HeldControllerExecution,
    TrustedExecutionAuthority,
    revalidate_execution_authority,
)
from scripts.release_controller_runtime_manifest import validate_file_graph
from scripts.release_controller_snapshot import (
    FileExpectation,
    SealedFileSnapshot,
    SnapshotBudget,
    capture_stream,
    validate_snapshot_descriptor,
)

CONTROLLER_TIMEOUT_SECONDS = 60.0
CONTROLLER_OUTPUT_LIMIT_BYTES = 64 * 1024
_CLEANUP_TIMEOUT_SECONDS = 2.0
_BWRAP = Path("/usr/bin/bwrap")
_MAX_ARGUMENT_BYTES = 64 * 1024
_MAX_SERIALIZED_ARGUMENT_BYTES = 1024 * 1024
# The independently deployed bubblewrap parser caps combined option/command tokens.
_MAX_BWRAP_ARGUMENTS = 9000
_MAX_RUNTIME_MOUNTS = 256
# Args/filter readers each reserve their two-descriptor creation peak; pipes
# reserve four slots. Launch peaks at DEVNULL + four output-pipe ends + two
# Popen error-pipe ends. Later output readers/pidfd/selector use at most four.
_SNAPSHOT_INFRASTRUCTURE_DESCRIPTORS = 2
_PIPE_INFRASTRUCTURE_DESCRIPTORS = 4
_LAUNCH_INFRASTRUCTURE_DESCRIPTORS = 7
_NAMESPACE_INFORMATION_LIMIT_BYTES = 4096
_NAMESPACE_STARTUP_TIMEOUT_SECONDS = 3.0


def _network_filter() -> bytes:
    """Fixed native x86-64 filter; reject alternate ABIs before syscall numbers.

    Kernel seccomp_data.nr/arch offsets and the native syscall table are defined
    by Linux UAPI. No input, runtime or controller bytes select this policy.
    https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html
    https://github.com/torvalds/linux/blob/master/arch/x86/entry/syscalls/syscall_64.tbl
    """
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or sys.byteorder != "little"
        or struct.calcsize("P") != 8
    ):
        raise OSError("controller seccomp requires the native Linux x86-64 ABI")
    # Classic BPF: LD W ABS, JEQ K, RET K, JGE K. Check AUDIT_ARCH_X86_64
    # first, then reject the x32 bit/high syscall numbers; compat i386 has a
    # different audit architecture and cannot bypass the native-number policy.
    instructions = [
        (0x20, 0, 0, 4),
        (0x15, 1, 0, 0xC000003E),
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
        (0x35, 0, 1, 0x40000000),
        (0x06, 0, 0, 0x80000000),
    ]
    # socket through getsockopt; accept4, recvmmsg, sendmmsg; io_uring can
    # submit network operations without a socket syscall visible to seccomp.
    # ptrace/process_vm and pidfd_getfd must not transfer/rewrite capabilities.
    denied = (*range(41, 56), 101, 288, 299, 307, 310, 311, 425, 426, 427, 438)
    for number in denied:
        instructions.extend(((0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)))
    instructions.append((0x06, 0, 0, 0x7FFF0000))
    return b"".join(struct.pack("<HBBI", *instruction) for instruction in instructions)


@dataclass(frozen=True)
class ControllerRun:
    """Bounded preparatory observation; no approval authority is implied."""

    returncode: int
    stdout: bytes
    stderr: bytes
    failure: Literal["timeout", "output-limit"] | None
    authority: Literal["preparatory-unattested"] = "preparatory-unattested"


_LAUNCHER = """
import sys
sys.path[:] = sys.argv[1].split(':') + ['/controller']
import importlib.machinery
class ClosedImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'fieldkit' or fullname.startswith('fieldkit.'):
            raise ImportError('fieldkit imports are forbidden in initial-export C0')
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None:
            locations = list(spec.submodule_search_locations or ())
            if spec.origin not in (None, 'built-in', 'frozen'):
                locations.append(spec.origin)
            for location in locations:
                if not (location.startswith('/controller/') or location.startswith('/runtime/')):
                    raise ImportError('import outside controller/runtime closure')
        return spec
sys.meta_path.insert(0, ClosedImports())
import resource
resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
import runpy
entrypoint = sys.argv[2]
sys.argv[:] = [entrypoint, *sys.argv[3:]]
runpy.run_path(entrypoint, run_name='__main__')
"""


def _validate_reader(snapshot: SealedFileSnapshot, *, deadline: float) -> None:
    validate_snapshot_descriptor(
        snapshot.descriptor, snapshot.expectation, identity=snapshot.identity, deadline=deadline
    )


def _held_reader(
    snapshot: SealedFileSnapshot, execution: HeldControllerExecution, stack: ExitStack
) -> SealedFileSnapshot:
    # Reopen only a retained anonymous file, giving bwrap an independent offset.
    # dup2 alone would share the borrowed owner's open-file-description offset.
    with execution.descriptor_capacity.allocate(
        1, lambda: (os.open(f"/proc/self/fd/{snapshot.descriptor}", os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK),)
    ) as independent:
        descriptor = stack.enter_context(execution.descriptor_capacity.duplicate(independent[0]))
    reader = SealedFileSnapshot(descriptor, snapshot.expectation, snapshot.identity)
    _validate_reader(reader, deadline=execution.capture_deadline)
    if os.lseek(descriptor, 0, os.SEEK_CUR) != 0:
        raise ValueError("controller snapshot reader must start at offset zero")
    return reader


def _sealed_data(data: bytes, execution: HeldControllerExecution, stack: ExitStack) -> SealedFileSnapshot:
    stack.enter_context(execution.descriptor_capacity.infrastructure(_SNAPSHOT_INFRASTRUCTURE_DESCRIPTORS))
    expected = FileExpectation(len(data), hashlib.sha256(data).hexdigest(), 0o400)
    budget = SnapshotBudget(len(data), len(data), execution.capture_deadline)
    return stack.enter_context(capture_stream(io.BytesIO(data), expected, budget=budget))


def _initial_mounts(execution: HeldControllerExecution) -> tuple[tuple[str, ...], tuple[str, ...]]:
    files = execution.initial_files
    destinations = tuple(file.destination for file in files)
    parents = validate_file_graph(destinations)
    if parents != execution.parent_directories:
        raise ValueError("controller retained mount graph mismatch")
    expected = {
        file.destination: file.expectation for file in execution.runtime_manifest.files if file.stage == "initial"
    }
    if len(expected) > _MAX_RUNTIME_MOUNTS:
        raise ValueError("runtime mount count exceeds bounds")
    members = validate_controller_capture(execution.selection.controller)
    expected.update(
        {
            f"/controller/{path}": FileExpectation(len(data), hashlib.sha256(data).hexdigest(), 0o400)
            for path, data in members
        }
    )
    observed = {file.destination: file.snapshot.expectation for file in files if file.role != "input"}
    if observed != expected or any(
        file.role == "input" and not file.destination.startswith("/input/") for file in files
    ):
        raise ValueError("controller initial file grant disagrees with retained closure")
    if "/input" not in parents or "/controller" not in parents:
        raise ValueError("controller initial input or controller closure is missing")
    imports = execution.runtime_manifest.import_paths
    if any(path not in destinations and path not in parents for path in imports):
        raise ValueError("runtime import root is absent from closed graph")
    return parents, imports


def _pin_namespace(information: bytes, stack: ExitStack) -> int | None:
    """Pin the trusted bwrap-reported namespace init, never a recycled host PID."""
    value = json.loads(information)
    if not isinstance(value, dict):
        raise process_supervision.ProcessError("controller namespace information is invalid")
    pid, namespace = value.get("child-pid"), value.get("pid-namespace")
    if type(pid) is not int or pid <= 0 or type(namespace) is not int or namespace <= 0:
        raise process_supervision.ProcessError("controller namespace identity is missing")
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return None
    stack.callback(os.close, descriptor)
    if select.select([descriptor], [], [], 0)[0]:
        return descriptor
    try:
        observed = Path(f"/proc/{pid}/ns/pid").stat().st_ino
    except FileNotFoundError:
        if select.select([descriptor], [], [], 0)[0]:
            return descriptor
        raise process_supervision.ProcessError("controller namespace identity disappeared") from None
    if observed != namespace:
        raise process_supervision.ProcessError("controller namespace PID was recycled")
    return descriptor


def _finish_namespace(descriptor: int | None, *, terminate: bool) -> None:
    """Namespace-init death acknowledges kernel teardown of detached descendants."""
    if descriptor is None or select.select([descriptor], [], [], 0)[0]:
        return
    if terminate:
        with suppress(ProcessLookupError):
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    if not select.select([descriptor], [], [], _CLEANUP_TIMEOUT_SECONDS)[0]:
        raise process_supervision.ProcessError("controller namespace did not terminate")


def _namespace_information(descriptor: int) -> bytes:
    """Read only trusted bwrap metadata before opening the controller gate."""
    deadline = time.monotonic() + _NAMESPACE_STARTUP_TIMEOUT_SECONDS
    information = bytearray()
    os.set_blocking(descriptor, False)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise process_supervision.ProcessError("controller namespace startup timed out")
        data = os.read(descriptor, _NAMESPACE_INFORMATION_LIMIT_BYTES + 1 - len(information))
        if not data:
            if not information:
                raise process_supervision.ProcessError("controller namespace information is missing")
            return bytes(information)
        information.extend(data)
        if len(information) > _NAMESPACE_INFORMATION_LIMIT_BYTES:
            raise process_supervision.ProcessError("controller namespace information exceeds limit")


def _observe(
    execution: HeldControllerExecution,
    build_command: Callable[[int, int, ExitStack], tuple[tuple[str, ...], list[int]]],
    validate_gate: Callable[[float], None],
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
) -> ControllerRun:
    with ExitStack() as stack:
        stack.enter_context(execution.descriptor_capacity.infrastructure(_PIPE_INFRASTRUCTURE_DESCRIPTORS))
        with ExitStack() as launch_descriptors:
            information_reader, information_writer = os.pipe2(os.O_CLOEXEC)
            stack.callback(os.close, information_reader)
            launch_descriptors.callback(os.close, information_writer)
            gate_reader, gate_writer = os.pipe2(os.O_CLOEXEC)
            launch_descriptors.callback(os.close, gate_reader)
            stack.callback(os.close, gate_writer)
            command, descriptors = build_command(information_writer, gate_reader, stack)
            stack.enter_context(execution.descriptor_capacity.infrastructure(_LAUNCH_INFRASTRUCTURE_DESCRIPTORS))
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"LC_ALL": "C.UTF-8"},
                cwd="/",
                pass_fds=(*descriptors, information_writer, gate_reader),
                start_new_session=True,
            )
        namespace_descriptor: int | None = None
        output = [bytearray(), bytearray()]
        failure: Literal["timeout", "output-limit"] | None = None
        deadline = time.monotonic() + timeout_seconds
        try:
            namespace_descriptor = _pin_namespace(_namespace_information(information_reader), stack)
            if namespace_descriptor is None or select.select([namespace_descriptor], [], [], 0)[0]:
                raise process_supervision.ProcessError("controller namespace exited before startup gate")
            if time.monotonic() >= deadline:
                failure = "timeout"
            else:
                validate_gate(min(deadline, execution.capture_deadline))
                if time.monotonic() >= min(deadline, execution.capture_deadline):
                    failure = "timeout"
            if failure is None and os.write(gate_writer, b"1") != 1:
                raise process_supervision.ProcessError("controller startup gate release failed")
            with selectors.DefaultSelector() as selector:
                assert process.stdout is not None and process.stderr is not None
                for index, pipe in enumerate((process.stdout.fileno(), process.stderr.fileno())):
                    os.set_blocking(pipe, False)
                    selector.register(pipe, selectors.EVENT_READ, index)
                while failure is None and selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        failure = "timeout"
                        break
                    for key, _ in selector.select(timeout=remaining):
                        data = os.read(key.fd, 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        buffer = output[key.data]
                        room = output_limit_bytes - len(buffer)
                        buffer.extend(data[:room])
                        if len(data) > room:
                            failure = "output-limit"
                            break
                    if failure is not None:
                        break
            if failure is None:
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    failure = "timeout"
            if failure is not None:
                _finish_namespace(namespace_descriptor, terminate=True)
                errors = process_supervision.terminate_process_group(
                    process, kill_after_seconds=_CLEANUP_TIMEOUT_SECONDS
                )
                if errors:
                    raise process_supervision.ProcessError("controller cleanup was interrupted")
            else:
                _finish_namespace(namespace_descriptor, terminate=False)
        except BaseException:
            try:
                _finish_namespace(namespace_descriptor, terminate=True)
            finally:
                if process.returncode is None:
                    process_supervision.terminate_process_group(process, kill_after_seconds=_CLEANUP_TIMEOUT_SECONDS)
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        return ControllerRun(
            process.returncode if process.returncode is not None else -1, bytes(output[0]), bytes(output[1]), failure
        )


def run_initial_export_controller(
    execution: HeldControllerExecution,
    *,
    authority: TrustedExecutionAuthority,
    temporary_parent: Path,
    arguments: tuple[str, ...] = (),
    timeout_seconds: float = CONTROLLER_TIMEOUT_SECONDS,
    output_limit_bytes: int = CONTROLLER_OUTPUT_LIMIT_BYTES,
) -> ControllerRun:
    """Run authenticated held initial files; refresh independent authority at the gate.

    The owning capture context must outlive this call. No source paths are opened,
    host-stage tools/raw archive are granted, or deployment authority is inferred
    from a dataclass. Bubblewrap options use a mandatory sealed descriptor; its
    outer command is a retained canonical tuple checked again at the gate, because
    bubblewrap --args does not supply its executable/positional arguments.
    Observations remain preparatory and unattested.
    """
    if not isinstance(execution, HeldControllerExecution) or not isinstance(authority, TrustedExecutionAuthority):
        raise ValueError("held execution and independent authority provider are required")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= CONTROLLER_TIMEOUT_SECONDS:
        raise ValueError("controller timeout must be positive and bounded")
    if type(output_limit_bytes) is not int or not 0 < output_limit_bytes <= CONTROLLER_OUTPUT_LIMIT_BYTES:
        raise ValueError("controller output limit must be positive and bounded")
    if (
        any("\0" in argument for argument in arguments)
        or sum(len(argument.encode("utf-8")) for argument in arguments) > _MAX_ARGUMENT_BYTES
    ):
        raise ValueError("controller arguments exceed bounds or contain NUL")
    if not _BWRAP.is_file() or not os.access(_BWRAP, os.X_OK):
        raise OSError("controller containment requires bubblewrap")
    parents, imports = _initial_mounts(execution)
    with ExitStack() as stack:
        readers = tuple(
            (file.destination, _held_reader(file.snapshot, execution, stack)) for file in execution.initial_files
        )
        network_filter = _sealed_data(_network_filter(), execution, stack)
        arguments_snapshot: SealedFileSnapshot | None = None
        command: tuple[str, ...] = ()
        expected_command: tuple[str, ...] = ()

        def build_command(
            information_writer: int, gate_reader: int, launch_stack: ExitStack
        ) -> tuple[tuple[str, ...], list[int]]:
            nonlocal arguments_snapshot, command, expected_command
            options = [
                "--info-fd",
                str(information_writer),
                "--block-fd",
                str(gate_reader),
                "--unshare-all",
                "--as-pid-1",
                "--unshare-user",
                "--disable-userns",
                "--die-with-parent",
                "--new-session",
                "--cap-drop",
                "ALL",
                "--clearenv",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--seccomp",
                str(network_filter.descriptor),
            ]
            for parent in parents:
                options.extend(("--dir", parent))
            for destination, reader in readers:
                options.extend(
                    ("--perms", f"{reader.expectation.mode:o}", "--ro-bind-data", str(reader.descriptor), destination)
                )
            options.extend(
                (
                    "--tmpfs",
                    "/scratch",
                    "--setenv",
                    "HOME",
                    "/scratch",
                    "--setenv",
                    "TMPDIR",
                    "/scratch",
                    "--setenv",
                    "LC_ALL",
                    "C.UTF-8",
                    "--chdir",
                    "/scratch",
                    "--remount-ro",
                    "/",
                )
            )
            data = b"\0".join(value.encode("utf-8") for value in options) + b"\0"
            if len(data) > _MAX_SERIALIZED_ARGUMENT_BYTES:
                raise ValueError("controller serialized arguments exceed bounds")
            arguments_snapshot = _sealed_data(data, execution, launch_stack)
            expected_command = (
                str(_BWRAP),
                "--args",
                str(arguments_snapshot.descriptor),
                "--",
                execution.runtime_manifest.interpreter,
                "-I",
                "-S",
                "-c",
                _LAUNCHER,
                ":".join(imports),
                f"/controller/{execution.selection.controller.entrypoint}",
                "/input",
                *arguments,
            )
            command = expected_command
            encoded_command = tuple(value.encode("utf-8") + b"\0" for value in expected_command)
            # Linux MAX_ARG_STRLEN is 32 native pages; SC_ARG_MAX also includes
            # argv/env pointers and their terminating NULL entries.
            native_argument_limit = 32 * os.sysconf("SC_PAGESIZE")
            sandbox_environment = (b"LC_ALL=C.UTF-8\0", b"HOME=/scratch\0", b"TMPDIR=/scratch\0", b"PWD=/scratch\0")
            pointer_bytes = (len(command) + len(sandbox_environment) + 2) * struct.calcsize("P")
            if any(len(value) > native_argument_limit for value in encoded_command) or sum(
                map(len, encoded_command)
            ) + len(b"LC_ALL=C.UTF-8\0") + pointer_bytes >= os.sysconf("SC_ARG_MAX"):
                raise ValueError("controller spawn argv exceeds native host capacity")
            return command, [
                arguments_snapshot.descriptor,
                network_filter.descriptor,
                *(reader.descriptor for _, reader in readers),
            ]

        def validate_gate(deadline: float) -> None:
            if not expected_command or tuple(command) != expected_command:
                raise ValueError("controller canonical spawn command changed before startup gate")
            for _, reader in readers:
                _validate_reader(reader, deadline=deadline)
            _validate_reader(network_filter, deadline=deadline)
            if arguments_snapshot is None:
                raise ValueError("controller sealed arguments are missing")
            _validate_reader(arguments_snapshot, deadline=deadline)
            revalidate_execution_authority(execution, authority=authority)

        return _observe(
            execution,
            build_command,
            validate_gate,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )
