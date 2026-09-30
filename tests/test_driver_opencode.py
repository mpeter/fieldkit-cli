"""Tests for fieldkit.driver.opencode — headless OpenCode session execution.

Covers the run_opencode contract: --file invocation, push verification, and the
OpencodeOutcome failure-mode distinction (timeout vs non-zero exit vs not-pushed)
plus persisted child stdout/stderr logs.

Patches target ``fieldkit.driver.opencode.*`` because run_opencode resolves its
collaborators (shutil, _run_with_rate_limit_guard, _branch_pushed,
get_fieldkit_data) in that module — this suite was split out of test_driver.py
when the session concern moved to its own module. ``_run_with_rate_limit_guard``
returns ``(returncode, rate_limited)`` rather than a subprocess.run-style result.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _make_work_order(tmp_path: Path) -> Path:
    wo = tmp_path / "docs" / "work-orders" / "test.md"
    wo.parent.mkdir(parents=True)
    wo.write_text("---\nlast_reviewed: 2026-07-11\n---\n# Work Order\n")
    return wo


def test_passes_file_flag_not_content(tmp_path: Path) -> None:
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode._branch_pushed", return_value=True),
        patch("fieldkit.driver.opencode._pr_exists", return_value=True),
        patch("fieldkit.driver.opencode.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard") as mock_run,
    ):
        mock_run.return_value = (0, False)
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "ok"
    cmd = mock_run.call_args[0][0]
    assert "--file" in cmd
    assert str(wo) in cmd
    # Work order content must NOT appear inline in the argv
    assert "---" not in cmd
    assert cmd[:7] == ["/usr/bin/opencode", "run", "--pure", "--auto", "--agent", "build", "--dir"]
    message = cmd[-1]
    assert "structured `edit_sites` authority" in message
    assert "journeyman" not in message.casefold()


def test_retry_prompt_uses_only_bounded_typed_local_receipt(tmp_path: Path) -> None:
    from fieldkit.driver.opencode import _build_opencode_command
    from fieldkit.driver.retry_state import RetryReceipt

    receipt = RetryReceipt(
        failure_code="verification-failed",
        attempt=1,
        source_revision="a" * 40,
    )

    command = _build_opencode_command("/usr/bin/opencode", tmp_path, tmp_path / "work.md", receipt)
    message = command[-1]

    assert "verification-failed" in message
    assert "a" * 40 in message
    assert "data only" in message.lower()
    assert "previous attempt failed with this error" not in message


def test_build_opencode_env_removes_vertex_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Developer runs stay non-Vertex even when the interactive shell enables it."""
    from fieldkit.driver.opencode import _build_opencode_env

    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")

    result = _build_opencode_env(99, None)

    assert result["FIELDKIT_LLM_ACCOUNT"] == "driver-issue-99"
    assert "CLAUDE_CODE_USE_VERTEX" not in result


def test_build_opencode_env_excludes_unreviewed_secrets_and_python_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fieldkit.driver.opencode import _build_opencode_env

    monkeypatch.setenv("FICTIONAL_SECRET_TOKEN", "sentinel")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sentinel")
    monkeypatch.setenv("PYTHONPATH", "/fictional/injection")
    monkeypatch.setenv("PATH", "/usr/bin")

    result = _build_opencode_env(99, None)

    assert result["PATH"] == "/usr/bin"
    assert "FICTIONAL_SECRET_TOKEN" not in result
    assert "AWS_SECRET_ACCESS_KEY" not in result
    assert "PYTHONPATH" not in result


def test_returns_false_when_branch_not_pushed(tmp_path: Path) -> None:
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode._branch_pushed", return_value=False),
        patch("fieldkit.driver.opencode.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard") as mock_run,
    ):
        mock_run.return_value = (0, False)
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "failed"


def test_returns_false_when_pr_not_opened(tmp_path: Path) -> None:
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode._branch_pushed", return_value=True),
        patch("fieldkit.driver.opencode._pr_exists", return_value=False),
        patch("fieldkit.driver.opencode.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard") as mock_run,
    ):
        mock_run.return_value = (0, False)
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "failed"


def test_dry_run_does_not_execute(tmp_path: Path) -> None:
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard") as mock_run,
    ):
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test", dry_run=True)

    assert result.status == "ok"
    mock_run.assert_not_called()


