"""Exact artifact diagnostic commands retain bounded output and deadlines."""

import subprocess
from io import BytesIO
from pathlib import Path

import pytest

from scripts import documentation_command_runner as runner

pytestmark = pytest.mark.unit


class _OwnerProcess:
    def __init__(self, *, expires: bool = False) -> None:
        self.stdout = BytesIO(b"x" * 100_000)
        self.stderr = BytesIO(b"y" * 100_000)
        self.expires = expires
        self.deadlines: list[int | None] = []

    def wait(self, timeout: int | None = None) -> int:
        self.deadlines.append(timeout)
        if self.expires:
            raise subprocess.TimeoutExpired("diagnostic-owner", timeout or 0)
        return 1


@pytest.mark.parametrize(
    ("argv", "deadline", "stdout_limit"),
    [
        (runner._ARTIFACT_DIAGNOSTIC_ARGV, 1800, 65_536),
        (runner._ARTIFACT_DIAGNOSTIC_ARGV[:-1], 1800, 4000),
        ((*runner._ARTIFACT_DIAGNOSTIC_ARGV, "--unexpected"), 120, 4000),
        (("ordinary-owner",), 120, 4000),
    ],
)
def test_traced_command_uses_exact_diagnostic_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    deadline: int,
    stdout_limit: int,
) -> None:
    process = _OwnerProcess()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    transport = runner._TraceTransport(("trace-owner",), BytesIO(), BytesIO(), "owner")
    result = runner._run_traced(tmp_path, argv, {}, transport, (), timeout=None)

    assert result.exit_code == 1
    assert result.argv == argv
    assert process.deadlines == [deadline]
    assert result.stdout == "x" * stdout_limit
    assert result.stderr == "y" * 4000
    assert transport.writer.closed


@pytest.mark.parametrize("timeout", [None, 17])
def test_diagnostic_timeout_reaps_owner_and_cannot_report_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: int | None
) -> None:
    process = _OwnerProcess(expires=True)
    reaped: list[_OwnerProcess] = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(runner, "_kill_and_reap", reaped.append)
    transport = runner._TraceTransport(("trace-owner",), BytesIO(), BytesIO(), "owner")
    result = runner._run_traced(tmp_path, runner._ARTIFACT_DIAGNOSTIC_ARGV, {}, transport, (), timeout=timeout)

    assert result.exit_code == 124
    assert process.deadlines == [1800 if timeout is None else timeout]
    assert reaped == [process]
    assert len(result.stdout.encode("utf-8")) == 65_536
    assert len(result.stderr.encode("utf-8")) == 4000
    assert transport.writer.closed
