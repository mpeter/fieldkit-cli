"""Regression coverage for the isolated AgentReady worktree wrapper."""

import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).parent.parent / "scripts" / "agentready_assess.py"
_GIT_TEST_TIMEOUT_SECONDS = 20


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("agentready_assess", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(command: list[str], returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode)


def _run_main(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType) -> int:
    monkeypatch.setattr(sys, "argv", ["agentready_assess.py", "agentready==2.49.0"])
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert isinstance(exc_info.value.code, int)
    return exc_info.value.code


def test_main_runs_assessment_in_temporary_worktree_and_removes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    calls: list[tuple[list[str], int, Path | None]] = []

    def fake_run(command: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append((command, timeout, cwd))
        return _completed(command, 0)

    monkeypatch.setattr(module, "_run", fake_run)

    exit_code = _run_main(monkeypatch, module)

    assert exit_code == 0
    assert [calls[0][0][4], calls[1][0][2], calls[2][0][4]] == ["add", "assess", "remove"]
    assert calls[1][2] == Path(calls[0][0][6])
    assert calls[2][2] is None


def test_main_removes_worktree_when_assessment_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        del timeout, cwd
        calls.append(command)
        return _completed(command, 7 if command[0] == "uvx" else 0)

    monkeypatch.setattr(module, "_run", fake_run)

    exit_code = _run_main(monkeypatch, module)

    assert exit_code == 7
    assert calls[-1][4] == "remove"


def test_main_stops_without_assessment_when_worktree_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        del timeout, cwd
        calls.append(command)
        return _completed(command, 4)

    monkeypatch.setattr(module, "_run", fake_run)

    exit_code = _run_main(monkeypatch, module)

    assert exit_code == 4
    assert len(calls) == 2
    assert calls[-1][4] == "remove"


def test_main_reports_worktree_creation_timeout_and_attempts_cleanup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        del cwd
        calls.append(command)
        if command[4] == "add":
            raise subprocess.TimeoutExpired(command, timeout)
        return _completed(command, 0)

    monkeypatch.setattr(module, "_run", fake_run)

    exit_code = _run_main(monkeypatch, module)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert calls[-1][4] == "remove"
    assert "failed to create clean worktree" in captured.err


def test_main_reports_cleanup_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_module()

    def fake_run(command: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        del timeout, cwd
        if command[0] == "uvx":
            return _completed(command, 7)
        return _completed(command, 9 if command[4] == "remove" else 0)

    monkeypatch.setattr(module, "_run", fake_run)

    exit_code = _run_main(monkeypatch, module)

    captured = capsys.readouterr()
    assert exit_code == 9
    assert "failed to remove clean worktree" in captured.err
    assert "assessment exit 7" in captured.err


def test_main_removes_worktree_when_assessment_times_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        del cwd
        calls.append(command)
        if command[0] == "uvx":
            raise subprocess.TimeoutExpired(command, timeout)
        return _completed(command, 0)

    monkeypatch.setattr(module, "_run", fake_run)

    exit_code = _run_main(monkeypatch, module)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert calls[-1][4] == "remove"
    assert "assessment failed" in captured.err


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TEST_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()


def test_main_preserves_caller_branch_and_removes_real_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "test: initialize repository")
    _git(repository, "switch", "-c", "feature")
    initial_head = _git(repository, "rev-parse", "HEAD")

    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    cwd_record = tmp_path / "assessor-cwd.txt"
    fake_uvx = executable_dir / "uvx"
    fake_uvx.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['AGENTREADY_CWD_RECORD']).write_text(os.getcwd(), encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_uvx.chmod(0o755)

    module = _load_module()
    monkeypatch.setattr(module, "_REPO_ROOT", repository)
    monkeypatch.setattr(module, "_CONFIG", repository / "agentready.yaml")
    monkeypatch.setattr(module, "_OUTPUT_DIR", repository / "output")
    monkeypatch.setenv("PATH", f"{executable_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTREADY_CWD_RECORD", str(cwd_record))

    exit_code = _run_main(monkeypatch, module)

    assert exit_code == 0
    assert _git(repository, "branch", "--show-current") == "feature"
    assert _git(repository, "rev-parse", "HEAD") == initial_head
    assert Path(cwd_record.read_text(encoding="utf-8")).name == "tree"
    assert "agentready-clean-" not in _git(repository, "worktree", "list")