def test_missing_packaged_executor_is_a_bounded_failure(tmp_path: Path) -> None:
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)
    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode.importlib.resources.files", side_effect=FileNotFoundError),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard") as mock_run,
    ):
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "failed"
    assert result.reason == "packaged driver executor instructions are unavailable"
    mock_run.assert_not_called()


def test_run_opencode_timeout_yields_timed_out_reason(tmp_path: Path) -> None:
    """Bug 3 (key regression): a child ``TimeoutExpired`` is a distinct, named
    failure mode — ``ok=False`` with a reason containing 'timed out', separable
    from a plain non-zero exit.
    """
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode.get_fieldkit_data", return_value=tmp_path),
        patch(
            "fieldkit.driver.opencode._run_with_rate_limit_guard",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=3600),
        ),
    ):
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "failed"
    assert "timed out" in result.reason


def test_run_opencode_rate_limited_yields_distinct_outcome(tmp_path: Path) -> None:
    """Sustained rate-limiting (historic regression) yields ``status="rate_limited"`` — a
    distinct outcome from a real work-order failure, so the driver does not
    spend an attempt on infrastructure starvation.
    """
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard", return_value=(0, True)),
    ):
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "rate_limited"
    assert "rate limit" in result.reason.lower()


def test_run_opencode_nonzero_exit_yields_exit_code_reason(tmp_path: Path) -> None:
    """Bug 3: a non-zero exit yields ``ok=False`` with 'exited with code'."""
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard", return_value=(2, False)),
    ):
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "failed"
    assert "exited with code" in result.reason
    assert "2" in result.reason


def test_run_opencode_exit_zero_not_pushed_yields_not_pushed_reason(tmp_path: Path) -> None:
    """Bug 3: exit-0 but branch never pushed yields ``ok=False`` / 'not pushed'."""
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode._branch_pushed", return_value=False),
        patch("fieldkit.driver.opencode.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard", return_value=(0, False)),
    ):
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "failed"
    assert "not pushed" in result.reason


def test_run_opencode_writes_child_stdout_stderr_logs(tmp_path: Path) -> None:
    """Bug 3: child stdout/stderr are persisted under logs/driver so a failed (or
    hung-then-killed) run is diagnosable after worktree teardown.
    """
    from fieldkit.driver.opencode import run_opencode

    wo = _make_work_order(tmp_path)

    with (
        patch("fieldkit.driver.opencode.shutil.which", return_value="/usr/bin/opencode"),
        patch("fieldkit.driver.opencode._branch_pushed", return_value=True),
        patch("fieldkit.driver.opencode._pr_exists", return_value=True),
        patch("fieldkit.driver.opencode.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.opencode._run_with_rate_limit_guard", return_value=(0, False)),
    ):
        result = run_opencode(wo, tmp_path, 99, "driver/issue-99-test")

    assert result.status == "ok"
    logs_dir = tmp_path / "logs" / "driver"
    assert len(list(logs_dir.glob("opencode-issue-99-*.out"))) == 1
    assert len(list(logs_dir.glob("opencode-issue-99-*.err"))) == 1
    assert logs_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in logs_dir.glob("opencode-issue-99-*.*"))


def test_opencode_output_overflow_is_bounded_and_nonpassing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.driver import opencode

    monkeypatch.setattr(opencode, "_OPENCODE_STREAM_BYTES", 1024)
    monkeypatch.setattr(opencode, "TIMEOUT_RATE_LIMIT_POLL", 0.01)
    monkeypatch.setattr(opencode, "get_fieldkit_data", lambda: tmp_path)
    stdout_path, stderr_path = opencode._open_opencode_logs(99)
    assert stdout_path is not None
    assert stderr_path is not None

    with pytest.raises(opencode.OpencodeOutputOverflow, match="bounded"):
        opencode._run_opencode_process(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 65536)"],
            tmp_path,
            {"PATH": os.environ["PATH"]},
            stdout_path,
            stderr_path,
        )

    assert stdout_path.stat().st_size <= 1024
    assert stderr_path.stat().st_size <= 1024


