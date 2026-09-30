"""Operating-system containment for executable documentation examples."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from scripts import documentation_command_runner

pytestmark = pytest.mark.integration
_RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _nested_owner(tmp_path: Path, program: str, *, timeout: int = 5) -> documentation_command_runner.CommandResult:
    """Exercise the exact outer owner with the real inherited runner code."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "documentation_command_runner.py",
        "documentation_commands.py",
        "documentation_runtime.py",
        "documentation_ca.py",
    ):
        shutil.copyfile(_RUNTIME_ROOT / "scripts" / name, scripts / name)
    (scripts / "check_skill_documentation_contract.py").write_text(
        "import json,os,sys\nfrom pathlib import Path\nsys.path.insert(0, '/workspace')\n"
        "from scripts.documentation_command_runner import _run_bounded\n"
        f"result=_run_bounded(Path('/workspace'), ('/run/python','-S','-c',{program!r}), "
        f"timeout={timeout}, inherited_owner='skill_semantic_contract')\n"
        "child_alive=False\n"
        "if result.stdout.strip().isdigit():\n"
        "    try:\n        os.kill(int(result.stdout.strip()),0)\n        child_alive=True\n"
        "    except ProcessLookupError:\n        pass\n"
        "print(json.dumps({'exit_code':result.exit_code,'stdout':result.stdout,"
        "'stderr':result.stderr,'child_alive':child_alive}))\n",
        encoding="utf-8",
    )
    return documentation_command_runner._run_bounded(
        tmp_path,
        documentation_command_runner._SEMANTIC_OWNER_ARGV,
        timeout=15,
        runtime_root=_RUNTIME_ROOT,
    )


def test_inherited_semantic_owner_runs_without_nested_containment(tmp_path: Path) -> None:
    result = _nested_owner(tmp_path, "print('nested success')")

    assert result.exit_code == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["exit_code"] == 0
    assert evidence["stdout"] == "nested success\n"


def test_outer_trace_rejects_swallowed_nested_network_attempt(tmp_path: Path) -> None:
    result = _nested_owner(
        tmp_path,
        "import socket; s=socket.socket(); s.connect_ex(('198.51.100.2',443)); print('swallowed')",
    )

    assert result.exit_code == 1
    assert "network attempt rejected" in result.stderr


def test_nested_timeout_reaps_detached_descendant(tmp_path: Path) -> None:
    result = _nested_owner(
        tmp_path,
        "import os,time; pid=os.fork(); "
        "(os.setsid(),time.sleep(30)) if pid==0 else "
        "(print(pid,flush=True),time.sleep(30))",
        timeout=1,
    )

    assert result.exit_code == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["exit_code"] == 124
    assert "unfinished descendant" in evidence["stderr"]
    assert evidence["child_alive"] is False


@pytest.mark.unit
def test_ambient_environment_cannot_authorize_inherited_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIELDKIT_DOC_OWNER", "skill_semantic_contract")
    marker = tmp_path / "forged-marker"
    marker.write_text("skill_semantic_contract", encoding="utf-8")
    monkeypatch.setattr(documentation_command_runner, "_INHERITED_MARKER", marker)

    assert not documentation_command_runner._inherited_semantic_sandbox(Path("/workspace"), "skill_semantic_contract")
    with pytest.raises(ValueError, match="inherited semantic sandbox context is unavailable"):
        documentation_command_runner._run_bounded(
            tmp_path, (sys.executable, "-c", "pass"), inherited_owner="skill_semantic_contract"
        )
    assert not os.statvfs(marker).f_flag & os.ST_RDONLY


