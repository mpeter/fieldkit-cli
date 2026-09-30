"""Real namespace and import-containment contracts for preparatory C0 runs."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import io
import json
import os
import select
import signal
import stat
import subprocess
import sys
import sysconfig
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import release_controller_bootstrap as bootstrap
from scripts import release_controller_capture_worker as worker
from scripts import release_controller_closure as closure
from scripts import release_controller_execution_input as execution
from scripts import release_controller_runner as runner
from scripts import release_controller_runtime_manifest as manifest
from scripts.release_controller_elf import inspect_elf
from scripts.release_controller_snapshot import SealedFileSnapshot
from tests.test_release_controller_execution_input import SyntheticAuthority
from tests.test_release_controller_runtime_manifest import runtime_fixture
from tests.test_release_trust_contracts import fixture_selection, mapping

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class RuntimeFixture:
    source: Path
    manifest_bytes: bytes


def _encoded(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> RuntimeFixture:
    """Real closed ELF/stdlib test inventory; synthetic pins grant no deployment."""
    executable = Path(sys.executable).resolve()
    stdlib = Path(sysconfig.get_path("stdlib"))
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    zip_target = f"{sys.platlibdir}/python{sys.version_info.major}{sys.version_info.minor}.zip"
    extension_target = f"{sys.platlibdir}/python{version}/lib-dynload"
    raw, _ = runtime_fixture()
    python = mapping(raw["python"])
    platform_version = ".".join(map(str, sys.version_info[:3]))
    python.update(
        version=platform_version,
        stdlib_directory=f"/runtime/{sys.platlibdir}",
        zip=f"/runtime/{zip_target}",
        extension_root=f"/runtime/{extension_target}",
        bytecode_magic=importlib.util.MAGIC_NUMBER.hex(),
    )
    archive_data = io.BytesIO()
    sourceless: list[dict[str, object]] = []
    with zipfile.ZipFile(archive_data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stdlib.rglob("*")):
            if path.suffix not in {".py", ".pyc"} or (path.suffix == ".pyc" and path.with_suffix(".py").exists()):
                continue
            relative = path.relative_to(stdlib)
            if any(
                part in {"site-packages", "__pycache__", "test", "tests", "idlelib", "tkinter", "ensurepip"}
                for part in relative.parts
            ):
                continue
            data = path.read_bytes()
            archive.writestr(relative.as_posix(), data)
            if path.suffix == ".pyc":
                sourceless.append({"path": relative.as_posix(), "size_bytes": len(data), "sha256": _digest(data)})
    python["sourceless_modules"] = sourceless
    sources: dict[str, tuple[bytes, str, str, str]] = {
        "bin/python": (executable.read_bytes(), "/runtime/bin/python", "interpreter", "initial"),
        zip_target: (archive_data.getvalue(), f"/runtime/{zip_target}", "python_zip", "initial"),
        "lock/uv.lock": (b"synthetic fixture lock\n", "/runtime/lock/uv.lock", "lock", "host"),
    }
    native: list[tuple[Path, str, str]] = [(executable, "/runtime/bin/python", "interpreter")]
    native.extend(
        (path, f"/runtime/{extension_target}/{path.name}", "extension")
        for path in sorted((stdlib / "lib-dynload").glob("*.so"))
    )
    seen: set[str] = set()
    search = (executable.parent.parent / sys.platlibdir, Path("/lib64"), Path("/usr/lib64"))
    while native:
        path, destination, role = native.pop()
        if destination in seen:
            continue
        seen.add(destination)
        source_name = (
            destination.removeprefix("/runtime/") if destination.startswith("/runtime/") else "native/" + path.name
        )
        sources[source_name] = (path.read_bytes(), destination, role, "initial")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            metadata = inspect_elf(descriptor)
        except ValueError as error:
            raise ValueError(f"test runtime ELF rejected: {path.name}: {error}") from error
        finally:
            os.close(descriptor)
        if metadata.interpreter:
            native.append((Path(metadata.interpreter), metadata.interpreter, "loader"))
        for name in metadata.needed:
            resolved = next((directory / name for directory in search if (directory / name).is_file()), None)
            if resolved is None:
                pytest.fail(f"explicit test ELF dependency unavailable: {name}")
            target = f"/lib64/{name}"
            native.append((resolved, target, "loader" if name.startswith("ld-linux-") else "library"))
    for name in ("git", "ssh-keygen", "uv"):
        sources[f"bin/{name}"] = (executable.read_bytes(), f"/runtime/bin/{name}", "tool", "host")
    files = [
        {
            "source": name,
            "destination": target,
            "size_bytes": len(data),
            "sha256": _digest(data),
            "mode": 0o500 if role in {"interpreter", "loader", "library", "extension", "tool"} else 0o400,
            "role": role,
            "stage": stage,
        }
        for name, (data, target, role, stage) in sorted(sources.items())
    ]
    raw["files"] = files
    raw["library_directories"] = ["/lib64", f"/runtime/{sys.platlibdir}"]
    tools = raw["tools"]
    assert isinstance(tools, list)
    for tool in tools:
        assert isinstance(tool, dict)
        if tool["name"] == "python":
            tool["version"] = platform_version
    source = tmp_path_factory.mktemp("closed-runner-runtime")
    for name, (data, _, _, _) in sources.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest_bytes = _encoded(raw)
    parsed = manifest.parse_runtime_manifest(manifest_bytes)
    assert len(parsed.files) <= 256
    return RuntimeFixture(source, manifest_bytes)


@contextmanager
def _held(
    tmp_path: Path, runtime: RuntimeFixture, capture: closure.ControllerCapture
) -> Iterator[tuple[execution.HeldControllerExecution, SyntheticAuthority]]:
    """Inject synthetic external authority; retain the real supervised capture boundary."""
    now = datetime.now(tz=UTC)
    selected = fixture_selection()
    schemas = Path(__file__).parents[1] / "docs/release-readiness"
    deployment = bootstrap.BootstrapDeploymentPolicy(
        (schemas / "release-trust-selection.schema.json").read_bytes(),
        (schemas / "release-controller-receipt.schema.json").read_bytes(),
        frozenset({7}),
    )
    mapping(selected["policy"]).update(
        selection_schema_sha256=_digest(deployment.selection_schema_bytes),
        receipt_schema_sha256=_digest(deployment.receipt_schema_bytes),
    )
    members = [{"path": path, "size_bytes": len(data), "sha256": _digest(data)} for path, data in capture.members]
    c0 = _encoded(
        {
            "schema_version": 1,
            "kind": "fieldkit.release-controller-manifest",
            "entrypoint": capture.entrypoint,
            "members": members,
        }
    )
    mapping(selected["controller"]).update(
        entrypoint=capture.entrypoint,
        manifest_sha256=_digest(c0),
        members=[{**member, "role": "fixture"} for member in members],
    )
    parsed = manifest.parse_runtime_manifest(runtime.manifest_bytes)
    by_destination = {file.destination: file for file in parsed.files}
    toolchain = mapping(selected["toolchain"])
    for tool in parsed.tools:
        toolchain[tool.name] = {
            "version": tool.version,
            "executable_sha256": by_destination[tool.destination].expectation.sha256,
        }
    toolchain.update(
        runtime_manifest_sha256=_digest(runtime.manifest_bytes),
        uv_lock_sha256=by_destination[parsed.lock].expectation.sha256,
    )
    selection_bytes = _encoded(selected)
    anchor = bootstrap.ExternalSelectionAnchor(
        _digest(selection_bytes), "synthetic-runner-pin", now - timedelta(hours=1), now + timedelta(hours=1), 7
    )
    retained = bootstrap.SelectionBoundControllerCapture(
        selection_bytes, _digest(selection_bytes), c0, _digest(c0), anchor.approval_reference, 7, capture
    )
    selected_root = tmp_path / "selected"
    selected_root.mkdir(exist_ok=True)
    (selected_root / "data.txt").write_text("selected", encoding="utf-8")
    archive = tmp_path / "selected.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in sorted(worker._REQUIRED_INPUT):
            output.writestr(name, b"{}\n")
        for path in sorted(selected_root.rglob("*")):
            if path.is_file():
                output.writestr("evidence/" + path.relative_to(selected_root).as_posix(), path.read_bytes())
    producer = mapping(selected["producer"])
    identities: list[tuple[str, str | int]] = []
    for key, value in sorted(producer.items()):
        assert isinstance(value, (str, int))
        identities.append((key, value))
    binding = execution.ExternalInputBinding(
        retained.selection_sha256,
        _digest(archive.read_bytes()),
        archive.stat().st_size,
        tuple(identities),
        anchor.approval_reference,
        7,
        "synthetic-runner-observation",
        now - timedelta(minutes=5),
        now + timedelta(minutes=5),
    )
    policy = execution.ExecutionPolicy(
        "synthetic-runner-policy",
        7,
        120,
        128 * 1024**2,
        256 * 1024**2,
        256,
        1280 * 1024**2,
        512 * 1024**2,
        512 * 1024**2,
        128 * 1024**2,
        256,
        4 * 1024**3,
        16,
    )
    authority = SyntheticAuthority(execution.ExecutionAuthorityState(anchor, deployment, policy, binding, now))
    with execution.authenticate_and_hold_execution_input(
        retained, runtime.manifest_bytes, authority=authority, runtime_source=runtime.source, input_archive=archive
    ) as held:
        yield held, authority


def _capture(tmp_path: Path, source: str, extra: dict[str, str] | None = None) -> closure.ControllerCapture:
    root = tmp_path / "c0"
    root.mkdir()
    sources = {"scripts/__init__.py": "", "scripts/main.py": source, **(extra or {})}
    members = []
    for path, contents in sources.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = contents.encode("utf-8")
        destination.write_bytes(data)
        members.append(closure.ControllerMember(path, len(data), hashlib.sha256(data).hexdigest()))
    return closure.capture_controller_closure(root, expected_entrypoint="scripts/main.py", expected_members=members)


def _run(
    tmp_path: Path,
    runtime: RuntimeFixture,
    capture: closure.ControllerCapture,
    *,
    timeout: float = 5,
    limit: int = 4096,
    arguments: tuple[str, ...] = (),
) -> runner.ControllerRun:
    with _held(tmp_path, runtime, capture) as (held, authority):
        return runner.run_initial_export_controller(
            held,
            authority=authority,
            temporary_parent=tmp_path,
            timeout_seconds=timeout,
            output_limit_bytes=limit,
            arguments=arguments,
        )


def _observe_namespace_processes(marker: str, stopped: int, identities: dict[int, str]) -> None:
    """Retain host PID/start-time identities for this run's namespace members."""
    needle = marker.encode() + b"\0"
    while not select.select([stopped], [], [], 0)[0]:
        for directory in Path("/proc").glob("[0-9]*"):
            try:
                if needle not in (directory / "cmdline").read_bytes():
                    continue
                status = (directory / "status").read_text(encoding="utf-8")
                nspid = next(line for line in status.splitlines() if line.startswith("NSpid:"))
                if len(nspid.split()) < 3:
                    continue
                fields = (directory / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
                identities[int(directory.name)] = fields[19]
            except (OSError, StopIteration):
                continue
        select.select([stopped], [], [], 0.01)


def _run_observing_descendants(
    tmp_path: Path,
    runtime: RuntimeFixture,
    capture: closure.ControllerCapture,
    *,
    timeout: float,
    limit: int = 4096,
) -> runner.ControllerRun:
    marker = f"c0-cleanup-{uuid.uuid4().hex}"
    identities: dict[int, str] = {}
    with _held(tmp_path, runtime, capture) as (held, authority), ExitStack() as stack:
        stop_reader, stop_writer = os.pipe2(os.O_CLOEXEC)
        report_reader, report_writer = os.pipe2(os.O_CLOEXEC)
        for descriptor in (stop_reader, stop_writer, report_reader, report_writer):
            stack.callback(os.close, descriptor)
        observer = os.fork()
        if observer == 0:
            _observe_namespace_processes(marker, stop_reader, identities)
            data = _encoded(identities)
            if len(data) > 4096 or os.write(report_writer, data) != len(data):
                os._exit(1)
            os._exit(0)
        observer_pidfd = os.pidfd_open(observer)
        stack.callback(os.close, observer_pidfd)
        try:
            result = runner.run_initial_export_controller(
                held,
                authority=authority,
                temporary_parent=tmp_path,
                timeout_seconds=timeout,
                output_limit_bytes=limit,
                arguments=(marker,),
            )
        finally:
            os.write(stop_writer, b"stop")
            if not select.select([observer_pidfd], [], [], 2)[0]:
                signal.pidfd_send_signal(observer_pidfd, signal.SIGKILL)
                os.waitpid(observer, 0)
                pytest.fail("namespace observer did not terminate")
            _, status = os.waitpid(observer, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        data = os.read(report_reader, 4097)
        assert len(data) <= 4096
        observed = json.loads(data)
        assert isinstance(observed, dict)
        for pid, start_time in observed.items():
            assert isinstance(pid, str) and isinstance(start_time, str)
            identities[int(pid)] = start_time
    assert len(identities) >= 2, "host must observe both controller and its forked child"
    for pid, start_time in identities.items():
        try:
            fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        assert fields[19] != start_time or fields[0] == "Z", f"descendant {pid} survived containment cleanup"
    return result


def test_only_captured_controller_runtime_and_read_only_input_visible(tmp_path: Path, runtime: RuntimeFixture) -> None:
    host = tmp_path / "host-private"
    host.write_text("private", encoding="utf-8")
    source = f"""
import os, pathlib, socket, sys
import helper
assert helper.VALUE == 7
assert sys.flags.isolated and sys.flags.no_site
assert all(p.startswith(('/runtime/', '/controller')) for p in sys.path)
assert pathlib.Path('/input/evidence/data.txt').read_text() == 'selected'
assert not pathlib.Path({str(host)!r}).exists()
assert not pathlib.Path('/home').exists()
assert not pathlib.Path('/usr').exists()
assert 'PYTHONPATH' not in os.environ
for destination in ['/input/evidence/data.txt', '/input/new', '/controller/scripts/main.py', '/controller/new', '/runtime/new', '/new']:
    try:
        pathlib.Path(destination).write_text('forbidden')
    except OSError:
        pass
    else:
        raise AssertionError(destination)
pathlib.Path('/scratch/result').write_text('private scratch')
try:
    socket.create_connection(('192.0.2.1', 80), timeout=0.1)
except OSError:
    pass
else:
    raise AssertionError('network escaped')
sys.stdout.write('contained')
"""
    capture = _capture(tmp_path, source, {"helper.py": "VALUE = 7\n"})
    result = _run(tmp_path, runtime, capture)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"contained"
    assert result.failure is None
    assert result.authority == "preparatory-unattested"
    assert host.read_text(encoding="utf-8") == "private"
    assert (tmp_path / "selected/data.txt").read_text(encoding="utf-8") == "selected"


def test_hostile_environment_input_and_candidate_imports_never_run(
    tmp_path: Path, runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "sentinel"
    hostile = tmp_path / "candidate"
    hostile.mkdir()
    payload = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('escaped')\n"
    for name in ("sitecustomize.py", "helper.py", "runpy.py"):
        (hostile / name).write_text(payload, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    monkeypatch.setenv("PYTHONHOME", str(hostile))
    monkeypatch.setenv("LD_PRELOAD", str(hostile / "bad.so"))
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "evil.py").write_text(payload, encoding="utf-8")
    capture = _capture(
        tmp_path,
        """
import sys
sys.path.insert(0, '/input/evidence')
try:
    import evil
except ImportError:
    pass
else:
    raise AssertionError('input import executed')
sys.path.insert(0, '/scratch')
from pathlib import Path
Path('/scratch/evil2.py').write_text('raise AssertionError("scratch import executed")')
try:
    import evil2
except ImportError:
    pass
else:
    raise AssertionError('scratch import executed')
try:
    __import__("fieldkit")
except ImportError:
    pass
else:
    raise AssertionError('fieldkit import permitted')
try:
    import helper
except ImportError:
    pass
else:
    raise AssertionError('candidate import executed')
sys.stdout.write('imports-contained')
""",
    )
    (tmp_path / "c0/scripts/main.py").write_text(payload, encoding="utf-8")
    result = _run(tmp_path, runtime, capture)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"imports-contained"
    assert not sentinel.exists()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_overflow_stops_and_retains_only_bounded_bytes(
    tmp_path: Path, runtime: RuntimeFixture, stream: str
) -> None:
    capture = _capture(
        tmp_path, f"import sys\nwhile True:\n    sys.{stream}.write('x'*8192)\n    sys.{stream}.flush()\n"
    )
    result = _run(tmp_path, runtime, capture, limit=100)
    assert result.failure == "output-limit"
    assert len(result.stdout) <= 100 and len(result.stderr) <= 100


@pytest.mark.parametrize("detach", [False, True])
def test_timeout_kills_even_detached_descendants(tmp_path: Path, runtime: RuntimeFixture, detach: bool) -> None:
    capture = _capture(
        tmp_path,
        f"""
import os, time
pid = os.fork()
if pid == 0:
    if {detach!r}:
        os.setsid()
    while True:
        time.sleep(0.05)
else:
    while True:
        time.sleep(0.05)
""",
    )
    result = _run_observing_descendants(tmp_path, runtime, capture, timeout=0.5)
    assert result.failure == "timeout"
    assert result.returncode != 0


def test_successful_parent_cannot_leave_detached_child_alive(tmp_path: Path, runtime: RuntimeFixture) -> None:
    capture = _capture(
        tmp_path,
        """
import os, time, sys
if os.fork() == 0:
    os.setsid()
    os.close(1)
    os.close(2)
    while True:
        time.sleep(0.05)
time.sleep(0.2)
sys.stdout.write('parent-exited')
""",
    )
    result = _run_observing_descendants(tmp_path, runtime, capture, timeout=5)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"parent-exited"
    assert result.failure is None


def test_host_stage_tools_lock_and_raw_archive_never_mounted(tmp_path: Path, runtime: RuntimeFixture) -> None:
    capture = _capture(
        tmp_path,
        "from pathlib import Path\nimport sys\nassert not Path('/runtime/bin/git').exists()\nassert not Path('/runtime/lock').exists()\nassert not Path('/retained').exists()\nsys.stdout.write('initial-only')\n",
    )
    result = _run(tmp_path, runtime, capture)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"initial-only"


def test_missing_containment_never_falls_back_to_host_execution(
    tmp_path: Path, runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _capture(tmp_path, "raise AssertionError('must not run')")
    monkeypatch.setattr(runner, "_BWRAP", tmp_path / "missing-bwrap")
    with pytest.raises(OSError, match="requires bubblewrap"):
        _run(tmp_path, runtime, capture)


def test_controller_failure_remains_non_passing_observation(tmp_path: Path, runtime: RuntimeFixture) -> None:
    capture = _capture(tmp_path, "raise SystemExit(3)")
    result = _run(tmp_path, runtime, capture)
    assert result.returncode == 3
    assert result.failure is None
    assert result.authority == "preparatory-unattested"


def test_native_network_and_io_uring_syscalls_denied(tmp_path: Path, runtime: RuntimeFixture) -> None:
    capture = _capture(
        tmp_path,
        """
import ctypes, errno, socket, sys
libc = ctypes.CDLL(None, use_errno=True)
for number in (41, 53, 425, 426, 427):
    ctypes.set_errno(0)
    assert libc.syscall(number, 0, 0, 0, 0, 0, 0) == -1
    assert ctypes.get_errno() == errno.EPERM, number
for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
    try:
        socket.socket(family)
    except PermissionError:
        pass
    else:
        raise AssertionError('network family permitted')
sys.stdout.write('seccomp-denied')
""",
    )
    result = _run(tmp_path, runtime, capture)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"seccomp-denied"


@pytest.mark.parametrize("failure", ["output-limit", "timeout"])
def test_early_failure_acknowledges_detached_child_death(
    tmp_path: Path, runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    capture = _capture(
        tmp_path,
        """
import os, sys, time
if os.fork() == 0:
    os.setsid()
    while True:
        time.sleep(0.01)
while True:
    sys.stdout.write('x' * 8192)
    sys.stdout.flush()
""",
    )
    write = os.write
    released = threading.Event()

    def delayed_release(descriptor: int, data: bytes) -> int:
        result = write(descriptor, data)
        if data == b"1":
            released.set()
            # Let C0 fork and fill its pipe before the first output observation.
            time.sleep(0.75)
        return result

    monkeypatch.setattr(os, "write", delayed_release)
    result = _run_observing_descendants(
        tmp_path, runtime, capture, timeout=0.5 if failure == "timeout" else 5, limit=100
    )
    assert result.failure == failure
    assert released.is_set()
    assert len(result.stdout) <= 100 and len(result.stderr) <= 100


def test_deadline_before_gate_release_never_executes_controller(
    tmp_path: Path, runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _capture(tmp_path, "raise AssertionError('controller must stay gated')")
    pin = runner._pin_namespace
    pinned: list[int] = []

    def delayed_pin(information: bytes, stack: ExitStack) -> int | None:
        descriptor = pin(information, stack)
        assert descriptor is not None
        pinned.append(descriptor)
        time.sleep(0.2)
        return descriptor

    monkeypatch.setattr(runner, "_pin_namespace", delayed_pin)
    result = _run(tmp_path, runtime, capture, timeout=0.1)
    assert result.failure == "timeout"
    assert pinned
    assert result.stdout == b"" and result.stderr == b""


@pytest.mark.unit
def test_second_pipe_allocation_failure_closes_both_first_pipe_descriptors(
    tmp_path: Path, runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _capture(tmp_path, "raise AssertionError('must stay gated')")
    with _held(tmp_path, runtime, capture) as (held, authority):
        pipe2 = os.pipe2
        allocated: list[int] = []

        def fail_second_pipe(flags: int) -> tuple[int, int]:
            if allocated:
                raise OSError(errno.EMFILE, "injected second pipe allocation failure")
            descriptors = pipe2(flags)
            allocated.extend(descriptors)
            return descriptors

        monkeypatch.setattr(os, "pipe2", fail_second_pipe)
        with pytest.raises(OSError, match="injected second pipe allocation failure") as error:
            runner.run_initial_export_controller(held, authority=authority, temporary_parent=tmp_path)
        assert error.value.errno == errno.EMFILE
        assert len(allocated) == 2
        # Reservation restoration may deliberately reuse closed descriptor numbers.
        for descriptor in allocated:
            assert not stat.S_ISFIFO(os.fstat(descriptor).st_mode)


@pytest.mark.parametrize(
    "change",
    ["expiry", "revocation", "generation", "schema", "policy", "input-expiry", "argument-reuse", "controller-reuse"],
)
def test_authority_or_descriptor_change_keeps_gate_closed_and_namespace_dies(
    tmp_path: Path, runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    capture = _capture(tmp_path, "raise AssertionError('controller must stay gated')")
    with _held(tmp_path, runtime, capture) as (held, authority):
        pin = runner._pin_namespace
        sealed = runner._sealed_data
        snapshots: list[SealedFileSnapshot] = []
        namespace_observers: list[int] = []
        released: list[bool] = []
        write = os.write

        def track_sealed(
            data: bytes, execution_input: execution.HeldControllerExecution, stack: ExitStack
        ) -> SealedFileSnapshot:
            result = sealed(data, execution_input, stack)
            snapshots.append(result)
            return result

        def record_write(descriptor: int, data: bytes) -> int:
            if data == b"1":
                released.append(True)
            return write(descriptor, data)

        def change_after_pin(information: bytes, stack: ExitStack) -> int | None:
            descriptor = pin(information, stack)
            assert descriptor is not None
            namespace_observers.append(os.dup(descriptor))
            state = authority.state
            if change == "expiry":
                authority.state = replace(state, trusted_utc=state.anchor.valid_before)
            elif change == "revocation":
                authority.state = replace(state, anchor=replace(state.anchor, approval_reference="revoked-pin"))
            elif change == "generation":
                authority.state = replace(state, anchor=replace(state.anchor, generation=8))
            elif change == "schema":
                authority.state = replace(state, deployment=replace(state.deployment, selection_schema_bytes=b"{}"))
            elif change == "policy":
                authority.state = replace(
                    state, execution_policy=replace(state.execution_policy, reference="changed-policy")
                )
            elif change == "input-expiry":
                authority.state = replace(state, trusted_utc=state.input_binding.valid_before)
            elif change == "argument-reuse":
                assert len(snapshots) == 2
                os.dup2(held.initial_files[0].snapshot.descriptor, snapshots[-1].descriptor, inheritable=False)
            elif change == "controller-reuse":
                c0 = next(file for file in held.initial_files if file.role == "controller")
                replacement = next(file for file in held.initial_files if file.role == "input")
                os.dup2(replacement.snapshot.descriptor, c0.snapshot.descriptor, inheritable=False)
            return descriptor

        monkeypatch.setattr(runner, "_sealed_data", track_sealed)
        monkeypatch.setattr(runner, "_pin_namespace", change_after_pin)
        monkeypatch.setattr(os, "write", record_write)
        try:
            with pytest.raises(
                ValueError, match=r"selection|authority|generation|schema|binding|snapshot|approval|closure|validity"
            ) as error:
                runner.run_initial_export_controller(held, authority=authority, temporary_parent=tmp_path)
            assert error.value.args
            assert not released
            assert namespace_observers
            assert all(select.select([descriptor], [], [], 0)[0] for descriptor in namespace_observers)
        finally:
            for descriptor in namespace_observers:
                os.close(descriptor)


def test_held_offsets_unchanged_and_repeated_execution_uses_independent_readers(
    tmp_path: Path, runtime: RuntimeFixture
) -> None:
    capture = _capture(
        tmp_path,
        "from pathlib import Path\nimport sys\nassert Path('/input/evidence/data.txt').read_text() == 'selected'\nsys.stdout.write('repeatable')\n",
    )
    with _held(tmp_path, runtime, capture) as (held, authority):
        initial = held.initial_files
        for file in initial:
            os.lseek(file.snapshot.descriptor, min(3, file.snapshot.expectation.size_bytes), os.SEEK_SET)
        offsets = tuple(os.lseek(file.snapshot.descriptor, 0, os.SEEK_CUR) for file in initial)
        first = runner.run_initial_export_controller(held, authority=authority, temporary_parent=tmp_path)
        assert first.returncode == 0, first.stderr.decode()
        assert first.stdout == b"repeatable"
        second = runner.run_initial_export_controller(held, authority=authority, temporary_parent=tmp_path)
        assert second.returncode == 0, second.stderr.decode()
        assert second.stdout == first.stdout
        assert tuple(os.lseek(file.snapshot.descriptor, 0, os.SEEK_CUR) for file in initial) == offsets


@pytest.mark.parametrize("failure", ["popen", "arguments", "capacity"])
def test_prelaunch_failures_restore_capacity_without_descriptor_leaks(
    tmp_path: Path, runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    capture = _capture(tmp_path, "raise AssertionError('must never run')")
    with _held(tmp_path, runtime, capture) as (held, authority):
        before = len(tuple(Path("/proc/self/fd").iterdir()))
        if failure == "popen":

            def fail_popen(*args: object, **kwargs: object) -> None:
                raise OSError(errno.EMFILE, "injected spawn failure")

            monkeypatch.setattr(subprocess, "Popen", fail_popen)
            expected_error: type[Exception] = OSError
            match = "injected spawn failure"
        elif failure == "arguments":
            monkeypatch.setattr(runner, "_MAX_SERIALIZED_ARGUMENT_BYTES", 1)
            expected_error = ValueError
            match = "serialized arguments"
        else:

            def fail_duplicate(source: int) -> object:
                raise ValueError("injected descriptor reservation exhaustion")

            monkeypatch.setattr(held.descriptor_capacity, "duplicate", fail_duplicate)
            expected_error = ValueError
            match = "reservation exhaustion"
        with pytest.raises(expected_error, match=match) as error:
            runner.run_initial_export_controller(held, authority=authority, temporary_parent=tmp_path)
        assert error.value.args
        assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.parametrize("change", ["immutable-command", "capture-deadline"])
def test_final_command_and_capture_deadline_checked_before_gate_release(
    tmp_path: Path, runtime: RuntimeFixture, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    capture = _capture(tmp_path, "raise SystemExit(3)")
    with _held(tmp_path, runtime, capture) as (held, authority):
        observe = runner._observe
        pin = runner._pin_namespace
        commands: list[tuple[str, ...]] = []
        write = os.write
        released: list[bool] = []

        def track_write(descriptor: int, data: bytes) -> int:
            if data == b"1":
                released.append(True)
            return write(descriptor, data)

        def captured_observe(
            execution_input: execution.HeldControllerExecution,
            build: Callable[[int, int, ExitStack], tuple[tuple[str, ...], list[int]]],
            validate: Callable[[float], None],
            *,
            timeout_seconds: float,
            output_limit_bytes: int,
        ) -> runner.ControllerRun:
            def captured_build(writer: int, reader: int, stack: ExitStack) -> tuple[tuple[str, ...], list[int]]:
                result = build(writer, reader, stack)
                commands.append(result[0])
                return result

            def final_validation(deadline: float) -> None:
                validate(deadline)
                if change == "capture-deadline":
                    object.__setattr__(execution_input, "capture_deadline", time.monotonic() - 1)

            return observe(
                execution_input,
                captured_build,
                final_validation,
                timeout_seconds=timeout_seconds,
                output_limit_bytes=output_limit_bytes,
            )

        def mutate_command(information: bytes, stack: ExitStack) -> int | None:
            descriptor = pin(information, stack)
            if change == "immutable-command":
                assert commands
                assert type(commands[-1]) is tuple
            return descriptor

        monkeypatch.setattr(runner, "_observe", captured_observe)
        monkeypatch.setattr(runner, "_pin_namespace", mutate_command)
        monkeypatch.setattr(os, "write", track_write)
        result = runner.run_initial_export_controller(held, authority=authority, temporary_parent=tmp_path)
        if change == "immutable-command":
            assert result.returncode == 3, result.stderr.decode()
            assert released
        else:
            assert result.failure == "timeout"
            assert result.stdout == b"" and result.stderr == b""
            assert not released
        assert commands
        assert commands[0][:2] == ("/usr/bin/bwrap", "--args")
        assert commands[0][3:8] == ("--", held.runtime_manifest.interpreter, "-I", "-S", "-c")
