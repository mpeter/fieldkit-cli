"""Prepared dependency inputs remain held through every documentation owner."""

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import check_documentation_examples as examples
from scripts import documentation_candidate_execution as execution
from scripts import documentation_manual
from scripts.documentation_command_runner import CommandResult
from scripts.documentation_commands import DOCUMENT_COMMANDS
from scripts.documentation_manual import ManualScenario
from scripts.documentation_runtime import DependencyInput, RuntimeTools
from tests.documentation_contract_support import passing_documentation_run, write_example_verification_fixture

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fixture_manual_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the example fixture bound to a fixed controller manual scenario."""
    monkeypatch.setattr(
        documentation_manual,
        "MANUAL_SCENARIOS",
        (
            ManualScenario(
                identifier="readme.release",
                document="README.md",
                field="fenced_blocks",
                sha256="a" * 64,
                proof_type="release-cutover",
                precondition_id="fixture-release-candidate",
            ),
        ),
    )


@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("owner_fails", [False, True])
def test_bound_preparation_lifetime_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied: bool, owner_fails: bool
) -> None:
    events: list[str] = []
    descriptor: int | None = None
    tools = RuntimeTools(11, 12)
    retained = DependencyInput(99, "b" * 64) if supplied else None
    uv = execution.TrustedUv(11, "a" * 64, 1)
    python = execution.TrustedInterpreter(12, "c" * 64, 2)

    @contextmanager
    def build(*_args: object) -> Iterator[execution.TrustedInterpreter]:
        events.append("runtime")
        try:
            yield python
        finally:
            events.append("runtime-close")

    @contextmanager
    def prepare(
        root: Path, observed_tools: RuntimeTools, *, supplied: DependencyInput | None
    ) -> Iterator[DependencyInput]:
        nonlocal descriptor
        assert root == tmp_path
        assert observed_tools == tools
        assert supplied == retained
        events.append("prepare")
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            yield DependencyInput(descriptor, "d" * 64)
        finally:
            os.close(descriptor)
            events.append("dependency-close")

    def runner(context: execution.BoundExecution) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        assert context.tools == tools
        assert context.dependency_input.directory_descriptor == descriptor
        assert os.fstat(context.dependency_input.directory_descriptor).st_ino == tmp_path.stat().st_ino
        assert events == ["prepare", "runtime"]
        events.append("owner")
        if owner_fails:
            raise ValueError("owner failed")
        return [], ("pending",)

    monkeypatch.setattr(execution, "_build_runtime", build)
    monkeypatch.setattr(execution, "prepare_dependencies", prepare)
    monkeypatch.setattr(
        execution, "_descriptor_identity", lambda fd, _label: ("a" * 64, 1) if fd == 11 else ("c" * 64, 2)
    )
    if owner_fails:
        with pytest.raises(ValueError, match="owner failed"):
            execution._run_bound_plan(tmp_path, tmp_path, "e" * 40, (), uv, python, runner, dependency_input=retained)
    else:
        result = execution._run_bound_plan(
            tmp_path, tmp_path, "e" * 40, (), uv, python, runner, dependency_input=retained
        )
        assert result == ([], ("pending",))
    assert events == ["prepare", "runtime", "owner", "runtime-close", "dependency-close"]
    assert descriptor is not None
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def test_bound_preparation_failure_prevents_runtime_and_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution, "_build_runtime", lambda *_: pytest.fail("runtime built after preparation failed"))

    @contextmanager
    def failed(*_args: object, **_options: object) -> Iterator[DependencyInput]:
        raise ValueError("closure unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(execution, "prepare_dependencies", failed)
    with pytest.raises(ValueError, match="closure unavailable"):
        execution._run_bound_plan(
            tmp_path,
            tmp_path,
            "e" * 40,
            (),
            execution.TrustedUv(11, "a" * 64, 1),
            execution.TrustedInterpreter(12, "c" * 64, 2),
            lambda _: pytest.fail("owner ran after preparation failed"),
        )


def test_check_threads_dependency_input_to_contract_and_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_example_verification_fixture(tmp_path)
    dependencies = DependencyInput(11, "a" * 64)
    observed: list[tuple[str, ...]] = []

    def run(root: Path, argv: tuple[str, ...], **options: object) -> CommandResult:
        assert options["dependency_input"] is dependencies
        observed.append(argv)
        return passing_documentation_run(root, argv)

    monkeypatch.setattr(examples, "_run", run)
    result, _pending = examples.check(tmp_path, dependency_input=dependencies)
    assert len(result) == 7
    assert len(observed) == 8
    assert observed[0] == DOCUMENT_COMMANDS["source_contract"][0]


@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("preparation_fails", [False, True])
def test_real_dirty_main_prepares_before_owners_and_keeps_diagnostic_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    preparation_fails: bool,
    supplied: bool,
) -> None:
    write_example_verification_fixture(tmp_path)
    events: list[str] = []
    dependencies = DependencyInput(11, "a" * 64)
    retained = DependencyInput(22, "b" * 64) if supplied else None
    tools = RuntimeTools(33, 44)

    @contextmanager
    def held_tools() -> Iterator[RuntimeTools]:
        events.append("tools")
        try:
            yield tools
        finally:
            events.append("tools-close")

    @contextmanager
    def prepare(
        root: Path, observed_tools: RuntimeTools, *, supplied: DependencyInput | None
    ) -> Iterator[DependencyInput]:
        assert root == tmp_path
        assert observed_tools is tools
        assert supplied is retained
        events.append("prepare")
        if preparation_fails:
            raise ValueError("dependency input failed")
        try:
            yield dependencies
        finally:
            events.append("dependency-close")

    def check(root: Path, **options: object) -> tuple[list[CommandResult], tuple[str, ...]]:
        assert events == ["tools", "prepare"]
        assert root == tmp_path
        assert options["runtime_tools"] is tools
        assert options["dependency_input"] is dependencies
        assert options["source_revision"] is None
        assert options["diagnostic_dirty"] is True
        commands = options["execution_commands"]
        assert isinstance(commands, tuple)
        events.append("owners")
        artifact_argv = examples._AUTOMATED_COMMANDS["automated.installed-base-artifact"][0]
        return [
            CommandResult((*argv, "--diagnostic-dirty") if argv == artifact_argv else argv, 1, "", "")
            for argv in commands
        ], ()

    monkeypatch.setattr(examples, "diagnostic_tools", held_tools)
    monkeypatch.setattr(examples, "prepare_dependencies", prepare)
    monkeypatch.setattr(examples, "check", check)
    result = examples.main(["--repo-root", str(tmp_path), "--report", "-"], dependency_input=retained)
    captured = capsys.readouterr()
    assert result == (2 if preparation_fails else 1)
    if preparation_fails:
        assert events == ["tools", "prepare", "tools-close"]
        assert "dependency input failed" in captured.err
    else:
        assert events == ["tools", "prepare", "owners", "dependency-close", "tools-close"]
        assert '"evidence_kind": "diagnostic"' in captured.out
        assert '"runtime_tool_approval": "pending"' in captured.out


@pytest.mark.parametrize("body_fails", [False, True])
def test_diagnostic_tools_hold_exact_descriptors_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_fails: bool
) -> None:
    uv_path = tmp_path / "uv"
    uv_path.write_bytes(b"diagnostic tool")
    monkeypatch.setattr(shutil, "which", lambda _: str(uv_path))
    descriptors: list[int] = []

    def exercise() -> int:
        with execution.diagnostic_tools() as tools:
            descriptors.extend((tools.uv_descriptor, tools.python_descriptor))
            assert os.pread(tools.uv_descriptor, 15, 0) == b"diagnostic tool"
            assert os.fstat(tools.python_descriptor).st_size > 0
            if body_fails:
                raise ValueError("diagnostic body failed")
            return len(descriptors)

    if body_fails:
        with pytest.raises(ValueError, match="diagnostic body failed"):
            exercise()
    else:
        assert exercise() == 2
    for descriptor in descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
