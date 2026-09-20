"""Tests for fieldkit.web.prs.run_gh — the gh CLI subprocess wrapper."""

import subprocess
from unittest.mock import patch

import pytest

from fieldkit.errors import WebDataError
from fieldkit.web.prs import _GH_TIMEOUT_SECONDS, run_gh

pytestmark = pytest.mark.unit


def _make_completed_process(*, returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_gh_raises_when_gh_not_on_path() -> None:
    with (
        patch("fieldkit.web.prs.shutil.which", return_value=None) as mock_which,
        patch("fieldkit.web.prs.subprocess.run") as mock_run,
        pytest.raises(WebDataError, match=r"gh CLI not found on PATH.*install GitHub CLI"),
    ):
        run_gh(["pr", "list"])

    mock_which.assert_called_once_with("gh")
    mock_run.assert_not_called()


def test_run_gh_raises_on_timeout() -> None:
    with (
        patch("fieldkit.web.prs.shutil.which", return_value="/opt/fake/gh"),
        patch(
            "fieldkit.web.prs.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh", "pr", "list"], timeout=_GH_TIMEOUT_SECONDS),
        ),
        pytest.raises(WebDataError, match=rf"gh pr list timed out after {_GH_TIMEOUT_SECONDS}s"),
    ):
        run_gh(["pr", "list", "--extra", "ignored-for-message"])


def test_run_gh_raises_on_nonzero_exit_uses_last_stderr_line() -> None:
    stderr = "warning: something\nerror: the real problem\n"
    completed = _make_completed_process(returncode=1, stderr=stderr)
    with (
        patch("fieldkit.web.prs.shutil.which", return_value="/opt/fake/gh"),
        patch("fieldkit.web.prs.subprocess.run", return_value=completed),
        pytest.raises(WebDataError, match=r"gh pr merge exited 1: error: the real problem"),
    ):
        run_gh(["pr", "merge", "7"])


def test_run_gh_raises_on_nonzero_exit_with_empty_stderr_falls_back() -> None:
    completed = _make_completed_process(returncode=1, stderr="   \n  ")
    with (
        patch("fieldkit.web.prs.shutil.which", return_value="/opt/fake/gh"),
        patch("fieldkit.web.prs.subprocess.run", return_value=completed),
        pytest.raises(WebDataError, match=r"gh pr merge exited 1: no stderr"),
    ):
        run_gh(["pr", "merge", "7"])


def test_run_gh_returns_stdout_unmodified_on_success() -> None:
    stdout = "  raw output with trailing spaces   \n"
    completed = _make_completed_process(returncode=0, stdout=stdout)
    with (
        patch("fieldkit.web.prs.shutil.which", return_value="/opt/fake/gh"),
        patch("fieldkit.web.prs.subprocess.run", return_value=completed),
    ):
        result = run_gh(["pr", "list"])

    assert result == stdout


def test_run_gh_invokes_subprocess_with_expected_args() -> None:
    completed = _make_completed_process(returncode=0, stdout="ok")
    with (
        patch("fieldkit.web.prs.shutil.which", return_value="/opt/fake/gh") as mock_which,
        patch("fieldkit.web.prs.subprocess.run", return_value=completed) as mock_run,
    ):
        run_gh(["pr", "list", "--json", "number"])

    mock_which.assert_called_once_with("gh")
    mock_run.assert_called_once_with(
        ["/opt/fake/gh", "pr", "list", "--json", "number"],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT_SECONDS,
        check=False,
    )