def test_nonzero_result_never_promotes_raw_stderr_to_diagnostics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from fieldkit.driver.opencode import _classify_opencode_result

    stderr = tmp_path / "run.err"
    stderr.write_text("fictional-token private/provider/body", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="fieldkit.driver.opencode"):
        result = _classify_opencode_result(
            1,
            False,
            issue_number=99,
            branch="driver/issue-99-test",
            repo_root=tmp_path,
        )

    assert result.status == "failed"
    assert "fictional-token" not in result.reason
    assert "fictional-token" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_open_opencode_logs_warns_when_rate_limit_guard_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """historic regression: log failure must disclose the unavailable fast-abort guard."""
    from fieldkit.driver.opencode import _open_opencode_logs

    def fail_mkdir(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("fieldkit.driver.opencode.get_fieldkit_data", lambda: tmp_path)
    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with caplog.at_level(logging.WARNING, logger="fieldkit.driver.opencode"):
        result = _open_opencode_logs(99)

    assert result == (None, None)
    assert "output will not be captured" in caplog.text
    assert "rate-limit fast-abort is disabled" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "disk full" not in caplog.text


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        pytest.param(1, "a" * 40 + "\trefs/heads/driver/issue-99-test\n", id="nonzero"),
        pytest.param(0, "unexpected output\n", id="malformed"),
    ],
)
def test_branch_pushed_rejects_failed_or_malformed_lookup(tmp_path: Path, returncode: int, stdout: str) -> None:
    from fieldkit.driver.opencode import _branch_pushed
    from fieldkit.util.bounded_process import BoundedProcessResult

    with patch(
        "fieldkit.driver.opencode.run_bounded_process",
        return_value=BoundedProcessResult(returncode, stdout, "private provider detail"),
    ):
        result = _branch_pushed("driver/issue-99-test", tmp_path)

    assert result is False


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        pytest.param(1, "1\n", id="nonzero"),
        pytest.param(0, "true\n", id="malformed"),
        pytest.param(0, "-1\n", id="negative"),
    ],
)
def test_pr_exists_rejects_failed_or_malformed_lookup(tmp_path: Path, returncode: int, stdout: str) -> None:
    from fieldkit.driver.opencode import _pr_exists
    from fieldkit.util.bounded_process import BoundedProcessResult

    with patch(
        "fieldkit.driver.opencode.run_bounded_process",
        return_value=BoundedProcessResult(returncode, stdout, "private provider detail"),
    ):
        result = _pr_exists("driver/issue-99-test", tmp_path)

    assert result is False


# --- sustained-rate-limit detection -----------------------------------------
# These exercise _run_with_rate_limit_guard itself. The suite above mocks it
# wholesale, so before historic regression's follow-up the detection logic — the part that
# decides whether to kill a running session — had no coverage at all.


class _FakeProc:
    """Popen stand-in whose wait() times out a fixed number of times.

    Each wait() call represents one poll interval, so a test can script what
    stderr looks like at each poll and assert on when the guard gives up.
    """

    def __init__(self, timeouts: int, returncode: int = -9) -> None:
        self._remaining = timeouts
        self._final_returncode = returncode
        self.returncode: int | None = None
        self.kills = 0
        self.pid = None
        self.stdout = BytesIO()
        self.stderr = BytesIO()

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._final_returncode
        return self._final_returncode

    def exited_unreaped(self) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return False
        return True

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self._remaining = 0
        self.returncode = self._final_returncode


class _FakeDrain:
    overflow = False

    def __init__(self, *_args: object) -> None:
        pass

    def poll(self, _timeout: float) -> None:
        threading.Event().wait(_timeout)

    def finish(self, _timeout: float) -> None:
        pass

    def close(self) -> None:
        pass


def _guard_with(tmp_path: Path, proc: _FakeProc, counts: list[int]) -> tuple[int, bool]:
    """Run the guard against *proc*, returning ``counts[i]`` at poll ``i``."""
    from fieldkit.driver import opencode as oc

    seq = iter([0, *counts])

    with (
        patch("fieldkit.driver.opencode.subprocess.Popen", return_value=proc),
        patch.object(oc, "_NonblockingDrain", _FakeDrain),
        patch.object(oc, "TIMEOUT_RATE_LIMIT_POLL", 0.001),
        patch.object(oc, "process_exited_unreaped", side_effect=lambda _proc: proc.exited_unreaped()),
        patch.object(oc, "_count_rate_limit_errors", side_effect=lambda *_paths: next(seq, counts[-1])),
        patch("fieldkit.driver.opencode.time.sleep"),
    ):
        return oc._run_with_rate_limit_guard([], tmp_path, {}, None, None, tmp_path / "err")