def test_tracee_cannot_inherit_writable_trace_pipe(tmp_path: Path) -> None:
    program = """
import fcntl,json,os,stat
trace_pipes=[]
for name in os.listdir('/proc/self/fd'):
    descriptor=int(name)
    if descriptor <= 2:
        continue
    try:
        writable=fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
        if writable and stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            trace_pipes.append(descriptor)
    except OSError:
        pass
print(json.dumps(trace_pipes))
"""
    result = documentation_command_runner._run(
        tmp_path,
        (sys.executable, "-I", "-c", program),
        runtime_root=_RUNTIME_ROOT,
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_netlink_text_in_unix_endpoint_cannot_hide_network_attempt(tmp_path: Path) -> None:
    result = documentation_command_runner._run(
        tmp_path,
        (
            sys.executable,
            "-I",
            "-c",
            "import socket; s=socket.socket(socket.AF_UNIX); s.connect_ex('/tmp/AF_NETLINK'); print('done')",
        ),
        runtime_root=_RUNTIME_ROOT,
    )
    assert result.exit_code != 0
    assert "network attempt rejected" in result.stderr


@pytest.mark.unit
def test_oversized_trace_record_is_non_passing() -> None:
    trace = documentation_command_runner._NetworkTrace()
    trace.consume(b"x" * 9000)
    trace.finish()
    assert trace.attempted is True


def test_native_network_attempt_is_rejected_even_when_child_swallows_error(tmp_path: Path) -> None:
    """A ``-S`` child cannot bypass parent-observed network enforcement."""
    result = documentation_command_runner._run(
        tmp_path,
        (
            sys.executable,
            "-S",
            "-c",
            (
                "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); "
                "s.sendto(b'x', ('198.51.100.44', 9)); print('child reported success')"
            ),
        ),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code != 0
    assert "network attempt rejected" in result.stderr
    assert "198.51.100.44" not in result.stderr


def test_unix_socket_attempt_is_rejected_without_retaining_private_path(tmp_path: Path) -> None:
    """Host IPC is denied and its endpoint never enters retained evidence."""
    result = documentation_command_runner._run(
        tmp_path,
        (
            sys.executable,
            "-S",
            "-c",
            (
                "import socket; s=socket.socket(socket.AF_UNIX); "
                "s.connect_ex('/tmp/customer-private.sock'); print('child reported success')"
            ),
        ),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code != 0
    assert "network attempt rejected" in result.stderr
    assert "customer-private.sock" not in result.stderr


def test_native_dns_attempt_is_rejected_even_when_lookup_error_is_swallowed(tmp_path: Path) -> None:
    """The synthetic resolver makes native hostname lookup observable and denied."""
    result = documentation_command_runner._run(
        tmp_path,
        (
            sys.executable,
            "-S",
            "-c",
            (
                "import socket; "
                "\ntry: socket.getaddrinfo('private-customer.invalid', 443)"
                "\nexcept socket.gaierror: pass"
                "\nprint('child reported success')"
            ),
        ),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code != 0
    assert "network attempt rejected" in result.stderr
    assert "private-customer.invalid" not in result.stderr
    assert "198.51.100.1" not in result.stderr


def test_native_sendmmsg_attempt_is_rejected(tmp_path: Path) -> None:
    """Batched native datagrams cannot bypass the traced syscall set."""
    program = """
import ctypes
import socket

class Iovec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("length", ctypes.c_size_t)]

class MessageHeader(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_void_p),
        ("name_length", ctypes.c_uint),
        ("iov", ctypes.POINTER(Iovec)),
        ("iov_length", ctypes.c_size_t),
        ("control", ctypes.c_void_p),
        ("control_length", ctypes.c_size_t),
        ("flags", ctypes.c_int),
    ]

class MultiMessageHeader(ctypes.Structure):
    _fields_ = [("header", MessageHeader), ("length", ctypes.c_uint)]

class SocketAddress(ctypes.Structure):
    _fields_ = [
        ("family", ctypes.c_ushort),
        ("port", ctypes.c_ushort),
        ("address", ctypes.c_ubyte * 4),
        ("padding", ctypes.c_ubyte * 8),
    ]

payload = ctypes.create_string_buffer(b"x")
iovec = Iovec(ctypes.cast(payload, ctypes.c_void_p), 1)
address = SocketAddress(socket.AF_INET, socket.htons(9), (ctypes.c_ubyte * 4)(198, 51, 100, 23))
message = MultiMessageHeader(
    MessageHeader(
        ctypes.cast(ctypes.pointer(address), ctypes.c_void_p),
        ctypes.sizeof(address),
        ctypes.pointer(iovec),
        1,
        None,
        0,
        0,
    ),
    0,
)
descriptor = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ctypes.CDLL(None).sendmmsg(descriptor.fileno(), ctypes.pointer(message), 1, 0)
print("child reported success")
"""
    result = documentation_command_runner._run(
        tmp_path,
        (sys.executable, "-S", "-c", program),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code != 0
    assert "network attempt rejected" in result.stderr
    assert "198.51.100.23" not in result.stderr


def test_clone_untraced_network_attempt_is_rejected(tmp_path: Path) -> None:
    """A child cannot opt out of tracing and hide a denied network attempt."""
    program = """
import ctypes
import os
import socket

libc = ctypes.CDLL(None, use_errno=True)
pid = libc.syscall(56, 0x00800000 | 17, 0, 0, 0, 0)
if pid == 0:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"x", ("198.51.100.77", 9))
    except OSError:
        pass
    os._exit(0)
if pid < 0:
    raise OSError(ctypes.get_errno(), "clone failed")
os.waitpid(pid, 0)
print("clone child completed")
"""
    result = documentation_command_runner._run(
        tmp_path,
        (sys.executable, "-S", "-c", program),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code != 0
    assert "network attempt rejected" in result.stderr
    assert "198.51.100.77" not in result.stderr


def test_clone3_untraced_attempt_is_rejected_when_kernel_denies_clone(tmp_path: Path) -> None:
    """Requesting an untraced clone fails closed even when no child is created."""
    program = """
import ctypes

class CloneArguments(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("pidfd", ctypes.c_uint64),
        ("child_tid", ctypes.c_uint64),
        ("parent_tid", ctypes.c_uint64),
        ("exit_signal", ctypes.c_uint64),
        ("stack", ctypes.c_uint64),
        ("stack_size", ctypes.c_uint64),
        ("tls", ctypes.c_uint64),
        ("set_tid", ctypes.c_uint64),
        ("set_tid_size", ctypes.c_uint64),
        ("cgroup", ctypes.c_uint64),
    ]

arguments = CloneArguments(flags=0x00800000, exit_signal=17)
ctypes.CDLL(None, use_errno=True).syscall(435, ctypes.byref(arguments), ctypes.sizeof(arguments))
print("clone3 request completed")
"""
    result = documentation_command_runner._run(
        tmp_path,
        (sys.executable, "-S", "-c", program),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code != 0
    assert "network attempt rejected" in result.stderr


def test_io_uring_submission_path_is_rejected(tmp_path: Path) -> None:
    """Unsupported asynchronous kernel I/O cannot bypass syscall observation."""
    program = """
import ctypes

parameters = ctypes.create_string_buffer(256)
ctypes.CDLL(None, use_errno=True).syscall(425, 1, ctypes.byref(parameters))
print("io_uring request completed")
"""
    result = documentation_command_runner._run(
        tmp_path,
        (sys.executable, "-S", "-c", program),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code != 0
    assert "network attempt rejected" in result.stderr


def test_isolated_loopback_server_remains_available(tmp_path: Path) -> None:
    """Tests may communicate with services created inside their own namespace."""
    result = documentation_command_runner._run(
        tmp_path,
        (
            sys.executable,
            "-S",
            "-c",
            (
                "import socket,threading; server=socket.socket(); "
                "server.bind(('127.0.0.1',0)); server.listen(); port=server.getsockname()[1]; "
                "threading.Thread(target=lambda:server.accept()[0].close()).start(); "
                "client=socket.create_connection(('127.0.0.1',port),1); client.close(); "
                "server.close(); print('loopback ok')"
            ),
        ),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "loopback ok"


def test_detached_descendant_cannot_hold_output_pipes_open(tmp_path: Path) -> None:
    """PID isolation reaps a setsid descendant when the scenario parent exits."""
    started = time.monotonic()
    result = documentation_command_runner._run_bounded(
        tmp_path,
        (
            sys.executable,
            "-S",
            "-c",
            ("import os,time; pid=os.fork(); os._exit(0) if pid else (os.setsid(), time.sleep(30))"),
        ),
        timeout=2,
        runtime_root=_RUNTIME_ROOT,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert result.exit_code == 124


def test_child_cannot_read_host_files_or_repository_metadata(tmp_path: Path) -> None:
    """The mount namespace exposes the candidate, not host secrets or Git history."""
    host_secret = tmp_path.parent / "host-secret.txt"
    host_secret.write_text("not available to the child", encoding="utf-8")
    result = documentation_command_runner._run(
        tmp_path,
        (
            sys.executable,
            "-S",
            "-c",
            (
                "from pathlib import Path; "
                f"assert not Path({str(host_secret)!r}).exists(); "
                "assert not Path('/workspace/.git').exists(); print('contained')"
            ),
        ),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "contained"


def test_trace_and_resolver_policy_are_not_mutable_through_scratch(tmp_path: Path) -> None:
    """The child cannot drain tracing or replace the resolver inode used by libc."""
    program = """
import os
from pathlib import Path

scratch = Path(os.environ["TMPDIR"])
assert not (scratch / "network-trace.fifo").exists()
(scratch / "resolver/resolv.conf").write_text("nameserver 127.0.0.1\\n", encoding="utf-8")
assert Path("/etc/resolv.conf").read_text(encoding="utf-8").startswith("nameserver 198.51.100.1\\n")
print("policy sealed")
"""
    result = documentation_command_runner._run(
        tmp_path,
        (sys.executable, "-S", "-c", program),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "policy sealed"


@pytest.mark.parametrize("absolute", [False, True], ids=("relative", "absolute"))
def test_candidate_top_level_symlink_cannot_escape_snapshot(tmp_path: Path, absolute: bool) -> None:
    """Every candidate bind resolves inside the supplied immutable snapshot."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    secret = tmp_path / "host-secret.txt"
    secret.write_text("must not be mounted", encoding="utf-8")
    target = secret if absolute else Path("../host-secret.txt")
    (candidate / "escape").symlink_to(target)

    with pytest.raises(ValueError, match="candidate bind escapes supplied root"):
        documentation_command_runner._run(
            candidate,
            (sys.executable, "-S", "-c", "print(open('/workspace/escape').read())"),
            runtime_root=_RUNTIME_ROOT,
        )


def test_runtime_virtual_environment_must_not_be_a_symlink(tmp_path: Path) -> None:
    """The locked dependency mount cannot redirect to an arbitrary host directory."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".venv").symlink_to(_RUNTIME_ROOT / ".venv", target_is_directory=True)

    with pytest.raises(ValueError, match="runtime virtual environment must be a real directory"):
        documentation_command_runner._run(
            candidate,
            (sys.executable, "-S", "-c", "print('must not run')"),
            runtime_root=runtime,
        )


def test_locked_environment_imports_candidate_source_not_runtime_source(tmp_path: Path) -> None:
    """The dependency environment's editable path resolves to the supplied candidate."""
    package = tmp_path / "src/fieldkit"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("ORIGIN = 'candidate-snapshot'\n", encoding="utf-8")

    result = documentation_command_runner._run(
        tmp_path,
        (
            "uv",
            "run",
            "python",
            "-c",
            (
                "import fieldkit; from pathlib import Path; "
                f"assert not Path({str(_RUNTIME_ROOT / 'README.md')!r}).exists(); "
                "print(fieldkit.ORIGIN)"
            ),
        ),
        runtime_root=_RUNTIME_ROOT,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "candidate-snapshot"


def test_missing_os_containment_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unavailable namespace boundary cannot silently become a host execution."""
    monkeypatch.setattr(documentation_command_runner, "_BWRAP", tmp_path / "missing-bwrap")

    with pytest.raises(OSError, match="containment tools are unavailable"):
        documentation_command_runner._run(
            tmp_path,
            (sys.executable, "-S", "-c", "print('must not run')"),
            runtime_root=_RUNTIME_ROOT,
        )


def test_source_revision_is_explicit_validated_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host value cannot impersonate the candidate bound by the parent."""
    monkeypatch.setenv("FIELDKIT_DOC_SOURCE_REVISION", "f" * 40)
    unbound = documentation_command_runner._isolated_environment(tmp_path / "unbound")
    environment = documentation_command_runner._isolated_environment(tmp_path / "bound", source_revision="a" * 40)

    assert "FIELDKIT_DOC_SOURCE_REVISION" not in unbound
    assert environment["FIELDKIT_DOC_SOURCE_REVISION"] == "a" * 40

    with pytest.raises(ValueError, match="full lowercase Git SHA"):
        documentation_command_runner._isolated_environment(tmp_path / "invalid", source_revision="A" * 40)
