"""Real child processes exercise bounded capture and resource enforcement."""

import json
import resource
import subprocess
import sys
from pathlib import Path
from unittest.mock import call, patch

import pytest

from scripts import process_supervision, resource_limited_exec

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("extra", [0, 1, 4096])
def test_capture_enforces_each_stream_limit(tmp_path: Path, stream: str, extra: int) -> None:
    """Accept the exact cap but never accept an over-limit or truncated stream."""
    command = [sys.executable, "-I", "-c", f"import sys; sys.{stream}.write('x' * {4096 + extra})"]
    if extra:
        with pytest.raises(process_supervision.ProcessError, match="output limit"):
            process_supervision.run_bounded(command, cwd=tmp_path, timeout_seconds=5, output_limit=4096)
    else:
        result = process_supervision.run_bounded(command, cwd=tmp_path, timeout_seconds=5, output_limit=4096)
        assert result.returncode == 0
        assert getattr(result, stream) == "x" * 4096


def test_child_receives_hard_resource_limits(tmp_path: Path) -> None:
    """The real child observes the configured hard and soft resource ceilings."""
    result = process_supervision.run_bounded(
        [
            sys.executable,
            "-I",
            "-c",
            "import json,resource; print(json.dumps([resource.getrlimit(k) "
            "for k in (resource.RLIMIT_AS,resource.RLIMIT_FSIZE,resource.RLIMIT_CORE)]))",
        ],
        cwd=tmp_path,
        timeout_seconds=5,
        output_limit=4096,
    )
    assert result.returncode == 0
    memory, file_size, core = json.loads(result.stdout)
    assert 0 < memory[0] == memory[1] <= 4 * 1024**3
    assert 0 < file_size[0] == file_size[1] <= 4097
    assert core == [0, 0]


def test_timeout_is_nonpassing(tmp_path: Path) -> None:
    """A sleeping child cannot outlive the wall-clock budget."""
    with pytest.raises(process_supervision.ProcessError, match="timed out"):
        process_supervision.run_bounded(
            [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=0.2,
            output_limit=4096,
        )


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_invalid_utf8_is_controlled(tmp_path: Path, stream: str) -> None:
    """Malformed output is a bounded execution failure, not an uncaught decoder error."""
    with pytest.raises(process_supervision.ProcessError, match="UTF-8"):
        process_supervision.run_bounded(
            [sys.executable, "-I", "-c", f"import sys; sys.{stream}.buffer.write(b'\\xff')"],
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=4096,
        )


def test_address_space_limit_rejects_allocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real allocation beyond the inherited address-space ceiling fails."""
    monkeypatch.setattr(process_supervision, "ADDRESS_SPACE_LIMIT", 128 * 1024**2)
    result = process_supervision.run_bounded(
        [sys.executable, "-I", "-c", "import mmap; mmap.mmap(-1, 256 * 1024**2)"],
        cwd=tmp_path,
        timeout_seconds=5,
        output_limit=4096,
    )
    assert result.returncode != 0
    assert "OSError" in result.stderr


def test_timeout_kills_term_ignoring_grandchild(tmp_path: Path) -> None:
    """The whole group is killed even when a descendant ignores graceful termination."""
    pid_file = tmp_path / "child.pid"
    grandchild = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); time.sleep(30)"
    )
    leader = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-I', '-c', {grandchild!r}]); time.sleep(30)"
    )
    with pytest.raises(process_supervision.ProcessError, match="timed out"):
        process_supervision.run_bounded(
            [sys.executable, "-I", "-c", leader], cwd=tmp_path, timeout_seconds=1, output_limit=4096
        )
    pid = int(pid_file.read_text(encoding="utf-8"))
    observation = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False, timeout=5
    )
    assert observation.returncode in (0, 1)
    assert not observation.stdout.strip() or observation.stdout.lstrip().startswith("Z")


def test_interruption_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An interruption after spawn still executes the shared cleanup path."""
    original_wait = subprocess.Popen.wait
    interrupted: list[subprocess.Popen[bytes]] = []

    def interrupt_once(process: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        if not interrupted:
            interrupted.append(process)
            raise KeyboardInterrupt("test interruption")
        return original_wait(process, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupt_once)
    with pytest.raises(KeyboardInterrupt, match="test interruption"):
        process_supervision.run_bounded(
            [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=4096,
        )
    assert len(interrupted) == 1
    assert interrupted[0].poll() is not None


def test_failed_limit_setup_never_executes_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure to establish any required limit prevents tool execution."""
    monkeypatch.setattr(sys, "argv", ["launcher", "1024", "4096", sys.executable])
    with (
        patch.object(resource, "setrlimit", side_effect=OSError("limit unavailable")),
        patch.object(resource_limited_exec.os, "execv") as execute,
    ):
        result = resource_limited_exec.main()
    assert result == 1
    execute.assert_not_called()


@pytest.mark.parametrize("inherited", [(512, 768), (512, resource.RLIM_INFINITY), (0, 768)])
def test_launcher_preserves_stricter_limits(monkeypatch: pytest.MonkeyPatch, inherited: tuple[int, int]) -> None:
    """Existing finite ceilings are never raised while applying the tool policy."""
    monkeypatch.setattr(sys, "argv", ["launcher", "1024", "4096", sys.executable])
    with (
        patch.object(resource, "getrlimit", return_value=inherited),
        patch.object(resource, "setrlimit") as set_limit,
        patch.object(resource_limited_exec.os, "execv") as execute,
    ):
        result = resource_limited_exec.main()
    assert result == 1  # A mocked exec returns; a real successful exec never does.
    ceiling = inherited[0]
    assert set_limit.call_args_list == [
        call(resource.RLIMIT_AS, (ceiling, ceiling)),
        call(resource.RLIMIT_FSIZE, (ceiling, ceiling)),
        call(resource.RLIMIT_CORE, (0, 0)),
    ]
    execute.assert_called_once_with(sys.executable, [sys.executable])
