"""Run documentation owner commands inside a bounded OS sandbox."""

import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

if TYPE_CHECKING or __package__:
    from scripts.documentation_ca import system_ca_mounts
    from scripts.documentation_commands import DOCUMENT_COMMANDS, EXAMPLE_COMMANDS
    from scripts.documentation_runtime import DependencyInput, RuntimeTools, directory_options
else:  # pragma: no cover - direct script execution
    from documentation_ca import system_ca_mounts
    from documentation_commands import DOCUMENT_COMMANDS, EXAMPLE_COMMANDS
    from documentation_runtime import DependencyInput, RuntimeTools, directory_options

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_LIMIT = 4000
_TIMEOUT_SECONDS = 120
_CLEANUP_TIMEOUT_SECONDS = 3
_DESCENDANT_GRACE_SECONDS = 3
_READER_JOIN_SECONDS = 3
_BWRAP = Path("/usr/bin/bwrap")
_STRACE = Path("/usr/bin/strace")
_SANDBOX_REPOSITORY = Path("/workspace")
_SANDBOX_UV = Path("/run/fieldkit-bin/uv")
_SANDBOX_DEPENDENCIES = Path("/run/fieldkit-doc-dependencies")
_INHERITED_MARKER = Path("/run/fieldkit-skill-semantic-owner")
_INHERITED_MODE = "skill_semantic_contract"
_SEMANTIC_OWNER_ARGV = DOCUMENT_COMMANDS[_INHERITED_MODE][0]
_NETWORK_ATTEMPT_MESSAGE = "documentation verifier blocked outbound network; network attempt rejected"
_ARTIFACT_SCENARIO_TIMEOUT_SECONDS = 1_800
_ARTIFACT_DIAGNOSTIC_OUTPUT_LIMIT = 64 * 1024
_ARTIFACT_DIAGNOSTIC_ARGV = (*EXAMPLE_COMMANDS["automated.installed-base-artifact"][0], "--diagnostic-dirty")
_COMMAND_TIMEOUTS = {
    EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]: _ARTIFACT_SCENARIO_TIMEOUT_SECONDS,
    _ARTIFACT_DIAGNOSTIC_ARGV: _ARTIFACT_SCENARIO_TIMEOUT_SECONDS,
}

# The controller supplies this code in memory. Isolated startup excludes the
# candidate's sitecustomize, PYTHONPATH and current directory from its imports.
_SUBREAPER_SUPERVISOR = """\
import ctypes
import os
import signal
import sys
import time

libc = ctypes.CDLL(None, use_errno=True)
# Same-UID owners must not rewrite the supervisor through procfs or ptrace.
if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
    os.write(2, b"documentation supervisor dumpability setup failed\\n")
    sys.exit(1)
enabled = ctypes.c_int()
if (libc.prctl(36, 1, 0, 0, 0) != 0
        or libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) != 0
        or enabled.value != 1):
    os.write(2, b"documentation subreaper setup failed\\n")
    sys.exit(1)

owner = os.fork()
if owner == 0:
    try:
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    except OSError:
        os.write(2, b"documentation owner execution failed\\n")
        os._exit(1)

cancelled = False
def cancel(signum, frame):
    global cancelled
    cancelled = True
signal.signal(signal.SIGTERM, cancel)

def kill_children():
    # The subreaper is the sole waiter: unreaped child PIDs cannot be reused.
    with open('/proc/self/task/%s/children' % os.getpid()) as children:
        for child_pid in children.read().split():
            try:
                os.kill(int(child_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

# Only this process waits for the owner and adopted descendants. The outer
# controller retains its original stage deadline while this owner runs.
while True:
    if cancelled:
        kill_children()
    waited, status = os.waitpid(owner, os.WNOHANG)
    if waited:
        break
    time.sleep(0.01)
owner_status = os.waitstatus_to_exitcode(status)
deadline = time.monotonic() + float(sys.argv[1])
cleaning = False
while True:
    # Check even after reaping a zombie: fork/reap churn cannot renew the grace.
    if not cleaning and (cancelled or time.monotonic() >= deadline):
        os.write(2, b"documentation sandbox terminated an unfinished descendant\\n")
        cleaning = True
        owner_status = 124
        deadline = time.monotonic() + 1
    if cleaning:
        # Killing parents adopts detached grandchildren on the next pass.
        kill_children()
        if time.monotonic() >= deadline:
            sys.exit(124)
    try:
        child, _ = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        sys.exit(owner_status if owner_status >= 0 else 128 - owner_status)
    if child == 0:
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
"""


