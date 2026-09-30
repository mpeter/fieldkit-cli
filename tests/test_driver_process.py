"""Bounded driver support-process execution."""

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.util.bounded_process import (
    BoundedProcessError,
    process_exited_unreaped,
    run_bounded_process,
    run_bounded_process_bytes,
    terminate_process_group,
)

pytestmark = pytest.mark.unit


def _mock_pipe_process() -> MagicMock:
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    os.close(stdout_write)
    os.close(stderr_write)
    return MagicMock(
        stdout=os.fdopen(stdout_read, "rb"), stderr=os.fdopen(stderr_read, "rb"), pid=424242, returncode=None
    )


def test_bounded_process_retains_complete_small_output(tmp_path: Path) -> None:
    result = run_bounded_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('ok'); sys.stderr.write('note')"],
        timeout=5,
        stdout_limit=16,
        stderr_limit=16,
        cleanup_timeout=1,
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert result.stdout == "ok"
    assert result.stderr == "note"


def test_bounded_process_child_sees_eof_instead_of_parent_stdin(tmp_path: Path) -> None:
    sentinel = b"fictional-secret-stdin-sentinel"
    original_stdin = os.dup(0)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, sentinel)
        os.dup2(read_fd, 0)
        result = run_bounded_process_bytes(
            [sys.executable, "-I", "-c", "import os; data = os.read(0, 1024); os.write(1, data or b'EOF')"],
            timeout=5,
            stdout_limit=1024,
            stderr_limit=1024,
            cleanup_timeout=1,
            cwd=tmp_path,
        )
    finally:
        os.dup2(original_stdin, 0)
        os.close(original_stdin)
        os.close(read_fd)
        os.close(write_fd)

    assert result.returncode == 0
    assert result.stdout == b"EOF"
    assert result.stderr == b""
    assert sentinel not in result.stdout + result.stderr


def test_group_is_signalled_before_owned_leader_is_reaped() -> None:
    events: list[str] = []
    process = MagicMock(pid=424242, returncode=None)

    def waitid(*_args: object) -> object:
        events.append("waitid-exited")
        return object()

    def killpg(*_args: object) -> None:
        events.append("killpg")

    def wait(*, timeout: float) -> int:
        del timeout
        events.append("wait")
        process.returncode = 0
        return 0

    process.wait.side_effect = wait
    with (
        patch("fieldkit.util.bounded_process.os.waitid", side_effect=waitid),
        patch("fieldkit.util.bounded_process.os.killpg", side_effect=killpg),
    ):
        assert process_exited_unreaped(process) is True
        terminate_process_group(process, cleanup_timeout=1)

    assert events == ["waitid-exited", "killpg", "wait"]


def test_lost_child_ownership_never_signals_process_group() -> None:
    process = MagicMock(pid=424242)
    with (
        patch("fieldkit.util.bounded_process.os.waitid", side_effect=ChildProcessError),
        patch("fieldkit.util.bounded_process.os.killpg") as killpg,
        pytest.raises(BoundedProcessError, match="ownership was lost"),
    ):
        process_exited_unreaped(process)

    killpg.assert_not_called()


def test_bounded_process_terminates_on_output_overflow(tmp_path: Path) -> None:
    with pytest.raises(BoundedProcessError, match="output exceeded"):
        run_bounded_process(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 65536)"],
            timeout=5,
            stdout_limit=1024,
            stderr_limit=1024,
            cleanup_timeout=1,
            cwd=tmp_path,
        )


def test_bounded_process_diagnostics_do_not_include_argv_or_child_payload(tmp_path: Path) -> None:
    with pytest.raises(BoundedProcessError) as captured:
        run_bounded_process(
            [sys.executable, "-c", "import os; os.write(2, b'fictional-secret' * 1000)"],
            timeout=5,
            stdout_limit=1024,
            stderr_limit=32,
            cleanup_timeout=1,
            cwd=tmp_path,
        )

    assert "fictional-secret" not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_output_overflow_kills_same_group_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "late-marker"
    child = f"import time, pathlib; time.sleep(0.2); pathlib.Path({str(marker)!r}).write_text('late')"
    parent = (
        f"import os, subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}]); os.write(1, b'x' * 65536)"
    )

    with pytest.raises(BoundedProcessError, match="output exceeded"):
        run_bounded_process(
            [sys.executable, "-c", parent],
            timeout=5,
            stdout_limit=1024,
            stderr_limit=1024,
            cleanup_timeout=1,
            cwd=tmp_path,
        )
    time.sleep(0.3)

    assert not marker.exists()


