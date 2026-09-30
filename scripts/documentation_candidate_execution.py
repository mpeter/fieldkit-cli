"""Bind documentation verification to one immutable source candidate."""

import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process_bytes
from fieldkit.util.text_snapshot import read_text_snapshot

if TYPE_CHECKING or __package__:
    from scripts.documentation_command_runner import _BWRAP, _kill_and_reap, _open_mount_source
    from scripts.documentation_commands import EXAMPLE_COMMANDS
    from scripts.documentation_dependencies import prepare_dependencies
    from scripts.documentation_runtime import DependencyInput, RuntimeTools
    from scripts.documentation_snapshot import materialize_candidate
    from scripts.git_worktree import git_environment, head_revision, require_clean_worktree
    from scripts.json_policy import load_json_bytes
else:  # pragma: no cover - direct script execution
    from documentation_command_runner import _BWRAP, _kill_and_reap, _open_mount_source
    from documentation_commands import EXAMPLE_COMMANDS
    from documentation_dependencies import prepare_dependencies
    from documentation_runtime import DependencyInput, RuntimeTools
    from documentation_snapshot import materialize_candidate
    from git_worktree import git_environment, head_revision, require_clean_worktree
    from json_policy import load_json_bytes

_CONTRACT_COMMAND = ("uv", "run", "python", "scripts/check_documentation_contract.py")
_CONTRACT_PATH = Path("docs/documentation-contract.json")
_GIT_TIMEOUT_SECONDS = 10
_GIT_IDENTITY_OUTPUT_LIMIT_BYTES = 64 * 1024
_GIT_IDENTITY_STDERR_LIMIT_BYTES = 64 * 1024
_GIT_CLEANUP_TIMEOUT_SECONDS = 5
_MAX_CONTRACT_BYTES = 5 * 1024 * 1024
_RUNTIME_TIMEOUT_SECONDS = 600
_REPORT_LIMIT_BYTES = 4 * 1024 * 1024
_MAX_TOOL_BYTES = 128 * 1024 * 1024
_SANDBOX_SNAPSHOT = Path("/workspace")
_SANDBOX_RUNTIME = Path("/runtime")
_SANDBOX_UV_CACHE = Path("/uv-cache")

CommandPlan = tuple[tuple[str, ...], ...]
PlanBuilder = Callable[[Path], CommandPlan]
DiagnosticRunner = Callable[[Path, CommandPlan], tuple[list[dict[str, object]], tuple[str, ...]]]


@dataclass(frozen=True)
class TrustedUv:
    """Race-safe identity for the exact uv process that invoked verification."""

    descriptor: int
    sha256: str
    size: int


@dataclass(frozen=True)
class TrustedInterpreter:
    """Opened identity for the interpreter used to build and run evidence."""

    descriptor: int
    sha256: str
    size: int


@dataclass(frozen=True)
class BoundExecution:
    """Immutable execution inputs delivered only to trusted controller code."""

    snapshot: Path
    runtime_root: Path
    source_revision: str
    commands: CommandPlan
    tools: RuntimeTools
    dependency_input: DependencyInput


SupervisedRunner = Callable[[BoundExecution], tuple[list[dict[str, object]], tuple[str, ...]]]


def _descriptor_identity(descriptor: int, label: str) -> tuple[str, int]:
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_size <= 0 or status.st_size > _MAX_TOOL_BYTES:
        raise ValueError(f"{label} has an unsupported file identity")
    digest = hashlib.sha256()
    offset = 0
    while offset < status.st_size:
        chunk = os.pread(descriptor, min(1024 * 1024, status.st_size - offset), offset)
        if not chunk:
            raise ValueError(f"{label} could not be read completely")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest(), status.st_size