@dataclass(frozen=True)
class CommandResult:
    """Bounded result for one fixed verification command."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


class _BoundedBytes:
    """Thread-safe tail buffer that never retains more than the evidence limit."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._value = bytearray()
        self._lock = threading.Lock()

    def append(self, value: bytes) -> None:
        with self._lock:
            self._value.extend(value)
            if len(self._value) > self._limit:
                del self._value[: len(self._value) - self._limit]

    def text(self) -> str:
        with self._lock:
            return bytes(self._value).decode("utf-8", errors="replace")


_NETWORK_GUARD = """\
import os
import sys

# Network enforcement is outside this interpreter so native extensions and
# ``python -S`` cannot bypass it.  In particular, do not rely on replacing
# socket.socket.connect here.

_audit_active = False
def _audit(event, args):
    global _audit_active
    if event != "open" or _audit_active or not args or not isinstance(args[0], (str, bytes)):
        return
    coverage = os.environ.get("FIELDKIT_DOC_COVERAGE")
    root = os.environ.get("FIELDKIT_REPO_ROOT")
    if not coverage or not root:
        return
    candidate = os.path.realpath(os.fsdecode(args[0]))
    root = os.path.realpath(root)
    if candidate != root and not candidate.startswith(root + os.sep):
        return
    _audit_active = True
    try:
        descriptor = os.open(coverage, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, (os.path.relpath(candidate, root) + "\\n").encode())
        finally:
            os.close(descriptor)
    finally:
        _audit_active = False
sys.addaudithook(_audit)
"""


