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
import subprocess
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


def test_build_opencode_env_removes_vertex_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Developer runs stay non-Vertex even when the interactive shell enables it."""
    from fieldkit.driver.opencode import _build_opencode_env

    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")

    result = _build_opencode_env(99, None)

    assert result["FIELDKIT_LLM_ACCOUNT"] == "driver-issue-99"
    assert "CLAUDE_CODE_USE_VERTEX" not in result


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

    def wait(self, timeout: float | None = None) -> int:
        if self._remaining > 0:
            self._remaining -= 1
            raise subprocess.TimeoutExpired("opencode", timeout or 0)
        self.returncode = self._final_returncode
        return self._final_returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self.returncode = self._final_returncode


def _guard_with(tmp_path: Path, proc: _FakeProc, counts: list[int]) -> tuple[int, bool]:
    """Run the guard against *proc*, returning ``counts[i]`` at poll ``i``."""
    from fieldkit.driver import opencode as oc

    seq = iter([0, *counts])

    with (
        patch.object(oc.subprocess, "Popen", return_value=proc),
        patch.object(oc, "_count_rate_limit_errors", side_effect=lambda *_paths: next(seq, counts[-1])),
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
    assert proc.kills == 0


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
    assert proc.kills == 0


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
        patch.object(oc.subprocess, "Popen", return_value=proc),
        patch.object(oc, "_count_rate_limit_errors", return_value=0) as count,
    ):
        oc._run_with_rate_limit_guard([], tmp_path, {}, None, None, stderr)

    assert all(call.args == (stderr,) for call in count.call_args_list)


def test_count_rate_limit_errors_ignores_missing_logs(tmp_path: Path) -> None:
    """Missing diagnostic logs degrade safely rather than masking the actual child outcome."""
    from fieldkit.driver.opencode import _count_rate_limit_errors

    result = _count_rate_limit_errors(tmp_path / "missing.err", tmp_path / "missing.log")

    assert result == 0
