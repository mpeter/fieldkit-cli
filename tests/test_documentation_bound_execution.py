"""Verification execution binds fixed inputs before any child runs."""

import hashlib
import os
import shutil
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from scripts import documentation_candidate_execution as execution
from scripts import documentation_command_runner as command_runner
from scripts.documentation_commands import EXAMPLE_COMMANDS

pytestmark = pytest.mark.unit


@pytest.mark.integration
@pytest.mark.parametrize("already_reaped", [False, True])
def test_cleanup_never_signals_a_reaped_process_group(monkeypatch: pytest.MonkeyPatch, already_reaped: bool) -> None:
    process = subprocess.Popen(
        (sys.executable, "-I", "-c", "pass" if already_reaped else "import time; time.sleep(60)"),
        start_new_session=True,
    )
    if already_reaped:
        assert process.wait(timeout=10) == 0
    signals: list[int] = []
    original_killpg = os.killpg

    def record_signal(group: int, signal: int) -> None:
        signals.append(group)
        if already_reaped:
            pytest.fail("cleanup signaled a reaped and potentially reused group")
        original_killpg(group, signal)

    monkeypatch.setattr(command_runner.os, "killpg", record_signal)
    try:
        result = command_runner._kill_and_reap(process)
        assert result is True
        assert signals == ([] if already_reaped else [process.pid])
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            original_killpg(process.pid, 9)
            process.wait(timeout=10)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), cwd=repository, check=True, capture_output=True, text=True, timeout=10
    ).stdout.strip()


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    repository = tmp_path / "candidate"
    (repository / "docs").mkdir(parents=True)
    (repository / "docs/documentation-contract.json").write_text("{}\n", encoding="utf-8")
    (repository / "source.md").write_text("committed\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate")
    return repository


def _result(argv: tuple[str, ...]) -> dict[str, object]:
    return {"argv": argv, "exit_code": 0, "stdout": "ok", "stderr": ""}