def test_burst_that_stops_does_not_abort(tmp_path: Path) -> None:
    """Three rejections in one poll then silence is transient, not sustained.

    This is the case a cumulative threshold gets wrong: the backoff is
    2/4/8/16s, so a burst can exceed any count inside a single interval and
    kill a session that recovers on the next attempt.
    """
    proc = _FakeProc(timeouts=3, returncode=0)
    returncode, rate_limited = _guard_with(tmp_path, proc, [3, 3, 3])

    assert rate_limited is False
    assert returncode == 0
    assert proc.kills == 1


def test_completed_guard_signals_owned_group_before_reaping(tmp_path: Path) -> None:
    """A successful leader remains unreaped until its owned process group is cleaned."""
    from fieldkit.driver import opencode as oc

    proc = _FakeProc(timeouts=0, returncode=0)
    with (
        patch("fieldkit.driver.opencode.subprocess.Popen", return_value=proc),
        patch.object(oc, "_NonblockingDrain", _FakeDrain),
        patch.object(oc, "process_exited_unreaped", return_value=True),
        patch.object(oc, "_count_rate_limit_errors", return_value=0),
        patch.object(oc, "_kill_and_reap", wraps=oc._kill_and_reap) as cleanup,
        patch("fieldkit.driver.opencode.time.sleep"),
    ):
        result = oc._run_with_rate_limit_guard([], tmp_path, {}, None, None, None)

    assert result == (0, False)
    cleanup.assert_called_once_with(proc)


def test_lost_process_ownership_never_signals_stale_group(tmp_path: Path) -> None:
    from fieldkit.driver import opencode as oc
    from fieldkit.util.bounded_process import BoundedProcessError

    proc = _FakeProc(timeouts=0, returncode=0)
    with (
        patch("fieldkit.driver.opencode.subprocess.Popen", return_value=proc),
        patch.object(oc, "_NonblockingDrain", _FakeDrain),
        patch.object(
            oc,
            "process_exited_unreaped",
            side_effect=BoundedProcessError("child process ownership was lost", reason="cleanup"),
        ),
        patch.object(oc, "_count_rate_limit_errors", return_value=0),
        patch.object(oc, "_kill_and_reap") as cleanup,
        patch("fieldkit.driver.opencode.time.sleep"),
        pytest.raises(BoundedProcessError, match="ownership was lost"),
    ):
        oc._run_with_rate_limit_guard([], tmp_path, {}, None, None, None)

    cleanup.assert_not_called()