def test_timeout_kills_same_group_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "late-timeout-marker"
    child = f"import time, pathlib; time.sleep(0.3); pathlib.Path({str(marker)!r}).write_text('late')"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"

    with pytest.raises(BoundedProcessError, match="timed out"):
        run_bounded_process(
            [sys.executable, "-c", parent],
            timeout=0.1,
            stdout_limit=1024,
            stderr_limit=1024,
            cleanup_timeout=1,
            cwd=tmp_path,
        )
    time.sleep(0.4)

    assert not marker.exists()


@pytest.mark.parametrize(
    ("timeout", "stdout_limit", "stderr_limit", "cleanup_timeout"),
    [
        (float("nan"), 1, 1, 1),
        (float("inf"), 1, 1, 1),
        (1, -1, 1, 1),
        (1, 1, -1, 1),
        (1, 1, 1, 0),
        (1, 1, 1, float("nan")),
        (1, 1, 1, float("inf")),
    ],
)
def test_invalid_bounds_fail_before_process_start(
    monkeypatch: pytest.MonkeyPatch,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    cleanup_timeout: float,
) -> None:
    popen = MagicMock()
    monkeypatch.setattr("fieldkit.util.bounded_process.subprocess.Popen", popen)

    with pytest.raises(ValueError, match=r"bounds|timeouts"):
        run_bounded_process(
            [sys.executable, "-c", "pass"],
            timeout=timeout,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            cleanup_timeout=cleanup_timeout,
        )

    popen.assert_not_called()