@pytest.mark.parametrize("mutate_source", [False, True])
def test_clean_execution_uses_snapshot_and_retains_original_identity(
    candidate: Path, monkeypatch: pytest.MonkeyPatch, mutate_source: bool
) -> None:
    revision = _git(candidate, "rev-parse", "HEAD")
    command = ("owner", "--check")

    def plan(root: Path) -> execution.CommandPlan:
        assert root != candidate
        assert (root / "source.md").read_text(encoding="utf-8") == "committed\n"
        assert not (root / ".git").exists()
        return (command,)

    def worker(
        root: Path,
        runtime_root: Path,
        source_revision: str,
        commands: execution.CommandPlan,
        uv: execution.TrustedUv,
        python: execution.TrustedInterpreter,
        runner: execution.SupervisedRunner,
        **_options: object,
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        assert root != candidate
        assert runtime_root.parent == root.parent
        assert source_revision == revision
        assert commands == (command,)
        assert callable(runner)
        assert uv.sha256 == "a" * 64
        assert python.sha256 == "b" * 64
        if mutate_source:
            (candidate / "source.md").write_text("changed during execution\n", encoding="utf-8")
        return [_result(command)], ("unproven",)

    monkeypatch.setattr(execution, "_run_bound_plan", worker)
    monkeypatch.setattr(
        execution,
        "_trusted_uv",
        lambda _digest: nullcontext(execution.TrustedUv(-1, "a" * 64, 1)),
    )
    monkeypatch.setattr(
        execution,
        "_trusted_interpreter",
        lambda: nullcontext(execution.TrustedInterpreter(-1, "b" * 64, 2)),
    )

    results, pending, binding = execution.execute_bound_candidate(
        candidate, plan, lambda *_: ([], ()), supervised_runner=lambda _: ([], ()), trusted_uv_sha256="a" * 64
    )

    assert results == [_result(command)]
    assert pending == ("unproven",)
    assert binding["source_revision"] == revision
    assert binding["immutable_snapshot"] is True
    assert binding["source_unchanged"] is not mutate_source
    assert binding["clean"] is not mutate_source
    assert binding["runtime_builder"] == {
        "name": "uv",
        "sha256": "a" * 64,
        "size": 1,
        "python_sha256": "b" * 64,
        "python_size": 2,
    }
    assert binding["runtime_tool_approval"] == "pending"


def test_initially_dirty_execution_cannot_become_release_evidence(candidate: Path) -> None:
    (candidate / "source.md").write_text("dirty\n", encoding="utf-8")

    def diagnostic(root: Path, commands: execution.CommandPlan) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        assert root == candidate
        assert commands == ()
        (candidate / "source.md").write_text("committed\n", encoding="utf-8")
        return [], ()

    results, pending, binding = execution.execute_bound_candidate(candidate, lambda _root: (), diagnostic)

    assert results == []
    assert pending == ()
    assert binding["clean"] is False
    assert binding["immutable_snapshot"] is False
    assert binding["runtime_environment_checked"] is False
    assert binding["source_unchanged"] is False
    assert binding["evidence_kind"] == "diagnostic"


def test_plan_restoring_dirty_source_cannot_promote_diagnostic(candidate: Path) -> None:
    (candidate / "source.md").write_text("dirty\n", encoding="utf-8")

    def plan(root: Path) -> execution.CommandPlan:
        (root / "source.md").write_text("committed\n", encoding="utf-8")
        return ()

    results, pending, binding = execution.execute_bound_candidate(candidate, plan, lambda *_: ([], ()))

    assert results == []
    assert pending == ()
    assert binding["evidence_kind"] == "diagnostic"
    assert binding["clean"] is False
    assert binding["source_unchanged"] is False


def test_clean_execution_requires_trusted_supervision(candidate: Path) -> None:
    with pytest.raises(ValueError, match="trusted owner supervision"):
        execution.execute_bound_candidate(candidate, lambda _: (), lambda *_: ([], ()), trusted_uv_sha256="a" * 64)


@pytest.mark.parametrize("diagnostic_argv", [False, True])
def test_supervised_result_must_match_frozen_snapshot_plan(
    candidate: Path, monkeypatch: pytest.MonkeyPatch, diagnostic_argv: bool
) -> None:
    command = EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    returned = (*command, "--diagnostic-dirty") if diagnostic_argv else ("other",)
    monkeypatch.setattr(execution, "_run_bound_plan", lambda *_, **_options: ([_result(returned)], ()))
    monkeypatch.setattr(
        execution,
        "_trusted_uv",
        lambda _digest: nullcontext(execution.TrustedUv(-1, "a" * 64, 1)),
    )
    monkeypatch.setattr(
        execution,
        "_trusted_interpreter",
        lambda: nullcontext(execution.TrustedInterpreter(-1, "b" * 64, 2)),
    )

    with pytest.raises(ValueError, match="frozen execution plan"):
        execution.execute_bound_candidate(
            candidate,
            lambda _root: (command,),
            lambda *_: ([], ()),
            supervised_runner=lambda _: ([], ()),
            trusted_uv_sha256="a" * 64,
        )


@pytest.mark.parametrize("variant", ["exact", "canonical", "near", "extra", "missing", "reordered", "other-owner"])
def test_dirty_execution_requires_exact_diagnostic_plan(candidate: Path, variant: str) -> None:
    (candidate / "source.md").write_text("dirty\n", encoding="utf-8")
    command = EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    diagnostic = (*command, "--diagnostic-dirty")
    owner = ("owner", "--check")
    variants = {
        "exact": (diagnostic, owner),
        "canonical": (command, owner),
        "near": ((*command, "--diagnostic-dirty=true"), owner),
        "extra": ((*diagnostic, "--extra"), owner),
        "missing": (diagnostic,),
        "reordered": (owner, diagnostic),
        "other-owner": (diagnostic, (*owner, "--diagnostic-dirty")),
    }

    def diagnostic_runner(
        _root: Path, commands: execution.CommandPlan
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        assert commands == (command, owner)
        return [_result(argv) for argv in variants[variant]], ()

    if variant != "exact":
        with pytest.raises(ValueError, match="frozen execution plan"):
            execution.execute_bound_candidate(candidate, lambda _root: (command, owner), diagnostic_runner)
        return
    results, pending, binding = execution.execute_bound_candidate(
        candidate, lambda _root: (command, owner), diagnostic_runner
    )
    assert results == [_result(diagnostic), _result(owner)]
    assert pending == ()
    assert binding["evidence_kind"] == "diagnostic"
    assert binding["clean"] is False
    assert binding["immutable_snapshot"] is False


def test_private_runtime_is_built_from_locked_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = tmp_path / "snapshot"
    runtime = tmp_path / "runtime"
    snapshot.mkdir()
    (runtime / ".venv/bin").mkdir(parents=True)
    uv_cache = tmp_path / "uv-cache"
    uv_cache.mkdir()
    trusted_python = tmp_path / "trusted-python"
    trusted_python.write_bytes(b"trusted interpreter")
    (runtime / ".venv/bin/python").symlink_to("/run/python")
    observed: dict[str, object] = {}

    class Process:
        pid = 12345

        def wait(self, *, timeout: int | float) -> int:
            observed["timeout"] = timeout
            return 0

    def popen(argv: tuple[str, ...], **options: object) -> Process:
        observed["argv"] = argv
        observed.update(options)
        return Process()

    monkeypatch.setattr(execution.subprocess, "Popen", popen)
    monkeypatch.setattr(execution, "_kill_and_reap", lambda _process: True)
    monkeypatch.setattr(execution, "_uv_cache_root", lambda: uv_cache)
    python_descriptor = os.open(trusted_python, os.O_RDONLY | os.O_CLOEXEC)
    python_status = os.fstat(python_descriptor)
    python_identity = execution.TrustedInterpreter(
        python_descriptor,
        hashlib.sha256(trusted_python.read_bytes()).hexdigest(),
        python_status.st_size,
    )

    try:
        with execution._build_runtime(
            snapshot,
            runtime,
            execution.TrustedUv(7, "a" * 64, 1),
            python_identity,
        ) as interpreter:
            assert interpreter.sha256 == python_identity.sha256
            assert (runtime / ".venv/bin/python").readlink() == Path("/run/python")
    finally:
        os.close(python_descriptor)

    argv = observed["argv"]
    assert isinstance(argv, tuple)
    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in argv
    assert "--disable-userns" in argv
    assert "--tmp-overlay" in argv
    assert argv[-18:] == (
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
        "/workspace",
        "--python",
        "/run/python",
        "--no-managed-python",
        "--no-python-downloads",
        "--quiet",
    )
    environment = observed["env"]
    assert isinstance(environment, dict)
    assert environment["UV_PROJECT_ENVIRONMENT"] == "/runtime/.venv"
    assert environment["UV_CACHE_DIR"] == "/uv-cache"
    assert environment["UV_OFFLINE"] == "1"
    assert "PATH" not in environment
    pass_fds = observed["pass_fds"]
    assert isinstance(pass_fds, tuple)
    assert pass_fds[:2] == (7, python_descriptor)
    assert len(pass_fds) == 6
    assert observed["start_new_session"] is True


def test_private_runtime_never_executes_python_from_ambient_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv_path = Path(shutil.which("uv") or pytest.fail("uv is unavailable"))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "ambient-python-ran"
    for name in ("python", "python3", "python3.11"):
        fake = fake_bin / name
        fake.write_text(f"#!/bin/sh\nprintf ran > {marker}\nexit 77\n", encoding="utf-8")
        fake.chmod(0o700)
    snapshot = tmp_path / "snapshot"
    runtime = tmp_path / "runtime"
    snapshot.mkdir()
    runtime.mkdir()
    (snapshot / "pyproject.toml").write_text(
        "[project]\nname='runtime-probe'\nversion='0'\nrequires-python='>=3.11'\n"
        "[dependency-groups]\nrelease-build=[]\n",
        encoding="utf-8",
    )
    subprocess.run(
        (str(uv_path), "lock", "--offline", "--project", str(snapshot)),
        check=True,
        capture_output=True,
        timeout=30,
    )
    monkeypatch.setenv("PATH", str(fake_bin))
    uv_descriptor = os.open(uv_path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        uv_status = os.fstat(uv_descriptor)
        uv = execution.TrustedUv(
            uv_descriptor,
            hashlib.sha256(uv_path.read_bytes()).hexdigest(),
            uv_status.st_size,
        )
        with (
            execution._trusted_interpreter() as trusted_interpreter,
            execution._build_runtime(snapshot, runtime, uv, trusted_interpreter) as interpreter,
        ):
            assert interpreter.sha256 == trusted_interpreter.sha256
    finally:
        os.close(uv_descriptor)

    assert not marker.exists()


def test_runtime_build_timeout_kills_descendants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_uv = tmp_path / "uv"
    runtime = tmp_path / "runtime"
    marker = runtime / "delayed-child"
    fake_uv.write_text(
        "#!/usr/bin/python3\n"
        "import subprocess,time\n"
        "subprocess.Popen(['/usr/bin/python3','-c',\"import pathlib,time;time.sleep(0.4);pathlib.Path('/runtime/delayed-child').write_text('ran')\"])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    runtime.mkdir()
    uv_descriptor = os.open(fake_uv, os.O_RDONLY | os.O_CLOEXEC)
    monkeypatch.setattr(execution, "_RUNTIME_TIMEOUT_SECONDS", 0.1)
    try:
        status = os.fstat(uv_descriptor)
        uv = execution.TrustedUv(uv_descriptor, "a" * 64, status.st_size)
        with (
            execution._trusted_interpreter() as trusted_interpreter,
            pytest.raises(ValueError, match="could not be built"),
            execution._build_runtime(snapshot, runtime, uv, trusted_interpreter),
        ):
            pass
    finally:
        os.close(uv_descriptor)

    time.sleep(0.6)
    assert not marker.exists()


@pytest.mark.parametrize("parent_action", ["time.sleep(10)", "raise SystemExit(0)", "raise SystemExit(9)"])
def test_runtime_build_contains_detached_descendants_on_every_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_action: str
) -> None:
    fake_uv = tmp_path / "uv"
    runtime = tmp_path / "runtime"
    marker = runtime / "detached-child"
    fake_uv.write_text(
        "#!/usr/bin/python3\n"
        "import subprocess,time\n"
        "subprocess.Popen(['/usr/bin/python3','-c',\"import pathlib,time;time.sleep(0.4);pathlib.Path('/runtime/detached-child').write_text('ran')\"], start_new_session=True)\n"
        f"{parent_action}\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    runtime.mkdir()
    uv_descriptor = os.open(fake_uv, os.O_RDONLY | os.O_CLOEXEC)
    monkeypatch.setattr(execution, "_RUNTIME_TIMEOUT_SECONDS", 0.1 if "sleep" in parent_action else 2)
    try:
        status = os.fstat(uv_descriptor)
        uv = execution.TrustedUv(uv_descriptor, "a" * 64, status.st_size)
        with (
            execution._trusted_interpreter() as trusted_interpreter,
            pytest.raises(ValueError),
            execution._build_runtime(snapshot, runtime, uv, trusted_interpreter),
        ):
            pass
    finally:
        os.close(uv_descriptor)

    time.sleep(0.6)
    assert not marker.exists()


def test_trusted_uv_rejects_ambient_or_mismatched_invoker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_uv = tmp_path / "uv"
    fake_uv.write_bytes(b"not the approved executable")
    original_open = os.open
    monkeypatch.setattr(
        execution.os,
        "open",
        lambda _path, flags: original_open(fake_uv, flags),
    )

    with pytest.raises(ValueError, match="does not match"), execution._trusted_uv("a" * 64):
        pass

    approved = hashlib.sha256(fake_uv.read_bytes()).hexdigest()
    with execution._trusted_uv(approved) as identity:
        assert identity.sha256 == approved
        assert identity.size == len(fake_uv.read_bytes())


@pytest.mark.parametrize("relative", ["report.json", "nested/report.json"])
def test_report_inside_candidate_is_rejected(candidate: Path, relative: str) -> None:
    output = candidate / relative
    output.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="outside the candidate"), execution.external_report(candidate, output):
        pass


def test_report_symlink_parent_into_candidate_is_rejected(candidate: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked").symlink_to(candidate, target_is_directory=True)

    with (
        pytest.raises(ValueError, match="outside the candidate"),
        execution.external_report(candidate, outside / "linked/report.json"),
    ):
        pass


def test_same_filesystem_report_is_rejected_before_it_can_be_relocated(candidate: Path, tmp_path: Path) -> None:
    outside = tmp_path / "reports"
    outside.mkdir()

    with (
        pytest.raises(ValueError, match="separate filesystem"),
        execution.external_report(candidate, outside / "report.json"),
    ):
        pass


def test_stdout_report_is_bounded_and_has_no_filesystem_sink(
    candidate: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with execution.external_report(candidate, "-") as report:
        assert report is not None
        report.write('{"status":"pending"}\n')

    assert capsys.readouterr().out == '{"status":"pending"}\n'


def test_stdout_report_rejects_oversized_content(candidate: Path) -> None:
    with execution.external_report(candidate, "-") as report:
        assert report is not None
        with pytest.raises(ValueError, match="size bound"):
            report.write("x" * (execution._REPORT_LIMIT_BYTES + 1))
