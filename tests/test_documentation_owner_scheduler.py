"""Resource admission and stable reporting for fixed documentation owners."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier, Event, Lock
from typing import ParamSpec, TypeVar

import pytest

from scripts import check_documentation_examples as verifier
from scripts.documentation_command_runner import CommandResult
from scripts.documentation_commands import DOCUMENT_COMMANDS, EXAMPLE_COMMANDS

pytestmark = pytest.mark.unit
_P = ParamSpec("_P")
_T = TypeVar("_T")


def test_scheduler_prioritizes_expensive_owners_but_reports_canonical_order(monkeypatch: pytest.MonkeyPatch) -> None:
    submitted: list[object] = []

    class RecordingExecutor(ThreadPoolExecutor):
        def submit(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> Future[_T]:
            submitted.append(args[0])
            return super().submit(fn, *args, **kwargs)

    monkeypatch.setattr(verifier, "ThreadPoolExecutor", RecordingExecutor)
    cheap = ("fixture", "cheap")
    artifact = EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    first_user = DOCUMENT_COMMANDS["first_user_guides_contract"][0]
    configuration = DOCUMENT_COMMANDS["configuration_contract"][0]
    commands = (cheap, configuration, first_user, artifact)

    def run(argv: tuple[str, ...]) -> CommandResult:
        return CommandResult(argv, 0, "output", "")

    results = verifier._run_owners(commands, run)

    assert [result.argv for result in results] == list(commands)
    assert submitted == [artifact, first_user, configuration, cheap]


@pytest.mark.parametrize("exit_code", [1, 124, -9])
def test_scheduler_reserves_artifact_slots_and_runs_remaining_owners_after_failure(exit_code: int) -> None:
    artifact = EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    commands = (*(("fixture", str(index)) for index in range(5)), artifact)
    first_wave = Barrier(3, timeout=5)
    tail_started = Event()
    lock = Lock()
    active_weight = 0
    peak_weight = 0
    started: list[tuple[str, ...]] = []
    completed: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...]) -> CommandResult:
        nonlocal active_weight, peak_weight
        weight = 2 if argv == artifact else 1
        with lock:
            active_weight += weight
            peak_weight = max(peak_weight, active_weight)
            started.append(argv)
        if argv in (artifact, *commands[:2]):
            first_wave.wait()
        if argv == artifact:
            assert tail_started.wait(timeout=5)
        if argv == commands[2]:
            tail_started.set()
        with lock:
            completed.append(argv)
            active_weight -= weight
        return CommandResult(argv, exit_code if argv == artifact else 0, repr(argv), "retained diagnostics")

    results = verifier._run_owners(commands, run)

    assert [result.argv for result in results] == list(commands)
    assert peak_weight == 4
    assert active_weight == 0
    assert sorted(started) == sorted(commands)
    assert len(started) == len(commands)
    assert completed.index(commands[2]) < completed.index(artifact)
    assert results[-1] == CommandResult(artifact, exit_code, repr(artifact), "retained diagnostics")


def test_scheduler_reports_results_in_plan_order_when_completion_is_reversed() -> None:
    commands = tuple(("fixture", str(index)) for index in range(4))
    first_wave = Barrier(4, timeout=5)
    release_first = Event()

    def run(argv: tuple[str, ...]) -> CommandResult:
        first_wave.wait()
        if argv == commands[0]:
            assert release_first.wait(timeout=5)
        if argv == commands[-1]:
            release_first.set()
        return CommandResult(argv, 0, repr(argv), "")

    results = verifier._run_owners(commands, run)

    assert results == [CommandResult(argv, 0, repr(argv), "") for argv in commands]


def test_scheduler_empty_plan_runs_nothing() -> None:
    def run(argv: tuple[str, ...]) -> CommandResult:
        pytest.fail(f"unexpected owner: {argv}")

    assert verifier._run_owners((), run) == []


def test_scheduler_runs_entire_plan_before_propagating_an_owner_exception() -> None:
    commands = tuple(("fixture", str(index)) for index in range(9))
    started: list[tuple[str, ...]] = []
    lock = Lock()

    def run(argv: tuple[str, ...]) -> CommandResult:
        with lock:
            started.append(argv)
        if argv == commands[0]:
            raise ValueError("owner unavailable")
        return CommandResult(argv, 0, "", "")

    with pytest.raises(ValueError, match="owner unavailable"):
        verifier._run_owners(commands, run)

    assert sorted(started) == sorted(commands)