def test_bounded_process_passes_only_explicit_descriptor_authority() -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"lease")
    try:
        result = run_bounded_process(
            [sys.executable, "-I", "-c", f"import os; os.write(1, os.read({read_fd}, 5))"],
            timeout=5,
            stdout_limit=16,
            stderr_limit=16,
            cleanup_timeout=1,
            pass_fds=(read_fd,),
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert result.returncode == 0
    assert result.stdout == "lease"


def test_bounded_process_cleans_child_and_streams_on_interrupt() -> None:
    process = _mock_pipe_process()
    with (
        patch("fieldkit.util.bounded_process.subprocess.Popen", return_value=process),
        patch("fieldkit.util.bounded_process.process_exited_unreaped", side_effect=[KeyboardInterrupt, False]),
        patch("fieldkit.util.bounded_process.terminate_process_group") as terminate,
        pytest.raises(KeyboardInterrupt),
    ):
        run_bounded_process(
            [sys.executable, "-I", "-c", "pass"], timeout=5, stdout_limit=16, stderr_limit=16, cleanup_timeout=1
        )

    assert terminate.call_count == 1
    assert terminate.call_args.args == (process,)
    assert 0 < terminate.call_args.kwargs["cleanup_timeout"] <= 1
    assert process.stdout.closed
    assert process.stderr.closed


def test_bounded_process_never_signals_after_observed_ownership_loss() -> None:
    process = _mock_pipe_process()
    with (
        patch("fieldkit.util.bounded_process.subprocess.Popen", return_value=process),
        patch("fieldkit.util.bounded_process.os.waitid", side_effect=ChildProcessError),
        patch("fieldkit.util.bounded_process.os.killpg") as killpg,
        pytest.raises(BoundedProcessError, match="ownership was lost"),
    ):
        run_bounded_process(
            [sys.executable, "-I", "-c", "pass"], timeout=5, stdout_limit=16, stderr_limit=16, cleanup_timeout=1
        )

    killpg.assert_not_called()
    process.kill.assert_not_called()
    process.wait.assert_not_called()


def test_actual_child_is_reaped_and_pipe_owners_close_after_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.util import bounded_process

    original_observer = bounded_process.process_exited_unreaped
    observed: list[subprocess.Popen[bytes]] = []

    def interrupt_once(process: subprocess.Popen[bytes]) -> bool:
        observed.append(process)
        if len(observed) == 1:
            raise KeyboardInterrupt
        return original_observer(process)

    monkeypatch.setattr(bounded_process, "process_exited_unreaped", interrupt_once)

    with pytest.raises(KeyboardInterrupt):
        run_bounded_process(
            [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
            timeout=5,
            stdout_limit=16,
            stderr_limit=16,
            cleanup_timeout=1,
        )

    assert observed
    process = observed[0]
    assert process.returncode is not None
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)


def test_interrupt_cleanup_shares_one_deadline_across_child_and_drainers(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = 0.0

    class StalledDrainer:
        ident = 1

        def __init__(self, *, args: tuple[object, ...], **_kwargs: object) -> None:
            descriptor = args[0]
            assert isinstance(descriptor, int)
            self.descriptor: int | None = descriptor

        def start(self) -> None:
            pass

        def join(self, *, timeout: float) -> None:
            nonlocal elapsed
            elapsed += timeout
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None

        def is_alive(self) -> bool:
            return True

    def consume_child_cleanup(_process: object, *, cleanup_timeout: float) -> None:
        nonlocal elapsed
        elapsed += cleanup_timeout

    process = _mock_pipe_process()
    monkeypatch.setattr("fieldkit.util.bounded_process.threading.Thread", StalledDrainer)
    monkeypatch.setattr("fieldkit.util.bounded_process.time.monotonic", lambda: elapsed)
    monkeypatch.setattr("fieldkit.util.bounded_process.subprocess.Popen", MagicMock(return_value=process))
    monkeypatch.setattr(
        "fieldkit.util.bounded_process.process_exited_unreaped", MagicMock(side_effect=[KeyboardInterrupt, False])
    )
    monkeypatch.setattr("fieldkit.util.bounded_process.terminate_process_group", consume_child_cleanup)

    with pytest.raises(KeyboardInterrupt):
        run_bounded_process(
            [sys.executable, "-I", "-c", "pass"], timeout=5, stdout_limit=16, stderr_limit=16, cleanup_timeout=0.05
        )

    assert elapsed <= 0.05
    assert process.stdout.closed
    assert process.stderr.closed


def test_cleanup_never_resignals_after_ownership_is_lost_during_interrupted_wait() -> None:
    from fieldkit.util.bounded_process import ProcessOwnershipLostError

    process = _mock_pipe_process()
    process.wait.side_effect = KeyboardInterrupt
    with (
        patch("fieldkit.util.bounded_process.subprocess.Popen", return_value=process),
        patch(
            "fieldkit.util.bounded_process.process_exited_unreaped",
            side_effect=[True, ProcessOwnershipLostError("lost", reason="cleanup")],
        ),
        patch("fieldkit.util.bounded_process.os.killpg") as killpg,
        pytest.raises(KeyboardInterrupt) as captured,
    ):
        run_bounded_process(
            [sys.executable, "-I", "-c", "pass"], timeout=5, stdout_limit=16, stderr_limit=16, cleanup_timeout=1
        )

    assert killpg.call_count == 1
    assert process.wait.call_count == 1
    assert any("ProcessOwnershipLostError" in note for note in captured.value.__notes__)
    assert process.stdout.closed
    assert process.stderr.closed


def test_interrupt_during_reap_retries_owned_cleanup_with_remaining_budget() -> None:
    process = _mock_pipe_process()
    waits = 0

    def interrupt_first_wait(*, timeout: float) -> int:
        nonlocal waits
        assert 0 < timeout <= 1
        waits += 1
        if waits == 1:
            raise KeyboardInterrupt
        process.returncode = 0
        return 0

    process.wait.side_effect = interrupt_first_wait
    with (
        patch("fieldkit.util.bounded_process.subprocess.Popen", return_value=process),
        patch("fieldkit.util.bounded_process.process_exited_unreaped", return_value=True),
        patch("fieldkit.util.bounded_process.os.killpg") as killpg,
        pytest.raises(KeyboardInterrupt),
    ):
        run_bounded_process(
            [sys.executable, "-I", "-c", "pass"], timeout=5, stdout_limit=16, stderr_limit=16, cleanup_timeout=1
        )

    assert waits == 2
    assert killpg.call_count == 2
    assert process.stdout.closed
    assert process.stderr.closed