@contextmanager
def _trusted_uv(expected_sha256: str) -> Iterator[TrustedUv]:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("trusted uv digest must be a lowercase SHA-256")
    try:
        descriptor = os.open(f"/proc/{os.getppid()}/exe", os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        raise ValueError("trusted invoking uv executable is unavailable on this platform") from None
    try:
        observed, size = _descriptor_identity(descriptor, "trusted invoking uv executable")
        if observed != expected_sha256:
            raise ValueError("invoking uv executable does not match the explicitly trusted digest")
        yield TrustedUv(descriptor, observed, size)
    finally:
        os.close(descriptor)


@contextmanager
def _trusted_interpreter() -> Iterator[TrustedInterpreter]:
    """Hold the exact running interpreter so PATH cannot select another one."""
    try:
        descriptor = os.open("/proc/self/exe", os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        raise ValueError("trusted Python interpreter is unavailable on this platform") from None
    try:
        digest, size = _descriptor_identity(descriptor, "trusted Python interpreter")
        yield TrustedInterpreter(descriptor, digest, size)
    finally:
        os.close(descriptor)


@contextmanager
def diagnostic_tools() -> Iterator[RuntimeTools]:
    """Hold current tools for diagnostics without granting release approval."""
    uv_path = shutil.which("uv")
    if uv_path is None:
        raise ValueError("documentation diagnostic uv executable is unavailable")
    descriptor = _open_mount_source(
        Path(uv_path), escape_error="documentation diagnostic uv is unavailable", readable=True
    )
    try:
        uv_identity = _descriptor_identity(descriptor, "diagnostic uv executable")
        with _trusted_interpreter() as python:
            yield RuntimeTools(descriptor, python.descriptor)
            if _descriptor_identity(descriptor, "diagnostic uv executable") != uv_identity:
                raise ValueError("diagnostic uv identity changed during verification")
            if _descriptor_identity(python.descriptor, "diagnostic interpreter") != (python.sha256, python.size):
                raise ValueError("diagnostic interpreter identity changed during verification")
    finally:
        os.close(descriptor)


def _tree_identity(repo_root: Path, revision: str) -> str:
    try:
        result = run_bounded_process_bytes(
            ["git", "--no-replace-objects", "rev-parse", f"{revision}^{{tree}}"],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT_SECONDS,
            stdout_limit=_GIT_IDENTITY_OUTPUT_LIMIT_BYTES,
            stderr_limit=_GIT_IDENTITY_STDERR_LIMIT_BYTES,
            cleanup_timeout=_GIT_CLEANUP_TIMEOUT_SECONDS,
            env=git_environment(),
        )
        tree = result.stdout.decode("ascii").strip()
    except (BoundedProcessError, UnicodeError):
        raise ValueError("candidate tree identity is unavailable") from None
    if result.returncode != 0 or len(tree) != 40 or any(character not in "0123456789abcdef" for character in tree):
        raise ValueError("candidate tree identity is unavailable")
    return tree


def _is_clean(repo_root: Path) -> bool:
    try:
        require_clean_worktree(repo_root)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return True


def _contract_sha256(path: Path) -> str | None:
    try:
        snapshot = read_text_snapshot(path, max_bytes=_MAX_CONTRACT_BYTES)
        content = snapshot.content.encode("utf-8")
        load_json_bytes(content)
        return hashlib.sha256(content).hexdigest()
    except (OSError, ValueError):
        return None


def candidate_binding(repo_root: Path, commands: Sequence[tuple[str, ...]]) -> dict[str, object]:
    """Capture the complete immutable identity used by documentation owners."""
    try:
        revision = head_revision(repo_root)
        tree = _tree_identity(repo_root, revision)
    except (OSError, ValueError, subprocess.SubprocessError):
        revision = None
        tree = None
    contract_sha256 = _contract_sha256(repo_root / _CONTRACT_PATH)
    clean = revision is not None and tree is not None and contract_sha256 is not None and _is_clean(repo_root)
    encoded_commands = json.dumps(list(commands), ensure_ascii=True, separators=(",", ":")).encode()
    return {
        "evidence_kind": "release_candidate" if clean else "diagnostic",
        "clean": clean,
        "source_revision": revision,
        "source_tree": tree,
        "documentation_contract_sha256": contract_sha256,
        "complete_commands_sha256": hashlib.sha256(encoded_commands).hexdigest(),
    }


def _same_source(before: dict[str, object], after: dict[str, object]) -> bool:
    fields = (
        "clean",
        "source_revision",
        "source_tree",
        "documentation_contract_sha256",
        "complete_commands_sha256",
    )
    return all(before[field] == after[field] for field in fields)


def _uv_cache_root() -> Path:
    """Return uv's supported default cache without trusting ambient path overrides."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache/uv"


def _runtime_builder_command(
    snapshot: Path,
    runtime_root: Path,
    uv: TrustedUv,
    python: TrustedInterpreter,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Put the offline builder behind a minimal PID and filesystem boundary."""
    if not _BWRAP.is_file() or not os.access(_BWRAP, os.X_OK):
        raise ValueError("locked documentation runtime containment is unavailable")
    mount_descriptors: list[int] = []
    try:
        usr_descriptor = _open_mount_source(
            Path("/usr"),
            escape_error="system runtime mount is unavailable",
            require_real_directory=True,
        )
        mount_descriptors.append(usr_descriptor)
        snapshot_descriptor = _open_mount_source(
            snapshot,
            trusted_root=snapshot,
            escape_error="candidate runtime source must be a real directory",
            require_real_directory=True,
        )
        mount_descriptors.append(snapshot_descriptor)
        runtime_descriptor = _open_mount_source(
            runtime_root,
            trusted_root=runtime_root,
            escape_error="candidate runtime destination must be a real directory",
            require_real_directory=True,
        )
        mount_descriptors.append(runtime_descriptor)
        cache_root = _uv_cache_root()
        cache_descriptor = _open_mount_source(
            cache_root,
            trusted_root=cache_root,
            escape_error="offline uv cache must be a real directory",
            require_real_directory=True,
        )
        mount_descriptors.append(cache_descriptor)
        command = (
            str(_BWRAP),
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--hostname",
            "fieldkit-docs-build",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/run",
            "--ro-bind-fd",
            str(uv.descriptor),
            "/run/uv",
            "--ro-bind-fd",
            str(python.descriptor),
            "/run/python",
            "--ro-bind-fd",
            str(usr_descriptor),
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
            "/home",
            "--dir",
            "/home/linuxbrew",
            "--dir",
            "/home/linuxbrew/.linuxbrew",  # pii-guard: ignore — fixed Homebrew runtime mount
            "--dir",
            "/home/linuxbrew/.linuxbrew/lib",  # pii-guard: ignore — fixed Homebrew runtime mount
            "--symlink",
            "/lib64/ld-linux-x86-64.so.2",
            "/home/linuxbrew/.linuxbrew/lib/ld.so",  # pii-guard: ignore — fixed Homebrew runtime mount
            "--ro-bind-fd",
            str(snapshot_descriptor),
            str(_SANDBOX_SNAPSHOT),
            "--bind-fd",
            str(runtime_descriptor),
            str(_SANDBOX_RUNTIME),
            "--overlay-src",
            f"/proc/self/fd/{cache_descriptor}",
            "--tmp-overlay",
            str(_SANDBOX_UV_CACHE),
            "--chdir",
            str(_SANDBOX_SNAPSHOT),
            "--",
            "/run/uv",
            "sync",
            "--locked",
            "--all-extras",
            "--dev",
            "--group",
            "release-build",
            "--offline",
            "--no-editable",
            "--link-mode",
            "copy",
            "--project",
            str(_SANDBOX_SNAPSHOT),
            "--python",
            "/run/python",
            "--no-managed-python",
            "--no-python-downloads",
            "--quiet",
        )
    except BaseException:
        for descriptor in mount_descriptors:
            os.close(descriptor)
        raise
    return command, tuple(mount_descriptors)


@contextmanager
def _build_runtime(
    snapshot: Path,
    runtime_root: Path,
    uv: TrustedUv,
    python: TrustedInterpreter,
) -> Iterator[TrustedInterpreter]:
    """Create a private exact environment from the candidate lock."""
    virtual_environment = runtime_root / ".venv"
    environment = {
        key: value for key, value in os.environ.items() if key in {"LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"}
    }
    environment.update(
        {
            "HOME": "/nonexistent",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_CACHE_DIR": str(_SANDBOX_UV_CACHE),
            "UV_OFFLINE": "1",
            "UV_NO_MANAGED_PYTHON": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_PROJECT_ENVIRONMENT": str(_SANDBOX_RUNTIME / ".venv"),
        }
    )
    process: subprocess.Popen[bytes] | None = None
    mount_descriptors: tuple[int, ...] = ()
    try:
        command, mount_descriptors = _runtime_builder_command(snapshot, runtime_root, uv, python)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            pass_fds=(uv.descriptor, python.descriptor, *mount_descriptors),
            start_new_session=True,
        )
        for descriptor in mount_descriptors:
            os.close(descriptor)
        mount_descriptors = ()
        exit_code = process.wait(timeout=_RUNTIME_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if process is not None:
            _kill_and_reap(process)
        raise ValueError("locked documentation runtime could not be built") from None
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            _kill_and_reap(process)
        raise ValueError("locked documentation runtime could not be built") from None
    except BaseException:
        if process is not None:
            _kill_and_reap(process)
        raise
    finally:
        for descriptor in mount_descriptors:
            os.close(descriptor)
    cleanup_complete = _kill_and_reap(process)
    interpreter = virtual_environment / "bin/python"
    if (
        exit_code != 0
        or not cleanup_complete
        or virtual_environment.is_symlink()
        or not virtual_environment.is_dir()
        or not interpreter.is_symlink()
        or interpreter.readlink() != Path("/run/python")
    ):
        raise ValueError("candidate lock cannot reproduce the documentation runtime offline")
    digest, size = _descriptor_identity(python.descriptor, "candidate runtime interpreter")
    if digest != python.sha256 or size != python.size:
        raise ValueError("candidate runtime interpreter does not match the trusted interpreter")
    yield python


def _run_bound_plan(
    snapshot: Path,
    runtime_root: Path,
    revision: str,
    owner_commands: CommandPlan,
    uv: TrustedUv,
    python: TrustedInterpreter,
    runner: SupervisedRunner,
    *,
    dependency_input: DependencyInput | None = None,
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    """Prepare dependencies before building the runtime and supervising owners."""
    preparation_tools = RuntimeTools(uv.descriptor, python.descriptor)
    with (
        prepare_dependencies(snapshot, preparation_tools, supplied=dependency_input) as dependencies,
        _build_runtime(snapshot, runtime_root, uv, python) as interpreter,
    ):
        tools = RuntimeTools(uv.descriptor, interpreter.descriptor)
        context = BoundExecution(snapshot, runtime_root, revision, owner_commands, tools, dependencies)
        results, pending = runner(context)
        if _descriptor_identity(uv.descriptor, "trusted uv") != (uv.sha256, uv.size):
            raise ValueError("trusted uv identity changed during verification")
        if _descriptor_identity(python.descriptor, "trusted interpreter") != (python.sha256, python.size):
            raise ValueError("trusted interpreter identity changed during verification")
        return results, pending


def _result_argv(results: list[dict[str, object]]) -> CommandPlan:
    commands: list[tuple[str, ...]] = []
    for result in results:
        argv = result.get("argv")
        exit_code = result.get("exit_code")
        if (
            not isinstance(argv, (list, tuple))
            or not all(isinstance(part, str) for part in argv)
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or not isinstance(result.get("stdout"), str)
            or not isinstance(result.get("stderr"), str)
        ):
            raise ValueError("documentation supervisor produced an invalid command result")
        commands.append(tuple(argv))
    return tuple(commands)


def execute_bound_candidate(
    repo_root: Path,
    plan_builder: PlanBuilder,
    diagnostic_runner: DiagnosticRunner,
    *,
    supervised_runner: SupervisedRunner | None = None,
    trusted_uv_sha256: str | None = None,
    dependency_input: DependencyInput | None = None,
) -> tuple[list[dict[str, object]], tuple[str, ...], dict[str, object]]:
    """Execute a clean candidate through trusted supervision and private runtime."""
    source = repo_root.resolve(strict=True)
    clean = _is_clean(source)
    if clean:
        if trusted_uv_sha256 is None:
            raise ValueError("clean candidate verification requires an explicitly trusted uv digest")
        if supervised_runner is None:
            raise ValueError("clean candidate verification requires trusted owner supervision")
        revision = head_revision(source)
        with (
            _trusted_uv(trusted_uv_sha256) as uv,
            _trusted_interpreter() as python,
            tempfile.TemporaryDirectory(prefix="fieldkit-documentation-candidate-", dir=source.parent) as temporary,
        ):
            private_root = Path(temporary)
            snapshot = private_root / "source"
            runtime_root = private_root / "runtime"
            runtime_root.mkdir(mode=0o700)
            materialize_candidate(source, revision, snapshot)
            owner_commands = plan_builder(snapshot)
            complete_commands = (_CONTRACT_COMMAND, *owner_commands)
            before = candidate_binding(source, complete_commands)
            if not before["clean"] or before["source_revision"] != revision:
                raise ValueError("candidate changed before committed verification began")
            results, pending = _run_bound_plan(
                snapshot,
                runtime_root,
                revision,
                owner_commands,
                uv,
                python,
                supervised_runner,
                dependency_input=dependency_input,
            )
        immutable_snapshot = True
        runtime_environment_checked = True
        runtime_builder = {
            "name": "uv",
            "sha256": uv.sha256,
            "size": uv.size,
            "python_sha256": python.sha256,
            "python_size": python.size,
        }
    else:
        owner_commands = plan_builder(source)
        complete_commands = (_CONTRACT_COMMAND, *owner_commands)
        before = candidate_binding(source, complete_commands)
        results, pending = diagnostic_runner(source, owner_commands)
        immutable_snapshot = False
        runtime_environment_checked = False
        runtime_builder = None
    returned_argv = _result_argv(results)
    expected_argv = owner_commands
    if not clean:
        artifact_argv = EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
        expected_argv = tuple(
            (*command, "--diagnostic-dirty") if command == artifact_argv else command for command in owner_commands
        )
    if returned_argv != expected_argv:
        raise ValueError("documentation supervisor results do not match the frozen execution plan")
    after = candidate_binding(source, complete_commands)
    source_unchanged = clean and bool(before["clean"]) and _same_source(before, after)
    binding = {
        **before,
        "clean": bool(before["clean"]) and source_unchanged,
        "evidence_kind": "release_candidate" if before["clean"] and source_unchanged else "diagnostic",
        "immutable_snapshot": immutable_snapshot,
        "runtime_environment_checked": runtime_environment_checked,
        "runtime_builder": runtime_builder,
        "runtime_tool_approval": "pending",
        "source_unchanged": source_unchanged,
    }
    return results, pending, binding


@dataclass
class ExternalReport:
    """Held report directory that cannot be redirected through a later symlink."""

    expected_parent: Path
    directory: int
    filename: str
    source_device: int
    directory_device: int

    def write(self, content: str) -> None:
        if len(content.encode("utf-8")) > _REPORT_LIMIT_BYTES:
            raise ValueError("documentation report exceeds its size bound")
        if os.fstat(self.directory).st_dev != self.directory_device or self.directory_device == self.source_device:
            raise ValueError("documentation report lost its filesystem boundary")
        try:
            current = Path(f"/proc/self/fd/{self.directory}").resolve(strict=True)
        except OSError:
            raise ValueError("documentation report directory changed during verification") from None
        if current != self.expected_parent:
            raise ValueError("documentation report directory changed during verification")
        temporary = f".{self.filename}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.filename, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self.directory)


@dataclass(frozen=True)
class StdoutReport:
    """Bounded report stream with no filesystem destination."""

    def write(self, content: str) -> None:
        if len(content.encode("utf-8")) > _REPORT_LIMIT_BYTES:
            raise ValueError("documentation report exceeds its size bound")
        sys.stdout.write(content)
        sys.stdout.flush()


@contextmanager
def external_report(source: Path, output: str | Path | None) -> Iterator[ExternalReport | StdoutReport | None]:
    """Hold a verified external parent directory for the complete verification."""
    if output is None:
        yield None
        return
    if os.fspath(output) == "-":
        yield StdoutReport()
        return
    output = Path(output)
    root = source.resolve(strict=True)
    source_device = root.stat().st_dev
    try:
        parent = output.parent.resolve(strict=True)
    except OSError:
        raise ValueError("documentation report parent is unavailable") from None
    if parent == root or parent.is_relative_to(root) or output.name in {"", ".", ".."}:
        raise ValueError("documentation report must be outside the candidate source")
    if output.exists() or output.is_symlink():
        try:
            resolved = output.resolve(strict=True)
        except OSError:
            raise ValueError("documentation report target is unavailable") from None
        if resolved == root or resolved.is_relative_to(root):
            raise ValueError("documentation report must be outside the candidate source")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor: int | None = None
    try:
        descriptor = os.open(parent, flags)
        opened = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
        if opened != parent:
            raise ValueError("documentation report parent changed during validation")
        directory_device = os.fstat(descriptor).st_dev
        if directory_device == source_device:
            raise ValueError("documentation file report requires a separate filesystem; use --report -")
        yield ExternalReport(parent, descriptor, output.name, source_device, directory_device)
    finally:
        if descriptor is not None:
            os.close(descriptor)