def _isolated_environment(
    temporary_root: Path,
    *,
    repo_root: Path | None = None,
    coverage_path: Path | None = None,
    source_revision: str | None = None,
) -> dict[str, str]:
    """Build an allowlisted environment for the OS-contained child."""
    guard_root = temporary_root / "python-guard"
    guard_root.mkdir(parents=True)
    (guard_root / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")
    resolver_root = temporary_root / "resolver"
    resolver_root.mkdir()
    (resolver_root / "resolv.conf").write_text(
        "nameserver 198.51.100.1\noptions timeout:1 attempts:1\n", encoding="utf-8"
    )
    (resolver_root / "hosts").write_text("127.0.0.1 localhost\n::1 localhost ip6-localhost\n", encoding="utf-8")
    (resolver_root / "nsswitch.conf").write_text("hosts: files dns\n", encoding="utf-8")
    for directory in ("home", "xdg-cache", "xdg-config", "xdg-data", "xdg-state"):
        (temporary_root / directory).mkdir()
    environment = {
        key: value for key, value in os.environ.items() if key in {"LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"}
    }
    environment.update(
        {
            "HOME": str(temporary_root / "home"),
            "TMPDIR": str(temporary_root),
            "UV_NO_CACHE": "1",
            "UV_NO_SYNC": "1",
            "UV_OFFLINE": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
            "VIRTUAL_ENV": str(_SANDBOX_REPOSITORY / ".venv"),
            "PIP_NO_INDEX": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": f"{_SANDBOX_REPOSITORY / '.venv/bin'}:{_SANDBOX_UV.parent}:/usr/bin:/bin",
            "PYTHONPATH": str(guard_root),
            "PYTEST_XDIST_WORKERS": "4",
            "PYTEST_ADDOPTS": shlex.join(("-o", f"cache_dir={temporary_root / 'pytest-cache'}")),
            "XDG_CACHE_HOME": str(temporary_root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(temporary_root / "xdg-config"),
            "XDG_DATA_HOME": str(temporary_root / "xdg-data"),
            "XDG_STATE_HOME": str(temporary_root / "xdg-state"),
        }
    )
    if repo_root is not None and coverage_path is not None:
        environment["FIELDKIT_REPO_ROOT"] = str(_SANDBOX_REPOSITORY)
        environment["FIELDKIT_DOC_COVERAGE"] = str(coverage_path)
    if source_revision is not None:
        if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
            raise ValueError("documentation source revision must be a full lowercase Git SHA")
        environment["FIELDKIT_DOC_SOURCE_REVISION"] = source_revision
    return environment


def _drain(stream: BinaryIO, output: _BoundedBytes, errors: list[str]) -> None:
    """Drain one binary pipe while retaining only its bounded tail."""
    try:
        while chunk := stream.read(65_536):
            output.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    except OSError:
        errors.append("output drain failed")


class _NetworkTrace:
    """Reduce an untrusted syscall stream to one bounded policy decision."""

    _ipv4 = re.compile(r'inet_addr\("(?P<address>[0-9.]+)"\)')
    _ipv6 = re.compile(r'inet_pton\(AF_INET6, "(?P<address>[0-9a-fA-F:]+)"')
    _quoted = re.compile(r'"(?:[^"\\]|\\.)*"')

    def __init__(self, owner_executable: str | None = None) -> None:
        self.attempted = False
        self.interrupted_descendant = False
        self._owner_executable = owner_executable
        self._owner_pid: str | None = None
        self._owner_exited = False
        self._first_exec_seen = False
        self._launcher_pid: str | None = None
        self._launcher_exited = False
        self._namespace_init_pid: str | None = None
        self._namespace_clone_seen = False
        self._partial = bytearray()
        self._lock = threading.Lock()

    @staticmethod
    def _is_loopback_v4(address: str) -> bool:
        first = address.partition(".")[0]
        return first == "127"

    @classmethod
    def _line_is_forbidden(cls, line: str) -> bool:
        structure = cls._quoted.sub('""', line)
        if any(syscall in structure for syscall in ("io_uring_setup(", "io_uring_enter(", "io_uring_register(")):
            # io_uring can submit socket operations without another traced
            # network syscall. This verifier does not support that execution
            # path, so even an unsuccessful setup attempt fails closed.
            return True
        if "CLONE_UNTRACED" in structure and ("clone(" in structure or "clone3(" in structure):
            # A child created with CLONE_UNTRACED is deliberately excluded
            # from ptrace event reporting and could hide a denied send.
            return True
        if not any(syscall in structure for syscall in ("connect(", "sendto(", "sendmsg(", "sendmmsg(")):
            return False
        if "AF_NETLINK" in structure:
            return False
        if "AF_UNIX" in structure:
            return True
        if "AF_INET6" in structure:
            addresses = [match.group("address") for match in cls._ipv6.finditer(line)]
            return not addresses or any(address != "::1" for address in addresses)
        if "AF_INET" in structure:
            addresses = [match.group("address") for match in cls._ipv4.finditer(line)]
            return not addresses or any(not cls._is_loopback_v4(address) for address in addresses)
        # A connected socket supplies NULL here; its earlier connect carried
        # the address. Unknown address shapes fail closed.
        return "NULL" not in structure

    def consume(self, chunk: bytes) -> None:
        """Inspect every complete line without retaining syscall arguments."""
        with self._lock:
            self._partial.extend(chunk)
            while b"\n" in self._partial:
                raw_line, _, remainder = self._partial.partition(b"\n")
                self._partial = bytearray(remainder)
                line = raw_line.decode("utf-8", errors="replace")
                if len(raw_line) > 8192 or self._line_is_forbidden(line):
                    self.attempted = True
                prefix = re.match(r"^\s*(\d+)\s+", line)
                if prefix is not None:
                    pid = prefix.group(1)
                    structure = re.sub(r"/\*.*?\*/", "", self._quoted.sub('""', line))
                    if not self._first_exec_seen and "execve(" in structure and re.search(r"\)\s+=\s+0$", structure):
                        self._first_exec_seen = True
                        if f"execve({json.dumps(str(_BWRAP))}," in line:
                            self._launcher_pid = pid
                    if pid == self._launcher_pid and not self._launcher_exited and not self._namespace_clone_seen:
                        clone = re.search(r"\b(?:clone|clone3)\((.*)\)\s+=\s+([1-9][0-9]*)$", structure)
                        if clone is not None:
                            self._namespace_clone_seen = True
                            flags = re.search(r"(?:^|[,{}])\s*flags=([A-Z0-9_|]+)(?:[,}]|$)", clone.group(1))
                            if flags is not None and "CLONE_NEWPID" in flags.group(1).split("|"):
                                self._namespace_init_pid = clone.group(2)
                    terminal = "+++ exited with " in structure or "+++ killed by " in structure
                    if pid == self._launcher_pid and (terminal or "exit_group(" in structure):
                        self._launcher_exited = True
                    if (
                        self._owner_executable is not None
                        and f"execve({json.dumps(self._owner_executable)}," in line
                        and self._owner_pid is None
                    ):
                        self._owner_pid = pid
                    if pid == self._owner_pid and "exit_group(" in self._quoted.sub('""', line):
                        self._owner_exited = True
                    helper_cleanup = pid == self._namespace_init_pid and "+++ killed by SIGKILL +++" in structure
                    if pid == self._namespace_init_pid and (terminal or "execve(" in structure):
                        # An exit_group entry can precede bwrap's teardown kill.
                        # Only a terminal record allows this PID to be reused.
                        self._namespace_init_pid = None
                    if self._owner_exited and "+++ killed by SIGKILL +++" in structure and not helper_cleanup:
                        self.interrupted_descendant = True
            if len(self._partial) > 8192:
                self.attempted = True
                self._partial.clear()

    def finish(self) -> None:
        """Inspect the final unterminated record and discard it."""
        with self._lock:
            if self._partial and self._line_is_forbidden(self._partial.decode("utf-8", errors="replace")):
                self.attempted = True
            self._partial.clear()


def _drain_trace(stream: BinaryIO, trace: _NetworkTrace, errors: list[str]) -> None:
    """Drain syscall evidence while retaining only a policy bit."""
    try:
        while chunk := stream.read(65_536):
            trace.consume(chunk if isinstance(chunk, bytes) else chunk.encode())
    except OSError:
        errors.append("network trace drain failed")
    finally:
        trace.finish()


@dataclass(frozen=True)
class _TraceTransport:
    argv: tuple[str, ...]
    reader: BinaryIO
    writer: BinaryIO
    owner_executable: str


@contextmanager
def _trace_transport(
    temporary_root: Path, sandbox: tuple[str, ...], owner_executable: str
) -> Iterator[_TraceTransport]:
    """Keep strace and its channel outside the tracee's mount/PID namespaces."""
    with tempfile.TemporaryDirectory(prefix="documentation-trace-", dir=temporary_root.parent) as directory:
        fifo = Path(directory) / "trace.fifo"
        os.mkfifo(fifo, mode=0o600)
        read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            write_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(write_fd, "wb", buffering=0) as writer:
                os.set_blocking(read_fd, True)
                command = (
                    str(_STRACE),
                    "-f",
                    "-q",
                    "-e",
                    "trace=connect,sendto,sendmsg,sendmmsg,clone,clone3,execve,exit_group,io_uring_setup,io_uring_enter,io_uring_register",
                    "-s",
                    "128",
                    "-o",
                    str(fifo),
                    *sandbox,
                )
                yield _TraceTransport(command, reader, writer, owner_executable)


def _sandbox_argv(repo_root: Path, argv: tuple[str, ...]) -> tuple[str, ...]:
    """Translate only paths whose exact sandbox mount is known."""
    executable = argv[0]
    if executable == "uv":
        return (str(_SANDBOX_UV), *argv[1:])
    executable_path = Path(executable)
    if executable_path.is_absolute() and executable_path.is_relative_to(repo_root):
        return (str(_SANDBOX_REPOSITORY / executable_path.relative_to(repo_root)), *argv[1:])
    return argv


def _owner_argv(repo_root: Path, argv: tuple[str, ...], dependency_input: DependencyInput | None) -> tuple[str, ...]:
    command = _sandbox_argv(repo_root, argv)
    if dependency_input is not None and argv in (_ARTIFACT_DIAGNOSTIC_ARGV, _ARTIFACT_DIAGNOSTIC_ARGV[:-1]):
        return (
            *command,
            "--dependency-root",
            str(_SANDBOX_DEPENDENCIES),
            "--dependency-manifest-sha256",
            dependency_input.manifest_sha256,
        )
    return command


def _open_mount_source(
    path: Path,
    *,
    trusted_root: Path | None = None,
    escape_error: str,
    require_real_directory: bool = False,
    readable: bool = False,
) -> int:
    """Open a verified bind source so later path replacement cannot redirect it."""
    if require_real_directory:
        try:
            path_status = path.lstat()
        except OSError as error:
            raise ValueError(escape_error) from error
        if not stat.S_ISDIR(path_status.st_mode):
            raise ValueError(escape_error)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(escape_error) from error
    root = trusted_root.resolve(strict=True) if trusted_root is not None else None
    if root is not None and not resolved.is_relative_to(root):
        raise ValueError(escape_error)
    try:
        access_mode = os.O_RDONLY if readable else os.O_PATH
        descriptor = os.open(resolved, access_mode | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise ValueError(escape_error) from error
    try:
        opened = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
        opened_status = os.fstat(descriptor)
        if opened != resolved or (root is not None and not opened.is_relative_to(root)):
            raise ValueError(escape_error)
        if require_real_directory and not stat.S_ISDIR(opened_status.st_mode):
            raise ValueError(escape_error)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _sandbox_command(
    repo_root: Path,
    runtime_root: Path,
    temporary_root: Path,
    argv: tuple[str, ...],
    *,
    runtime_tools: RuntimeTools | None = None,
    dependency_input: DependencyInput | None = None,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Build a minimal read-only mount and namespace boundary for one scenario."""
    if not all(path.is_file() and os.access(path, os.X_OK) for path in (_BWRAP, _STRACE)):
        raise OSError("documentation verifier containment tools are unavailable")
    if repo_root.is_symlink() or runtime_root.is_symlink():
        raise ValueError("candidate and runtime roots must be real directories")
    repo_root = repo_root.resolve(strict=True)
    runtime_root = runtime_root.resolve(strict=True)
    mount_descriptors: list[int] = []
    command = [
        str(_BWRAP),
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--hostname",
        "fieldkit-docs",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind-fd",
        "{usr_fd}",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--dir",
        "/run",
        "--dir",
        str(_SANDBOX_REPOSITORY),
    ]
    try:
        if dependency_input is not None:
            if re.fullmatch(r"[0-9a-f]{64}", dependency_input.manifest_sha256) is None:
                raise ValueError("documentation dependency manifest digest is invalid")
            dependency_fd = os.dup(dependency_input.directory_descriptor)
            mount_descriptors.append(dependency_fd)
            if not stat.S_ISDIR(os.fstat(dependency_fd).st_mode):
                raise ValueError("documentation dependency input must be a held directory")
            command.extend(("--ro-bind-fd", str(dependency_fd), str(_SANDBOX_DEPENDENCIES)))
        usr_fd = _open_mount_source(Path("/usr"), escape_error="system runtime mount is unavailable")
        mount_descriptors.append(usr_fd)
        command[command.index("{usr_fd}")] = str(usr_fd)
        python_fd = (
            os.dup(runtime_tools.python_descriptor)
            if runtime_tools is not None
            else _open_mount_source(
                Path("/proc/self/exe"), escape_error="trusted supervisor interpreter is unavailable"
            )
        )
        mount_descriptors.append(python_fd)
        command.extend(("--ro-bind-fd", str(python_fd), "/run/python"))
        for entry in sorted(repo_root.iterdir(), key=lambda path: path.name):
            if entry.name in {".git", ".venv"}:
                continue
            descriptor = _open_mount_source(
                entry,
                trusted_root=repo_root,
                escape_error="candidate bind escapes supplied root",
            )
            mount_descriptors.append(descriptor)
            command.extend(("--ro-bind-fd", str(descriptor), str(_SANDBOX_REPOSITORY / entry.name)))
        virtual_environment = runtime_root / ".venv"
        if virtual_environment.exists() or virtual_environment.is_symlink():
            venv_fd = _open_mount_source(
                virtual_environment,
                trusted_root=runtime_root,
                escape_error="runtime virtual environment must be a real directory",
                require_real_directory=True,
            )
            mount_descriptors.append(venv_fd)
            workspace_venv_fd = os.dup(venv_fd)
            mount_descriptors.append(workspace_venv_fd)
            command.extend(directory_options(runtime_root))
            command.extend(("--ro-bind-fd", str(venv_fd), str(runtime_root / ".venv")))
            command.extend(("--ro-bind-fd", str(workspace_venv_fd), str(_SANDBOX_REPOSITORY / ".venv")))
            source = repo_root / "src"
            if source.is_dir():
                source_fd = _open_mount_source(
                    source,
                    trusted_root=repo_root,
                    escape_error="candidate bind escapes supplied root",
                    require_real_directory=True,
                )
                mount_descriptors.append(source_fd)
                command.extend(("--ro-bind-fd", str(source_fd), str(runtime_root / "src")))
        if argv[0] == "uv":
            if runtime_tools is not None:
                uv_fd = os.dup(runtime_tools.uv_descriptor)
            else:
                uv = shutil.which("uv")
                if uv is None:
                    raise OSError("documentation verifier locked runner is unavailable")
                uv_fd = _open_mount_source(Path(uv), escape_error="documentation verifier locked runner is unavailable")
            mount_descriptors.append(uv_fd)
            command.extend(("--dir", str(_SANDBOX_UV.parent)))
            command.extend(("--ro-bind-fd", str(uv_fd), str(_SANDBOX_UV)))
            command.extend(
                directory_options(
                    Path("/home/linuxbrew/.linuxbrew/lib")  # pii-guard: ignore — fixed Homebrew runtime mount
                )
            )
            command.extend(
                (
                    "--symlink",
                    "/lib64/ld-linux-x86-64.so.2",
                    "/home/linuxbrew/.linuxbrew/lib/ld.so",  # pii-guard: ignore — fixed Homebrew runtime mount
                )
            )
        if argv == _SEMANTIC_OWNER_ARGV:
            marker = temporary_root / "semantic-owner-marker"
            marker.write_text(_INHERITED_MODE, encoding="utf-8")
            marker_fd = _open_mount_source(
                marker, trusted_root=temporary_root, escape_error="semantic owner marker is unavailable", readable=True
            )
            mount_descriptors.append(marker_fd)
            marker.unlink()
            command.extend(("--ro-bind-data", str(marker_fd), str(_INHERITED_MARKER)))
        resolver_root = temporary_root / "resolver"
        command.extend(("--dir", "/etc"))
        for name in ("resolv.conf", "hosts", "nsswitch.conf"):
            resolver_path = resolver_root / name
            resolver_fd = _open_mount_source(
                resolver_path,
                trusted_root=resolver_root,
                escape_error="documentation resolver policy is unavailable",
                readable=True,
            )
            mount_descriptors.append(resolver_fd)
            resolver_path.unlink()
            command.extend(("--ro-bind-data", str(resolver_fd), f"/etc/{name}"))
        command.extend(system_ca_mounts(temporary_root, mount_descriptors))
        temporary_fd = _open_mount_source(
            temporary_root,
            trusted_root=temporary_root,
            escape_error="documentation scratch directory is unavailable",
            require_real_directory=True,
        )
        mount_descriptors.append(temporary_fd)
        command.extend(directory_options(temporary_root.parent))
        command.extend(("--bind-fd", str(temporary_fd), str(temporary_root)))
        command.extend(
            (
                "--chdir",
                str(_SANDBOX_REPOSITORY),
                "--",
                "/run/python",
                "-I",
                "-S",
                "-c",
                _SUBREAPER_SUPERVISOR,
                str(_DESCENDANT_GRACE_SECONDS),
                *_owner_argv(repo_root, argv, dependency_input),
            )
        )
    except BaseException:
        for descriptor in mount_descriptors:
            os.close(descriptor)
        raise
    return tuple(command), tuple(mount_descriptors)


def _kill_and_reap(process: subprocess.Popen[bytes]) -> bool:
    """Signal only an unreaped group owner, then wait for a bounded interval."""
    if process.poll() is not None:
        return True
    # This supervisor is the sole waiter. Until it reaps the owner, its PID
    # cannot be reused; namespace teardown handles sandbox descendants.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def _run(
    repo_root: Path,
    argv: tuple[str, ...],
    *,
    runtime_root: Path | None = None,
    runtime_tools: RuntimeTools | None = None,
    dependency_input: DependencyInput | None = None,
    source_revision: str | None = None,
) -> CommandResult:
    """Run one committed scenario without evaluating documentation text."""
    temporary_parent = Path(os.environ.get("TMPDIR", repo_root.parent))
    with tempfile.TemporaryDirectory(prefix="documentation-examples-", dir=temporary_parent) as temporary_directory:
        temporary_root = Path(temporary_directory)
        environment = _isolated_environment(temporary_root, source_revision=source_revision)
        return _run_bounded(
            repo_root,
            argv,
            environment,
            runtime_root=runtime_root,
            runtime_tools=runtime_tools,
            dependency_input=dependency_input,
        )


def _run_bounded(
    repo_root: Path,
    argv: tuple[str, ...],
    environment: dict[str, str] | None = None,
    *,
    timeout: int | None = None,
    runtime_root: Path | None = None,
    runtime_tools: RuntimeTools | None = None,
    dependency_input: DependencyInput | None = None,
    source_revision: str | None = None,
    inherited_owner: Literal["skill_semantic_contract"] | None = None,
) -> CommandResult:
    """Run one command in an OS sandbox with bounded output and cleanup."""
    if environment is None:
        temporary_parent = Path(os.environ.get("TMPDIR", repo_root.parent))
        with tempfile.TemporaryDirectory(prefix="documentation-examples-", dir=temporary_parent) as temporary_directory:
            temporary_root = Path(temporary_directory)
            return _run_bounded(
                repo_root,
                argv,
                _isolated_environment(temporary_root, source_revision=source_revision),
                timeout=timeout,
                runtime_root=runtime_root,
                runtime_tools=runtime_tools,
                dependency_input=dependency_input,
                inherited_owner=inherited_owner,
            )
    if source_revision is not None:
        if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
            raise ValueError("documentation source revision must be a full lowercase Git SHA")
        environment = {**environment, "FIELDKIT_DOC_SOURCE_REVISION": source_revision}
    temporary_root = Path(environment["TMPDIR"])
    if inherited_owner is not None:
        if not _inherited_semantic_sandbox(repo_root, inherited_owner):
            raise ValueError("inherited semantic sandbox context is unavailable")
        command = (
            "/run/python",
            "-I",
            "-S",
            "-c",
            _SUBREAPER_SUPERVISOR,
            str(_DESCENDANT_GRACE_SECONDS),
            *_sandbox_argv(repo_root, argv),
        )
        return _run_inherited(repo_root, argv, environment, command, timeout=timeout)
    sandbox, mount_descriptors = _sandbox_command(
        repo_root,
        runtime_root or _REPO_ROOT,
        temporary_root,
        argv,
        runtime_tools=runtime_tools,
        dependency_input=dependency_input,
    )
    try:
        with _trace_transport(temporary_root, sandbox, _sandbox_argv(repo_root, argv)[0]) as transport:
            return _run_traced(repo_root, argv, environment, transport, mount_descriptors, timeout=timeout)
    finally:
        for descriptor in mount_descriptors:
            os.close(descriptor)


def _inherited_semantic_sandbox(repo_root: Path, owner: str) -> bool:
    """Require the controller's fixed owner marker on a read-only mount."""
    if owner != _INHERITED_MODE or repo_root.resolve() != _SANDBOX_REPOSITORY:
        return False
    try:
        marker_status = _INHERITED_MARKER.lstat()
        return (
            stat.S_ISREG(marker_status.st_mode)
            and marker_status.st_size == len(_INHERITED_MODE)
            and bool(os.statvfs(_INHERITED_MARKER).f_flag & os.ST_RDONLY)
            and _INHERITED_MARKER.read_text(encoding="utf-8") == _INHERITED_MODE
        )
    except OSError:
        return False


def _run_inherited(
    repo_root: Path,
    argv: tuple[str, ...],
    environment: dict[str, str],
    command: tuple[str, ...],
    *,
    timeout: int | None,
) -> CommandResult:
    """Bound an inner owner while the outer controller traces its descendants."""
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout_tail = _BoundedBytes(_OUTPUT_LIMIT)
    stderr_tail = _BoundedBytes(_OUTPUT_LIMIT)
    errors: list[str] = []
    assert process.stdout is not None and process.stderr is not None
    readers = (
        threading.Thread(target=_drain, args=(process.stdout, stdout_tail, errors), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr_tail, errors), daemon=True),
    )
    for reader in readers:
        reader.start()
    try:
        exit_code = process.wait(timeout=timeout or _TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_inherited(process)
        exit_code = 124
    except BaseException:
        _stop_inherited(process)
        raise
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)
    if any(reader.is_alive() for reader in readers):
        _stop_inherited(process)
        errors.append("output pipe remained open after command exit")
        exit_code = 124
    if errors:
        stderr_tail.append(("\n".join(errors) + "\n").encode())
        if exit_code == 0:
            exit_code = 1
    return CommandResult(argv, exit_code, stdout_tail.text(), stderr_tail.text())


def _stop_inherited(process: subprocess.Popen[bytes]) -> None:
    """Let the sole-waiter subreaper kill detached descendants before reaping."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_and_reap(process)


def _run_traced(
    repo_root: Path,
    argv: tuple[str, ...],
    environment: dict[str, str],
    transport: _TraceTransport,
    mount_descriptors: tuple[int, ...],
    *,
    timeout: int | None,
) -> CommandResult:
    process = subprocess.Popen(
        transport.argv,
        cwd=repo_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=mount_descriptors,
    )
    stdout_tail = _BoundedBytes(
        _ARTIFACT_DIAGNOSTIC_OUTPUT_LIMIT if argv == _ARTIFACT_DIAGNOSTIC_ARGV else _OUTPUT_LIMIT
    )
    stderr_tail = _BoundedBytes(_OUTPUT_LIMIT)
    drain_errors: list[str] = []
    network_trace = _NetworkTrace(transport.owner_executable)
    assert process.stdout is not None and process.stderr is not None
    readers = (
        threading.Thread(target=_drain, args=(process.stdout, stdout_tail, drain_errors), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr_tail, drain_errors), daemon=True),
        threading.Thread(target=_drain_trace, args=(transport.reader, network_trace, drain_errors), daemon=True),
    )
    for reader in readers:
        reader.start()
    exit_code: int
    try:
        exit_code = process.wait(timeout=timeout or _COMMAND_TIMEOUTS.get(argv, _TIMEOUT_SECONDS))
    except subprocess.TimeoutExpired:
        _kill_and_reap(process)
        exit_code = 124
    except BaseException:
        _kill_and_reap(process)
        raise
    finally:
        transport.writer.close()
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)
    if any(reader.is_alive() for reader in readers):
        _kill_and_reap(process)
        for reader in readers:
            reader.join(timeout=_READER_JOIN_SECONDS)
        drain_errors.append("output pipe remained open after command exit")
        exit_code = 124
    if network_trace.attempted:
        stderr_tail.append(f"{_NETWORK_ATTEMPT_MESSAGE}\n".encode())
        if exit_code == 0:
            exit_code = 1
    if network_trace.interrupted_descendant:
        stderr_tail.append(b"documentation sandbox terminated an unfinished descendant\n")
        exit_code = 124
    if drain_errors and exit_code == 0:
        exit_code = 1
    if drain_errors:
        stderr_tail.append(("\n".join(drain_errors) + "\n").encode())
    return CommandResult(
        argv=argv,
        exit_code=exit_code,
        stdout=stdout_tail.text(),
        stderr=stderr_tail.text(),
    )