def test_lost_ownership_with_inherited_pipes_returns_within_cleanup_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver import opencode as oc
    from fieldkit.util.bounded_process import BoundedProcessError

    child = "import time; time.sleep(0.6)"
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"

    def observe_after_reap(proc: subprocess.Popen[bytes]) -> bool:
        proc.wait(timeout=1)
        raise BoundedProcessError("child process ownership was lost", reason="cleanup")

    monkeypatch.setattr(oc, "TIMEOUT_PROCESS_KILL_GRACE", 0.05)
    monkeypatch.setattr(oc, "process_exited_unreaped", observe_after_reap)
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    fd_root = Path("/proc/self/fd")
    baseline_fds = len(tuple(fd_root.iterdir()))
    started = time.monotonic()
    with pytest.raises(BoundedProcessError, match="ownership was lost"):
        oc._run_with_rate_limit_guard(
            [sys.executable, "-c", parent],
            tmp_path,
            {"PATH": os.environ["PATH"]},
            None,
            None,
            None,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 0.3
    assert {thread.ident for thread in threading.enumerate()} == baseline_threads
    assert len(tuple(fd_root.iterdir())) == baseline_fds


def test_chatty_process_uses_wall_clock_timeout_not_readiness_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver import opencode as oc

    script = "import os, time; [(os.write(2, b'x'), time.sleep(0.05)) for _ in range(6)]; time.sleep(0.05)"
    monkeypatch.setattr(oc, "_OPENCODE_TIMEOUT", 1.0)
    monkeypatch.setattr(oc, "TIMEOUT_RATE_LIMIT_POLL", 0.2)

    started = time.monotonic()
    result = oc._run_with_rate_limit_guard(
        [sys.executable, "-c", script],
        tmp_path,
        {"PATH": os.environ["PATH"]},
        None,
        None,
        None,
    )
    elapsed = time.monotonic() - started

    assert result == (0, False)
    assert 0.3 <= elapsed < 1.0


def test_sustained_rate_limit_requires_real_poll_intervals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.driver import opencode as oc

    samples = 0

    def count_errors(*_paths: Path | None) -> int:
        nonlocal samples
        samples += 1
        return samples

    monkeypatch.setattr(oc, "_OPENCODE_TIMEOUT", 1.0)
    monkeypatch.setattr(oc, "TIMEOUT_RATE_LIMIT_POLL", 0.1)
    monkeypatch.setattr(oc, "_count_rate_limit_errors", count_errors)
    started = time.monotonic()
    returncode, rate_limited = oc._run_with_rate_limit_guard(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        tmp_path,
        {"PATH": os.environ["PATH"]},
        None,
        None,
        None,
    )
    elapsed = time.monotonic() - started

    assert rate_limited is True
    assert returncode != 0
    assert elapsed >= 0.19


def test_new_errors_in_consecutive_polls_aborts(tmp_path: Path) -> None:
    """Rejections still arriving across consecutive polls is sustained."""
    proc = _FakeProc(timeouts=5)
    _returncode, rate_limited = _guard_with(tmp_path, proc, [1, 2, 3])

    assert rate_limited is True
    assert proc.kills == 1


def test_abort_requires_more_than_one_poll(tmp_path: Path) -> None:
    """A single poll can never trip the abort, whatever the count reaches.

    Guards the property that makes "sustained" honest: the decision spans at
    least _RATE_LIMIT_SUSTAINED_POLLS intervals of real elapsed time.
    """
    from fieldkit.driver.opencode import _RATE_LIMIT_SUSTAINED_POLLS

    assert _RATE_LIMIT_SUSTAINED_POLLS >= 2

    proc = _FakeProc(timeouts=1, returncode=0)
    _returncode, rate_limited = _guard_with(tmp_path, proc, [99])

    assert rate_limited is False


def test_intermittent_errors_reset_the_streak(tmp_path: Path) -> None:
    """A quiet poll between rejections breaks the run — that is recovery."""
    proc = _FakeProc(timeouts=4, returncode=0)
    # new, quiet, new, quiet -> never two consecutive polls with new errors
    _returncode, rate_limited = _guard_with(tmp_path, proc, [1, 1, 2, 2])

    assert rate_limited is False
    assert proc.kills == 1


def test_count_rate_limit_errors_uses_only_its_per_run_stderr(tmp_path: Path) -> None:
    """A neighbouring OpenCode session must not make this run look rate limited."""
    from fieldkit.driver.opencode import _count_rate_limit_errors

    stderr = tmp_path / "run.err"
    other_run = tmp_path / "other-run.err"
    stderr.write_text("request failed: rate_limit_exceeded\n", encoding="utf-8")
    other_run.write_text("request failed: rate_limit_exceeded\n", encoding="utf-8")

    result = _count_rate_limit_errors(stderr)

    assert result == 1


def test_rate_limit_guard_never_reads_the_shared_opencode_log(tmp_path: Path) -> None:
    """Global logs cross-talk once more than one developer job can be admitted."""
    from fieldkit.driver import opencode as oc

    stderr = tmp_path / "run.err"
    proc = _FakeProc(timeouts=1, returncode=0)
    with (
        patch("fieldkit.driver.opencode.subprocess.Popen", return_value=proc),
        patch.object(oc, "_NonblockingDrain", _FakeDrain),
        patch.object(oc, "TIMEOUT_RATE_LIMIT_POLL", 0.001),
        patch.object(oc, "process_exited_unreaped", side_effect=lambda _proc: proc.exited_unreaped()),
        patch.object(oc, "_count_rate_limit_errors", return_value=0) as count,
        patch("fieldkit.driver.opencode.time.sleep"),
    ):
        oc._run_with_rate_limit_guard([], tmp_path, {}, None, None, stderr)

    assert all(call.args == (stderr,) for call in count.call_args_list)


def test_count_rate_limit_errors_ignores_missing_logs(tmp_path: Path) -> None:
    """Missing diagnostic logs degrade safely rather than masking the actual child outcome."""
    from fieldkit.driver.opencode import _count_rate_limit_errors

    result = _count_rate_limit_errors(tmp_path / "missing.err", tmp_path / "missing.log")

    assert result == 0
