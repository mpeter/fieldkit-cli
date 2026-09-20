"""Trusted-base snapshotting and exact-head completion-check execution."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal

from fieldkit.config._timeouts import (
    DRIVER_ATTEMPT_OUTPUT_BYTES,
    DRIVER_CHECK_OUTPUT_BYTES,
    DRIVER_OUTPUT_TAIL_BYTES,
    TIMEOUT_DRIVER_CHECK,
    TIMEOUT_DRIVER_FINALIZE_RESERVE,
    TIMEOUT_DRIVER_GIT,
    TIMEOUT_DRIVER_OUTPUT_DRAIN,
    TIMEOUT_DRIVER_RUN,
    TIMEOUT_PROCESS_KILL_GRACE,
)
from fieldkit.driver.done_check_evidence import CheckEvidence, EvidenceRecord, OutputEvidence, write_evidence
from fieldkit.driver.done_checks import (
    ArgvCheck,
    CheckerCheck,
    DoneCheck,
    DoneCheckContract,
    DoneCheckError,
    parse_done_checks,
)
from fieldkit.driver.github import GitHubLookupError, PullRequestIdentity, get_pr_identity
from fieldkit.errors import AuthError

_SHA_RE = frozenset("0123456789abcdef")
_AGGREGATE_MAKE = frozenset({"quality", "quality-full", "gazepy"})
_AUTHORITY_FILES = frozenset(
    {
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "tach.toml",
        "src/fieldkit/config/_timeouts.py",
        "src/fieldkit/config/__init__.py",
        "tests/test_quality_contract.py",
        "tests/test_governance_invariants.py",
        ".gaze/baseline.json",
        ".skillsaw-baseline.json",
    }
)


class VerificationError(RuntimeError):
    """Independent completion verification failed closed."""


@dataclass(frozen=True)
class TrustedChecker:
    path: str
    sha256: str
    snapshot_path: Path


@dataclass(frozen=True)
class TrustedCheckSnapshot:
    """Trusted-base authority captured before candidate execution."""

    attempt_id: str
    repository: str
    issue_number: int
    attempt: int
    branch: str
    base_sha: str
    work_order_path: str
    work_order_sha256: str
    contract_sha256: str
    contract: DoneCheckContract
    checkers: tuple[TrustedChecker, ...]
    authority: tuple[tuple[str, str, str], ...]
    executables: tuple[tuple[str, str, str], ...]
    python_environment: str
    makefile_path: Path | None
    snapshot_dir: Path


@dataclass(frozen=True)
class HeadIdentity:
    pr_number: int
    repository_id: int
    base_branch: str
    head_branch: str
    head_sha: str


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: str
    exit_code: int | None
    duration_ms: int
    argv: tuple[str, ...]
    executable_sha256: str
    stdout: OutputEvidence
    stderr: OutputEvidence
    stdout_tail: bytes
    stderr_tail: bytes


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    reason: str
    identity: HeadIdentity | None
    checks: tuple[CheckResult, ...]
    cleanup_status: str
    evidence_path: Path | None


@dataclass
class _VerificationProgress:
    identity: HeadIdentity | None
    checks: list[CheckResult]
    worktree: Path | None
    artifacts: Path
    evidence_path: Path | None


@dataclass
class _StreamCapture:
    digest: object
    count: int
    tail: deque[bytes]
    tail_bytes: int
    complete: bool
    expected: bytes | None
    expected_matches: bool

    @classmethod
    def create(cls, expected: bytes | None = None) -> "_StreamCapture":
        return cls(hashlib.sha256(), 0, deque(), 0, True, expected, True)

    def add(self, chunk: bytes) -> None:
        if self.expected is not None and chunk != self.expected[self.count : self.count + len(chunk)]:
            self.expected_matches = False
        self.digest.update(chunk)  # type: ignore[attr-defined]
        self.count += len(chunk)
        self.tail.append(chunk)
        self.tail_bytes += len(chunk)
        while self.tail and self.tail_bytes - len(self.tail[0]) >= DRIVER_OUTPUT_TAIL_BYTES:
            self.tail_bytes -= len(self.tail.popleft())

    def tail_value(self) -> bytes:
        return b"".join(self.tail)[-DRIVER_OUTPUT_TAIL_BYTES:]

    def evidence(self) -> OutputEvidence:
        return OutputEvidence(self.count, self.digest.hexdigest(), self.complete)  # type: ignore[attr-defined]


def _git(
    repo_root: Path, *args: str, check: bool = True, timeout: float = TIMEOUT_DRIVER_GIT
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError(f"git {' '.join(args)} failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or f"git {' '.join(args)} failed"
        if any(
            marker in detail.lower()
            for marker in ("authentication failed", "could not read username", "permission denied (publickey)")
        ):
            raise AuthError(detail)
        raise VerificationError(detail)
    return result


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, check=False, timeout=TIMEOUT_DRIVER_GIT
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError(f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        raise VerificationError(result.stderr.decode(errors="replace").strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _contract_hash(contract: DoneCheckContract) -> str:
    records: list[dict[str, object]] = []
    for check in contract.checks:
        record: dict[str, object] = {
            "id": check.id,
            "expected_exit": check.expected_exit,
            "expected_stdout": check.expected_stdout.decode("utf-8") if check.expected_stdout is not None else None,
        }
        if isinstance(check, ArgvCheck):
            record["argv"] = check.argv
        else:
            record.update({"checker": check.checker, "args": check.args})
        records.append(record)
    payload = json.dumps({"version": contract.version, "checks": records}, sort_keys=True, separators=(",", ":"))
    return _sha256(payload.encode())


def _authority_manifest(repo_root: Path, revision: str) -> tuple[tuple[str, str, str], ...]:
    output = _git(repo_root, "ls-tree", "-r", revision).stdout
    entries: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        metadata, path = line.split("\t", 1)
        mode, kind, blob = metadata.split()
        included = (
            path in _AUTHORITY_FILES
            or (path.startswith("scripts/") and path.endswith(".py"))
            or (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
        )
        if included and kind == "blob":
            entries.append((path, mode, blob))
    return tuple(sorted(entries))


def _trusted_executables(contract: DoneCheckContract, tool_environment: Path) -> tuple[tuple[str, str, str], ...]:
    names: set[str] = {"git", "python"}
    for check in contract.checks:
        if isinstance(check, ArgvCheck):
            names.add(check.argv[0])
            names.add(check.normalized_argv[0])
            if check.argv[0] == "make":
                names.update({"uv", "uvx"})
    resolved: list[tuple[str, str, str]] = []
    for name in sorted(names):
        verifier_tool = tool_environment / "bin" / name
        path: str | None
        if verifier_tool.is_file():
            path = str(verifier_tool)
        else:
            path = sys.executable if name == "python" else shutil.which(name)
        if path is None or not Path(path).is_file():
            raise VerificationError(f"trusted executable is unavailable: {name}")
        resolved_path = Path(path).absolute()
        resolved.append((name, str(resolved_path), _sha256(resolved_path.read_bytes())))
    return tuple(resolved)


def _make_tree_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(0o555 if path.is_dir() or mode & 0o111 else 0o444)
    root.chmod(0o555)


def _prepare_tool_environment(repo_root: Path, base_sha: str, snapshot_dir: Path) -> Path:
    project = snapshot_dir / "tool-project"
    project.mkdir(mode=0o700)
    for name in ("pyproject.toml", "uv.lock"):
        (project / name).write_bytes(_git_bytes(repo_root, "show", f"{base_sha}:{name}"))
    environment = snapshot_dir / "tool-environment"
    uv = shutil.which("uv")
    if uv is None:
        raise VerificationError("trusted executable is unavailable: uv")
    result = subprocess.run(
        [
            uv,
            "sync",
            "--frozen",
            "--no-install-project",
            "--python",
            sys.executable,
            "--project",
            str(project),
        ],
        cwd=snapshot_dir,
        env={"PATH": str(Path(uv).parent), "UV_NO_PROGRESS": "1", "UV_PROJECT_ENVIRONMENT": str(environment)},
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_DRIVER_CHECK,
    )
    if result.returncode != 0:
        raise VerificationError(result.stderr.strip() or "trusted verifier environment setup failed")
    _make_tree_read_only(environment)
    project.chmod(0o500)
    return environment


def _trusted_contract(repo_root: Path, work_order_path: Path, base_sha: str) -> tuple[str, bytes, DoneCheckContract]:
    try:
        relative = work_order_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise VerificationError("work order lies outside the repository") from exc
    blob = _git_bytes(repo_root, "show", f"{base_sha}:{relative}")
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("work order is not valid UTF-8") from exc
    try:
        return relative, blob, parse_done_checks(text)
    except DoneCheckError as exc:
        raise VerificationError(f"invalid trusted done-check contract: {exc}") from exc


def _snapshot_checkers(
    repo_root: Path, base_sha: str, contract: DoneCheckContract, snapshot_dir: Path
) -> tuple[TrustedChecker, ...]:
    checker_checks = [check for check in contract.checks if isinstance(check, CheckerCheck)]
    if not checker_checks:
        return ()
    checker_root = snapshot_dir / "done_checks"
    checker_root.mkdir(mode=0o700)
    checkers: list[TrustedChecker] = []
    for check in checker_checks:
        blob = _git_bytes(repo_root, "show", f"{base_sha}:{check.checker}")
        destination = checker_root / Path(check.checker).name
        destination.write_bytes(blob)
        destination.chmod(0o400)
        checkers.append(TrustedChecker(check.checker, _sha256(blob), destination))
    validator_blob = _git_bytes(repo_root, "show", f"{base_sha}:scripts/check_done_checkers.py")
    validator = snapshot_dir / "check_done_checkers.py"
    validator.write_bytes(validator_blob)
    validator.chmod(0o400)
    validation = subprocess.run(
        [sys.executable, "-I", str(validator)],
        cwd=snapshot_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_DRIVER_CHECK,
    )
    if validation.returncode != 0:
        raise VerificationError(validation.stdout.strip() or validation.stderr.strip() or "checker policy failed")
    checker_root.chmod(0o500)
    return tuple(checkers)


def create_trusted_snapshot(
    repo_root: Path,
    work_order_path: Path,
    *,
    repository: str,
    issue_number: int,
    attempt: int,
    branch: str,
    snapshot_root: Path,
) -> TrustedCheckSnapshot:
    """Capture validated work-order and checker blobs from ``origin/main``."""
    base_sha = _git(repo_root, "rev-parse", "origin/main").stdout.strip().lower()
    if len(base_sha) != 40 or any(character not in _SHA_RE for character in base_sha):
        raise VerificationError("origin/main did not resolve to a full commit SHA")
    relative_work_order, work_order_blob, contract = _trusted_contract(repo_root, work_order_path, base_sha)
    attempt_id = uuid.uuid4().hex
    snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot_root.chmod(0o700)
    snapshot_dir = snapshot_root / attempt_id
    snapshot_dir.mkdir(mode=0o700)
    snapshot_dir.chmod(0o700)
    try:
        checkers = _snapshot_checkers(repo_root, base_sha, contract, snapshot_dir)
        makefile_path = None
        if any(isinstance(check, ArgvCheck) and check.argv[0] == "make" for check in contract.checks):
            makefile_path = snapshot_dir / "Makefile"
            makefile_path.write_bytes(_git_bytes(repo_root, "show", f"{base_sha}:Makefile"))
            makefile_path.chmod(0o400)
        (snapshot_dir / "work-order.md").write_bytes(work_order_blob)
        (snapshot_dir / "work-order.md").chmod(0o400)
        tool_environment = _prepare_tool_environment(repo_root, base_sha, snapshot_dir)
        executables = _trusted_executables(contract, tool_environment)
        snapshot = TrustedCheckSnapshot(
            attempt_id=attempt_id,
            repository=repository,
            issue_number=issue_number,
            attempt=attempt,
            branch=branch,
            base_sha=base_sha,
            work_order_path=relative_work_order,
            work_order_sha256=_sha256(work_order_blob),
            contract_sha256=_contract_hash(contract),
            contract=contract,
            checkers=checkers,
            authority=_authority_manifest(repo_root, base_sha),
            executables=executables,
            python_environment=str(tool_environment),
            makefile_path=makefile_path,
            snapshot_dir=snapshot_dir,
        )
        snapshot_dir.chmod(0o500)
        return snapshot
    except BaseException as exc:
        if not _remove_snapshot_dir(snapshot_dir):
            exc.add_note(f"trusted snapshot cleanup failed: {snapshot_dir}")
        raise


def _remove_snapshot_dir(snapshot_dir: Path) -> bool:
    for path in snapshot_dir.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
    snapshot_dir.chmod(0o700)
    return _bounded_rmtree(snapshot_dir, TIMEOUT_DRIVER_OUTPUT_DRAIN)


def remove_snapshot(snapshot: TrustedCheckSnapshot) -> bool:
    """Remove a trusted snapshot after terminal finalization."""
    return _remove_snapshot_dir(snapshot.snapshot_dir)


def _head(identity: PullRequestIdentity) -> HeadIdentity:
    return HeadIdentity(
        identity.number, identity.repository_id, identity.base_branch, identity.head_branch, identity.head_sha
    )


def _validate_identity(snapshot: TrustedCheckSnapshot, identity: HeadIdentity) -> None:
    if identity.base_branch != "main":
        raise VerificationError(f"pull request base is {identity.base_branch!r}, expected 'main'")
    if identity.head_branch != snapshot.branch:
        raise VerificationError(f"pull request head is {identity.head_branch!r}, expected {snapshot.branch!r}")


def _replace_artifacts(values: tuple[str, ...], artifact_root: Path) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value.startswith("{artifacts}/"):
            relative = value.removeprefix("{artifacts}/")
            destination = (artifact_root / relative).resolve()
            if not destination.is_relative_to(artifact_root.resolve()):
                raise VerificationError("artifact path escaped its private root")
            destination.parent.mkdir(parents=True, exist_ok=True)
            result.append(str(destination))
        else:
            result.append(value)
    return tuple(result)


def _check_argv(snapshot: TrustedCheckSnapshot, check: DoneCheck, candidate: Path, artifacts: Path) -> tuple[str, ...]:
    executables = {name: path for name, path, _ in snapshot.executables}
    if isinstance(check, CheckerCheck):
        checker = next((item for item in snapshot.checkers if item.path == check.checker), None)
        if checker is None or _sha256(checker.snapshot_path.read_bytes()) != checker.sha256:
            raise VerificationError(f"trusted checker hash changed: {check.checker}")
        args = _replace_artifacts(check.args, artifacts)
        return (executables["python"], "-I", str(checker.snapshot_path), *args)
    if check.argv[0] == "make":
        if snapshot.makefile_path is None:
            raise VerificationError("trusted Makefile is unavailable")
        return (executables["make"], "-f", str(snapshot.makefile_path), check.argv[1])
    args = _replace_artifacts(check.normalized_argv[1:], artifacts)
    return (executables[check.normalized_argv[0]], *args)


def _minimal_environment(snapshot: TrustedCheckSnapshot, candidate: Path, check: DoneCheck) -> dict[str, str]:
    directories = sorted({str(Path(path).parent) for _, path, _ in snapshot.executables})
    environment = {
        "PATH": os.pathsep.join(directories),
        "PYTHONPATH": str(candidate / "src"),
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if isinstance(check, ArgvCheck) and check.argv[0] == "make":
        environment.update(
            {
                "MAKEFLAGS": "",
                "MFLAGS": "",
                "MAKEFILES": "",
                "UV_NO_SYNC": "1",
                "UV_PROJECT_ENVIRONMENT": snapshot.python_environment,
            }
        )
    return environment


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TIMEOUT_PROCESS_KILL_GRACE)
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        pass
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TIMEOUT_PROCESS_KILL_GRACE)


def _drain_stream(
    stream: BinaryIO,
    capture: _StreamCapture,
    peer: _StreamCapture,
    attempt_bytes: list[int],
    overflow: threading.Event,
    lock: threading.Lock,
) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            with lock:
                capture.add(chunk)
                attempt_bytes[0] += len(chunk)
                if (
                    capture.count + peer.count > DRIVER_CHECK_OUTPUT_BYTES
                    or attempt_bytes[0] > DRIVER_ATTEMPT_OUTPUT_BYTES
                ):
                    capture.complete = False
                    overflow.set()
                    return
    except OSError:
        capture.complete = False
        overflow.set()


def _wait_for_process(
    process: subprocess.Popen[bytes], overflow: threading.Event, started: float, timeout: float
) -> str:
    while process.poll() is None:
        if overflow.is_set():
            _terminate(process)
            return "output-overflow"
        if time.monotonic() - started >= timeout:
            _terminate(process)
            return "timeout"
        time.sleep(0.02)
    return "completed"


def _verified_executable(snapshot: TrustedCheckSnapshot, argv: tuple[str, ...]) -> str:
    digest = next(value for _, path, value in snapshot.executables if path == argv[0])
    try:
        current = _sha256(Path(argv[0]).read_bytes())
    except OSError as exc:
        raise VerificationError(f"trusted executable is unavailable at launch: {argv[0]}") from exc
    if current != digest:
        raise VerificationError(f"trusted executable hash changed: {argv[0]}")
    return digest


def _check_timeout(check: DoneCheck, deadline: float) -> float:
    remaining = deadline - time.monotonic()
    aggregate = isinstance(check, ArgvCheck) and check.argv[0] == "make" and check.argv[1] in _AGGREGATE_MAKE
    timeout = remaining if aggregate else min(float(TIMEOUT_DRIVER_CHECK), remaining)
    if timeout <= 0:
        raise VerificationError("verification budget exhausted before check launch")
    return timeout


def _completed_status(check: DoneCheck, status: str, exit_code: int | None, stdout: _StreamCapture) -> str:
    if status != "completed":
        return status
    expected_stdout_ok = check.expected_stdout is None or (
        stdout.expected_matches and stdout.count == len(check.expected_stdout)
    )
    return "passed" if exit_code == check.expected_exit and expected_stdout_ok else "failed"


def _run_check(
    snapshot: TrustedCheckSnapshot,
    check: DoneCheck,
    candidate: Path,
    artifacts: Path,
    *,
    deadline: float,
    attempt_bytes: list[int],
) -> CheckResult:
    argv = _check_argv(snapshot, check, candidate, artifacts)
    executable_digest = _verified_executable(snapshot, argv)
    timeout = _check_timeout(check, deadline)
    process = subprocess.Popen(
        argv,
        cwd=candidate,
        env=_minimal_environment(snapshot, candidate, check),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    stdout = _StreamCapture.create(check.expected_stdout)
    stderr = _StreamCapture.create()
    overflow = threading.Event()
    lock = threading.Lock()
    threads = [
        threading.Thread(
            target=_drain_stream, args=(process.stdout, stdout, stderr, attempt_bytes, overflow, lock), daemon=True
        ),
        threading.Thread(
            target=_drain_stream, args=(process.stderr, stderr, stdout, attempt_bytes, overflow, lock), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()
    status = _wait_for_process(process, overflow, started, timeout)
    for thread in threads:
        thread.join(timeout=TIMEOUT_DRIVER_OUTPUT_DRAIN)
    if overflow.is_set() and status == "completed":
        status = "output-overflow"
    if any(thread.is_alive() for thread in threads):
        status = "stream-error"
        stdout.complete = False
        stderr.complete = False
    _terminate(process)
    exit_code = process.poll()
    status = _completed_status(check, status, exit_code, stdout)
    return CheckResult(
        check.id,
        status,
        exit_code,
        round((time.monotonic() - started) * 1000),
        (check.checker, *check.args) if isinstance(check, CheckerCheck) else check.argv,
        executable_digest,
        stdout.evidence(),
        stderr.evidence(),
        stdout.tail_value(),
        stderr.tail_value(),
    )


def _candidate_clean(candidate: Path, head_sha: str) -> bool:
    if _git(candidate, "rev-parse", "HEAD").stdout.strip().lower() != head_sha:
        return False
    return not _git(candidate, "status", "--porcelain", "--untracked-files=all").stdout


def _evidence_record(
    snapshot: TrustedCheckSnapshot,
    identity: HeadIdentity | None,
    checks: tuple[CheckResult, ...],
    *,
    started_at: str,
    cleanup_status: str,
    decision: Literal["passed", "failed", "incomplete"],
    phase: Literal["checks-complete", "terminal", "failure"],
    final_head: str | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        attempt_id=snapshot.attempt_id,
        repository_id=identity.repository_id if identity else 0,
        issue_number=snapshot.issue_number,
        pr_number=identity.pr_number if identity else None,
        attempt=snapshot.attempt,
        base_sha=snapshot.base_sha,
        initial_head_sha=identity.head_sha if identity else None,
        final_head_sha=final_head,
        work_order_sha256=snapshot.work_order_sha256,
        contract_sha256=snapshot.contract_sha256,
        checker_sha256={checker.path: checker.sha256 for checker in snapshot.checkers},
        started_at=started_at,
        completed_at=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        checks=tuple(
            CheckEvidence(
                c.check_id,
                c.status,
                c.exit_code,
                c.duration_ms,
                c.argv,
                c.executable_sha256,
                c.stdout,
                c.stderr,
            )
            for c in checks
        ),
        cleanup_status=cleanup_status,
        decision=decision,
        phase=phase,
    )


def _execute_verification(
    snapshot: TrustedCheckSnapshot,
    repo_root: Path,
    evidence_root: Path,
    verifier_root: Path,
    deadline: float,
    started_at: str,
    progress: _VerificationProgress,
) -> None:
    if time.monotonic() >= deadline:
        raise VerificationError("verification budget exhausted before setup")
    progress.identity = _head(get_pr_identity(snapshot.repository, snapshot.branch))
    _validate_identity(snapshot, progress.identity)
    _git(repo_root, "fetch", "origin", progress.identity.head_sha)
    if _authority_manifest(repo_root, progress.identity.head_sha) != snapshot.authority:
        raise VerificationError("authority_changed")
    progress.worktree = verifier_root / f"head-{snapshot.attempt_id}"
    verifier_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    verifier_root.chmod(0o700)
    progress.artifacts.mkdir(mode=0o700, parents=True)
    _git(repo_root, "worktree", "add", "--detach", str(progress.worktree), progress.identity.head_sha)
    attempt_bytes = [0]
    for check in snapshot.contract.checks:
        result = _run_check(
            snapshot, check, progress.worktree, progress.artifacts, deadline=deadline, attempt_bytes=attempt_bytes
        )
        progress.checks.append(result)
        if result.status != "passed":
            raise VerificationError(f"completion check {check.id!r} {result.status}")
        if not _candidate_clean(progress.worktree, progress.identity.head_sha):
            raise VerificationError(f"completion check {check.id!r} changed the submitted tree")
    progress.evidence_path = write_evidence(
        evidence_root,
        _evidence_record(
            snapshot,
            progress.identity,
            tuple(progress.checks),
            started_at=started_at,
            cleanup_status="pending",
            decision="incomplete",
            phase="checks-complete",
        ),
    )


def _bounded_rmtree(path: Path, timeout: float) -> bool:
    failed = threading.Event()

    def remove() -> None:
        try:
            shutil.rmtree(path, ignore_errors=False)
        except FileNotFoundError:
            pass
        except OSError:
            failed.set()

    thread = threading.Thread(target=remove, daemon=True)
    thread.start()
    thread.join(timeout=max(0.0, timeout))
    return not thread.is_alive() and not failed.is_set()


def _cleanup_verifier(repo_root: Path, progress: _VerificationProgress, deadline: float) -> str:
    errors: list[str] = []
    if progress.worktree is not None:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VerificationError("cleanup deadline exhausted")
            cleanup_result = _git(
                repo_root,
                "worktree",
                "remove",
                "--force",
                str(progress.worktree),
                check=False,
                timeout=min(float(TIMEOUT_DRIVER_GIT), remaining),
            )
        except VerificationError:
            errors.append("worktree-remove")
        else:
            if cleanup_result.returncode != 0:
                errors.append("worktree-remove")
    remaining = min(float(TIMEOUT_DRIVER_OUTPUT_DRAIN), deadline - time.monotonic())
    if not _bounded_rmtree(progress.artifacts, remaining):
        errors.append("artifact-remove")
    return "passed" if not errors else ",".join(errors)


def _attempt_verification(
    snapshot: TrustedCheckSnapshot,
    repo_root: Path,
    evidence_root: Path,
    verifier_root: Path,
    deadline: float,
    started_at: str,
    progress: _VerificationProgress,
) -> tuple[bool, str, AuthError | None]:
    try:
        _execute_verification(snapshot, repo_root, evidence_root, verifier_root, deadline, started_at, progress)
        return True, "", None
    except AuthError as exc:
        return False, str(exc), exc
    except (VerificationError, GitHubLookupError, OSError, subprocess.SubprocessError) as exc:
        return False, str(exc), None


def _apply_cleanup_result(
    repo_root: Path,
    progress: _VerificationProgress,
    finalization_deadline: float,
    passed: bool,
    reason: str,
) -> tuple[bool, str, str]:
    cleanup_status = _cleanup_verifier(repo_root, progress, finalization_deadline)
    if cleanup_status != "passed":
        return False, reason or f"verifier cleanup failed: {cleanup_status}", cleanup_status
    if time.monotonic() >= finalization_deadline:
        return False, reason or "driver run budget exhausted during verifier cleanup", cleanup_status
    return passed, reason, cleanup_status


def _final_identity_result(
    snapshot: TrustedCheckSnapshot,
    progress: _VerificationProgress,
    finalization_deadline: float,
    passed: bool,
    reason: str,
    auth_error: AuthError | None,
) -> tuple[bool, str, AuthError | None, str | None]:
    if progress.identity is None or auth_error is not None:
        return passed, reason, auth_error, None
    if time.monotonic() >= finalization_deadline:
        return False, reason or "driver run budget exhausted before final identity check", None, None
    try:
        final_identity = _head(
            get_pr_identity(
                snapshot.repository,
                snapshot.branch,
                timeout=max(0.001, finalization_deadline - time.monotonic()),
            )
        )
        _validate_identity(snapshot, final_identity)
    except AuthError as exc:
        return False, reason or str(exc), exc, None
    except (VerificationError, GitHubLookupError, OSError, subprocess.SubprocessError) as exc:
        return False, reason or str(exc), None, None
    if time.monotonic() >= finalization_deadline:
        return False, reason or "driver run budget exhausted during final identity check", None, final_identity.head_sha
    if final_identity != progress.identity:
        return False, reason or "pull request identity moved during verification", None, final_identity.head_sha
    return passed, reason, None, final_identity.head_sha


def _write_terminal_evidence(
    snapshot: TrustedCheckSnapshot,
    progress: _VerificationProgress,
    evidence_root: Path,
    started_at: str,
    cleanup_status: str,
    passed: bool,
    reason: str,
    final_head: str | None,
    finalization_deadline: float,
) -> tuple[bool, str, BaseException | None]:
    try:
        progress.evidence_path = write_evidence(
            evidence_root,
            _evidence_record(
                snapshot,
                progress.identity,
                tuple(progress.checks),
                started_at=started_at,
                cleanup_status=cleanup_status,
                decision="passed" if passed else "failed",
                phase="terminal" if progress.identity is not None else "failure",
                final_head=final_head,
            ),
        )
    except (VerificationError, GitHubLookupError, OSError, subprocess.SubprocessError) as exc:
        return False, reason or f"terminal evidence failed: {exc}", exc
    if passed and time.monotonic() >= finalization_deadline:
        passed = False
        reason = reason or "driver run budget exhausted during terminal evidence"
        try:
            progress.evidence_path = write_evidence(
                evidence_root,
                _evidence_record(
                    snapshot,
                    progress.identity,
                    tuple(progress.checks),
                    started_at=started_at,
                    cleanup_status=cleanup_status,
                    decision="failed",
                    phase="terminal",
                    final_head=final_head,
                ),
            )
        except (VerificationError, GitHubLookupError, OSError, subprocess.SubprocessError) as exc:
            return False, reason, exc
    return passed, reason, None


def verify_submitted_head(
    snapshot: TrustedCheckSnapshot,
    repo_root: Path,
    *,
    evidence_root: Path,
    verifier_root: Path,
    run_started_monotonic: float,
) -> VerificationResult:
    """Verify the sole open pull request head and persist terminal evidence."""
    started_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline = run_started_monotonic + TIMEOUT_DRIVER_RUN - TIMEOUT_DRIVER_FINALIZE_RESERVE
    finalization_deadline = run_started_monotonic + TIMEOUT_DRIVER_RUN
    progress = _VerificationProgress(None, [], None, verifier_root / f"artifacts-{snapshot.attempt_id}", None)
    passed = False
    reason = ""
    auth_error: AuthError | None = None
    cleanup_status = "not-started"
    try:
        passed, reason, auth_error = _attempt_verification(
            snapshot, repo_root, evidence_root, verifier_root, deadline, started_at, progress
        )
    finally:
        passed, reason, cleanup_status = _apply_cleanup_result(
            repo_root, progress, finalization_deadline, passed, reason
        )
    passed, reason, auth_error, final_head = _final_identity_result(
        snapshot, progress, finalization_deadline, passed, reason, auth_error
    )
    passed, reason, terminal_error = _write_terminal_evidence(
        snapshot,
        progress,
        evidence_root,
        started_at,
        cleanup_status,
        passed,
        reason,
        final_head,
        finalization_deadline,
    )
    if auth_error is not None:
        if terminal_error is not None:
            raise auth_error from terminal_error
        raise auth_error
    return VerificationResult(
        passed, reason, progress.identity, tuple(progress.checks), cleanup_status, progress.evidence_path
    )
