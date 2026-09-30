"""Tests for fixed documentation-example verification routing."""

import json
import signal
import subprocess
import sys
from contextlib import nullcontext
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Barrier, Lock

import pytest
from packaging.version import Version

from scripts import (
    check_documentation_contract,
    check_documentation_examples,
    documentation_candidate_execution,
    documentation_command_runner,
    documentation_commands,
    documentation_manual,
)
from scripts.documentation_commands import OUTER_SCENARIOS
from scripts.documentation_manual import ManualScenario
from tests.documentation_contract_support import artifact_summary_fixture, write_example_verification_fixture
from tests.documentation_contract_support import passing_documentation_run as _passing_documentation_run
from tests.documentation_contract_support import prepared_diagnostic_fixture as prepared_diagnostic_fixture

pytestmark = pytest.mark.unit

_PRODUCTION_MANUAL_SCENARIOS = documentation_manual.MANUAL_SCENARIOS


def passing_documentation_run(
    repo_root: Path, argv: tuple[str, ...], **options: object
) -> documentation_command_runner.CommandResult:
    """Preserve actual diagnostic argv and the producer's nonpassing summary."""
    canonical = argv[:-1] if argv[-1:] == ("--diagnostic-dirty",) else argv
    result = _passing_documentation_run(repo_root, canonical, **options)
    if canonical != argv and result.stdout:
        return documentation_command_runner.CommandResult(
            argv, 1, json.dumps(artifact_summary_fixture(diagnostic=True)), ""
        )
    return documentation_command_runner.CommandResult(argv, result.exit_code, result.stdout, result.stderr)


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("diagnostic", [False, True])
def test_unattested_artifact_retention_requires_nonpassing_command(exit_code: int, diagnostic: bool) -> None:
    report = artifact_summary_fixture(diagnostic=True)
    command = documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    if diagnostic:
        command = (*command, "--diagnostic-dirty")
    result = documentation_command_runner.CommandResult(command, exit_code, json.dumps(report), "")
    retained = check_documentation_examples._artifact_digests(
        [result], expected_name="fieldkit-cli", expected_version=Version("1.0.0")
    )
    assert retained == (report["artifacts"] if diagnostic and exit_code == 1 else [])


@pytest.mark.parametrize(
    "suffix",
    [(), ("--diagnostic-dirty",), ("--diagnostic-dirty=true",), ("--diagnostic-dirty", "--extra")],
)
@pytest.mark.parametrize("revision", [None, "b" * 40])
def test_artifact_retention_requires_exact_mode_and_source_binding(
    suffix: tuple[str, ...], revision: str | None
) -> None:
    command = documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    report = artifact_summary_fixture()
    artifacts = report["artifacts"]
    assert isinstance(artifacts, list)
    artifacts[0]["source_revision"] = revision
    result = documentation_command_runner.CommandResult((*command, *suffix), 0, json.dumps(report), "")
    retained = check_documentation_examples._artifact_digests(
        [result], expected_name="fieldkit-cli", expected_version=Version("1.0.0")
    )
    assert retained == (artifacts if suffix == () and revision is not None else [])


@pytest.mark.parametrize("status", ["pass", "pending", "diagnostic", "fail"])
@pytest.mark.parametrize("source_binding", ["unattested-dirty", "committed", None])
def test_diagnostic_artifact_retention_requires_unattested_nonpassing_report(
    status: str, source_binding: str | None
) -> None:
    command = documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    report = artifact_summary_fixture(diagnostic=True)
    report["status"] = status
    artifacts = report["artifacts"]
    assert isinstance(artifacts, list)
    artifacts[0]["source_binding"] = source_binding
    result = documentation_command_runner.CommandResult((*command, "--diagnostic-dirty"), 1, json.dumps(report), "")
    retained = check_documentation_examples._artifact_digests(
        [result], expected_name="fieldkit-cli", expected_version=Version("1.0.0")
    )
    assert retained == (artifacts if status == "diagnostic" and source_binding == "unattested-dirty" else [])


@pytest.mark.parametrize("diagnostic", [False, True])
@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "partial-invalid",
        "duplicate",
        "unsafe",
        "digest",
        "revision",
        "mixed",
        "version",
        "distribution",
        "schema",
        "failures",
        "criteria",
        "truncated",
        "deep",
        "duplicate-key",
    ],
)
def test_artifact_summary_rejects_whole_invalid_report(diagnostic: bool, defect: str) -> None:
    report = artifact_summary_fixture(diagnostic=diagnostic)
    artifacts = report["artifacts"]
    assert isinstance(artifacts, list)
    if defect == "missing":
        artifacts.pop()
    elif defect == "partial-invalid":
        artifacts[1] = None
    elif defect == "duplicate":
        artifacts[1] = artifacts[0]
    elif defect == "unsafe":
        artifacts[1]["name"] = "../fieldkit_cli-1.0.0.tar.gz"
    elif defect == "digest":
        artifacts[1]["sha256"] = "A" * 64
    elif defect in ("revision", "mixed"):
        artifacts[1]["source_revision"] = "invalid" if defect == "revision" else "c" * 40
    elif defect == "version":
        artifacts[1]["name"] = "fieldkit_cli-2.0.0.tar.gz"
    elif defect == "distribution":
        artifacts[1]["name"] = "other-1.0.0.tar.gz"
    elif defect == "schema":
        report["schema_version"] = True
    elif defect == "failures":
        report["failures"] = ["contradictory"]
    elif defect == "criteria":
        artifacts[1]["criteria"] = [{"criterion_id": "SMOKE101", "status": "pending"}]
    stdout = json.dumps(report)
    if defect == "truncated":
        stdout = stdout[:-1]
    elif defect == "deep":
        stdout = "[" * 65 + stdout + "]" * 65
    elif defect == "duplicate-key":
        stdout = stdout.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    command = documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    result = documentation_command_runner.CommandResult(
        (*command, "--diagnostic-dirty") if diagnostic else command, int(diagnostic), stdout, ""
    )
    retained = check_documentation_examples._artifact_digests(
        [result], expected_name="fieldkit-cli", expected_version=Version("1.0.0")
    )
    assert retained == []


def test_failed_dirty_summary_survives_sorted_producer_serialization() -> None:
    from scripts.check_documentation_example_scenarios import DocumentationScenarioEvidence
    from scripts.smoke_artifact import SmokeCriterion, SmokeReport

    artifacts = tuple(
        SmokeReport(1, None, name, "a" * 64, "base", "3.11", "linux", (SmokeCriterion("SMOKE101", "fail"),))
        for name in ("fieldkit_cli-1.0.0-py3-none-any.whl", "fieldkit_cli-1.0.0.tar.gz")
    )
    failures = (
        *(f"{artifact.artifact_name}:SMOKE101" for artifact in artifacts),
        "z.block:SMOKE101",
        "a.block:SMOKE101",
        "a.block:SMOKE102",
    )
    report = DocumentationScenarioEvidence(
        "fail", {"z.block": ("SMOKE101",), "a.block": ("SMOKE101", "SMOKE102")}, failures, artifacts
    ).summary()
    command = documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    result = documentation_command_runner.CommandResult(
        (*command, "--diagnostic-dirty"), 1, json.dumps(report, sort_keys=True), ""
    )
    retained = check_documentation_examples._artifact_digests(
        [result], expected_name="fieldkit-cli", expected_version=Version("1.0.0")
    )
    assert retained == report["artifacts"]


@pytest.mark.parametrize("expected_name,expected_version", [("other", "1.0.0"), ("fieldkit-cli", "2.0.0")])
def test_artifact_summary_binds_candidate_metadata(expected_name: str, expected_version: str) -> None:
    command = documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    result = documentation_command_runner.CommandResult(command, 0, json.dumps(artifact_summary_fixture()), "")
    retained = check_documentation_examples._artifact_digests(
        [result], expected_name=expected_name, expected_version=Version(expected_version)
    )
    assert retained == []


@pytest.mark.parametrize("diagnostic_dirty", [False, True])
def test_artifact_diagnostic_mode_is_explicitly_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diagnostic_dirty: bool
) -> None:
    write_example_verification_fixture(tmp_path)
    observed: list[tuple[str, ...]] = []

    def run(source: Path, command: tuple[str, ...], **_options: object) -> documentation_command_runner.CommandResult:
        observed.append(command)
        return passing_documentation_run(source, command)

    monkeypatch.setattr(check_documentation_examples, "_run", run)
    results, _pending = check_documentation_examples.check(tmp_path, diagnostic_dirty=diagnostic_dirty)
    assert results
    command = documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
    executed = (*command, "--diagnostic-dirty") if diagnostic_dirty else command
    assert executed in observed
    assert any(result.argv == executed for result in results)
    assert not diagnostic_dirty or all(result.argv != command for result in results)


@pytest.fixture(autouse=True)
def fixture_manual_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use an explicit controller fixture, never a candidate-derived registry."""
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


def test_contributor_registration_keeps_outer_commands_out_of_leaf_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.documentation_commands import CONTRIBUTOR_JOURNEY_COMMANDS

    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", _PRODUCTION_MANUAL_SCENARIOS)
    assert CONTRIBUTOR_JOURNEY_COMMANDS == (
        ("make", "bootstrap"),
        ("make", "pr-check"),
        ("uv", "run", "pytest", "tests/", "-q"),
        ("make", "quality-full"),
        ("make", "docs"),
    )
    commands = check_documentation_examples._owner_commands(Path(__file__).parents[1])
    assert not set(commands) & set(CONTRIBUTOR_JOURNEY_COMMANDS)


@pytest.mark.parametrize("phase", ["leaf", "unknown"])
def test_contributor_phase_substitution_is_rejected(phase: str) -> None:
    registry = {
        "automated.contributor-journey": {
            "mode": "automated",
            "phase": phase,
            "evidence": "make bootstrap",
        }
    }
    documents = {
        "AGENTS.md": {
            "fenced_blocks": [
                {
                    "id": "agents.md.block-1",
                    "verification_id": "automated.contributor-journey",
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="phase"):
        check_documentation_examples._automated_verification_identifiers(documents, registry)


def test_outer_contributor_page_is_pending() -> None:
    from scripts.documentation_commands import CONTRIBUTOR_JOURNEY_EVIDENCE

    commands, pending = check_documentation_examples._document_verification_plan(
        {
            "contributor_gate": {
                "paths": ["CONTRIBUTING.md"],
                "phase": "contributor_journey",
                "evidence": CONTRIBUTOR_JOURNEY_EVIDENCE,
            }
        }
    )
    assert commands == ()
    assert pending == ("document:contributor_gate:CONTRIBUTING.md",)


@pytest.mark.parametrize("subject", ["example", "document"])
def test_outer_owner_cannot_absorb_unrelated_leaf_subject(subject: str) -> None:
    from scripts.documentation_commands import CONTRIBUTOR_JOURNEY_EVIDENCE

    metadata = {"mode": "automated", "phase": "contributor_journey", "evidence": CONTRIBUTOR_JOURNEY_EVIDENCE}
    with pytest.raises(ValueError, match="unsupported"):
        if subject == "example":
            check_documentation_examples._automated_verification_identifiers(
                {
                    "README.md": {
                        "fenced_blocks": [{"id": "readme.block-1", "verification_id": "automated.contributor-journey"}]
                    }
                },
                {"automated.contributor-journey": metadata},
            )
        else:
            check_documentation_examples._document_verification_plan(
                {
                    "contributor_gate": {
                        **metadata,
                        "paths": ["README.md"],
                    }
                }
            )


@pytest.mark.parametrize("subject", ["example", "document", "identifier"])
def test_registered_outer_subject_cannot_downgrade_to_leaf(
    tmp_path: Path,
    subject: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", _PRODUCTION_MANUAL_SCENARIOS)
    path = Path(__file__).parents[1] / "docs/documentation-contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    if subject == "example":
        contract["documents"]["AGENTS.md"]["fenced_blocks"][0]["verification_id"] = "automated.installed-base-artifact"
    elif subject == "identifier":
        contract["documents"]["AGENTS.md"]["fenced_blocks"][0]["id"] = "agents.renamed"
    else:
        contract["verification"]["contributor_gate"]["paths"] = []
        contract["verification"]["first_user_guides_contract"]["paths"].append("CONTRIBUTING.md")
    target = tmp_path / "docs/documentation-contract.json"
    target.parent.mkdir()
    target.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="contributor journey"):
        check_documentation_examples._owner_commands(tmp_path)


@pytest.mark.usefixtures("prepared_diagnostic_fixture")
def test_green_leaf_checks_cannot_clear_registered_contributor_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.documentation_commands import CONTRIBUTOR_JOURNEY_EVIDENCE

    write_example_verification_fixture(tmp_path)
    path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["example_verifications"]["automated.contributor-journey"] = {
        "classification": "safe_automated_command",
        "mode": "automated",
        "phase": "contributor_journey",
        "evidence": CONTRIBUTOR_JOURNEY_EVIDENCE,
    }
    contract["documents"]["AGENTS.md"] = {
        "fenced_blocks": [
            {
                "id": "agents.md.block-1",
                "classification": "safe_automated_command",
                "verification_id": "automated.contributor-journey",
            }
        ]
    }
    contract["verification"] = {
        "contributor_gate": {
            "paths": ["CONTRIBUTING.md"],
            "phase": "contributor_journey",
            "evidence": CONTRIBUTOR_JOURNEY_EVIDENCE,
        }
    }
    path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)
    results, pending = check_documentation_examples.check(tmp_path)
    assert results and all(result.exit_code == 0 for result in results)
    assert "example-claims:agents.md.block-1" in pending
    assert "document:contributor_gate:CONTRIBUTING.md" in pending
    assert (
        check_documentation_examples.main(
            [
                "--repo-root",
                str(tmp_path),
                "--require-complete",
                "--report",
                "-",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert "example-claims:agents.md.block-1" in report["pending"]
    assert "document:contributor_gate:CONTRIBUTING.md" in report["pending"]


_STRUCTURAL_PENDING = (
    "example-claims:readme.compatibility",
    "example-claims:readme.configuration",
    "example-claims:readme.exit-codes",
    "example-claims:readme.forecast",
    "example-claims:readme.quota",
)


class _SuccessfulProcess:
    """Minimal Popen double for isolated-command environment assertions."""

    returncode = 0

    def __init__(self, argv: tuple[str, ...], **kwargs: object) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 1
        self.timeout: int | None = None
        self.stdout = BytesIO()
        self.stderr = BytesIO()

    def wait(self, timeout: int | None = None) -> int:
        self.timeout = timeout
        return self.returncode


def test_run_isolates_child_state_beside_checkout_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Automated examples do not inherit a host's temporary or config state."""
    monkeypatch.delenv("TMPDIR", raising=False)
    captured: dict[str, object] = {}

    def popen(argv: tuple[str, ...], **kwargs: object) -> _SuccessfulProcess:
        process = _SuccessfulProcess(argv, **kwargs)
        captured.update(kwargs)
        return process

    monkeypatch.setattr("scripts.documentation_command_runner.subprocess.Popen", popen)

    result = documentation_command_runner._run(tmp_path, ("example", "--check"))

    environment = captured["env"]
    assert isinstance(environment, dict)
    temporary_root = Path(environment["TMPDIR"])
    assert temporary_root.parent == tmp_path.parent
    assert environment["HOME"] == str(temporary_root / "home")
    assert environment["UV_NO_CACHE"] == "1"
    assert environment["PYTEST_ADDOPTS"] == f"-o cache_dir={temporary_root / 'pytest-cache'}"
    assert environment["PYTEST_XDIST_WORKERS"] == "4"
    assert environment["XDG_CACHE_HOME"] == str(temporary_root / "xdg-cache")
    assert environment["XDG_CONFIG_HOME"] == str(temporary_root / "xdg-config")
    assert environment["XDG_DATA_HOME"] == str(temporary_root / "xdg-data")
    assert environment["XDG_STATE_HOME"] == str(temporary_root / "xdg-state")
    assert captured["start_new_session"] is True
    assert result.exit_code == 0
    assert not temporary_root.exists()


def test_run_removes_host_credentials_and_installs_offline_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release verification cannot inherit credentials or unrestricted Python networking."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-escape")
    monkeypatch.setenv("GH_TOKEN", "must-not-escape")
    captured: dict[str, object] = {}

    def popen(argv: tuple[str, ...], **kwargs: object) -> _SuccessfulProcess:
        process = _SuccessfulProcess(argv, **kwargs)
        captured.update(kwargs)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        captured["guard_text"] = (Path(environment["PYTHONPATH"]) / "sitecustomize.py").read_text(encoding="utf-8")
        return process

    monkeypatch.setattr("scripts.documentation_command_runner.subprocess.Popen", popen)

    documentation_command_runner._run(tmp_path, ("example", "--check"))

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "GH_TOKEN" not in environment
    assert environment["UV_OFFLINE"] == "1"
    assert environment["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"
    assert environment["PIP_NO_INDEX"] == "1"
    assert "socket.socket.connect" in str(captured["guard_text"])


def test_run_offline_guard_blocks_outbound_python_network(tmp_path: Path) -> None:
    """The child-side guard fails before an automated owner reaches the network."""
    result = documentation_command_runner._run(
        tmp_path,
        (
            sys.executable,
            "-c",
            "import socket; socket.create_connection(('198.51.100.1', 443), timeout=1)",
        ),
    )

    assert result.exit_code != 0
    assert "documentation verifier blocked outbound network" in result.stderr


@pytest.mark.parametrize("owner_status", [0, 7])
def test_run_waits_for_short_lived_orphan_and_preserves_owner_status(tmp_path: Path, owner_status: int) -> None:
    program = f"""
import os,time
if os.fork():
    os._exit({owner_status})
time.sleep(0.2)
os.write(1, b'orphan finished\\n')
os._exit(0)
"""
    result = documentation_command_runner._run(tmp_path, (sys.executable, "-I", "-c", program))
    assert result.exit_code == owner_status, result.stderr
    assert result.stdout.strip() == "orphan finished"


def test_run_keeps_network_guard_active_after_owner_exit(tmp_path: Path) -> None:
    program = """
import os,socket,time
if os.fork():
    os._exit(0)
time.sleep(0.2)
sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try:
    sock.sendto(b'x', ('198.51.100.44', 9))
except OSError:
    pass
os._exit(0)
"""
    result = documentation_command_runner._run(tmp_path, (sys.executable, "-I", "-c", program))
    assert result.exit_code == 1, result.stderr
    assert "network attempt rejected" in result.stderr


@pytest.mark.parametrize("setup_result", [-1, 0])
def test_subreaper_setup_failure_never_launches_owner(setup_result: int) -> None:
    prelude = f"""
import ctypes,os
class FailedSetup:
    def prctl(self, option, *args):
        return {setup_result} if option in (36, 37) else 0
ctypes.CDLL=lambda *args, **kwargs: FailedSetup()
os.fork=lambda: (_ for _ in ()).throw(AssertionError('owner launched'))
"""
    result = subprocess.run(
        (sys.executable, "-I", "-S", "-c", prelude + documentation_command_runner._SUBREAPER_SUPERVISOR, "3", "unused"),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "subreaper setup failed" in result.stderr
    assert "owner launched" not in result.stderr


@pytest.mark.parametrize(("option", "setup_result"), [(4, -1), (3, -1), (3, 1)])
def test_supervisor_dumpability_setup_failure_never_launches_owner(option: int, setup_result: int) -> None:
    prelude = f"""
import ctypes,os
class FailedSetup:
    def prctl(self, option, *args):
        return {setup_result} if option == {option} else 0
ctypes.CDLL=lambda *args, **kwargs: FailedSetup()
os.fork=lambda: (_ for _ in ()).throw(AssertionError('owner launched'))
"""
    result = subprocess.run(
        (sys.executable, "-I", "-S", "-c", prelude + documentation_command_runner._SUBREAPER_SUPERVISOR, "3", "unused"),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "dumpability setup failed" in result.stderr
    assert "owner launched" not in result.stderr


@pytest.mark.parametrize("mode", ["rb", "r+b"])
def test_sandbox_owner_cannot_open_supervisor_memory(tmp_path: Path, mode: str) -> None:
    program = f"""
import os
try:
    stream=open('/proc/' + str(os.getppid()) + '/mem', {mode!r})
except PermissionError:
    print('supervisor memory denied')
else:
    stream.close()
    raise AssertionError('supervisor memory accessible')
"""
    result = documentation_command_runner._run(tmp_path, (sys.executable, "-I", "-c", program))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "supervisor memory denied"


@pytest.mark.parametrize("wait_result", [0, 123])
def test_subreaper_deadline_bounds_live_children_and_reaping_churn(wait_result: int) -> None:
    prelude = f"""
import ctypes,itertools,os,time
class Setup:
    def prctl(self, option, value, *args):
        if option == 37:
            value._obj.value=1
        return 0
ctypes.CDLL=lambda *args, **kwargs: Setup()
os.fork=lambda: 42
os.waitpid=lambda pid, flags: (42, 0) if pid == 42 else ({wait_result}, 0)
clock=itertools.count()
time.monotonic=lambda: next(clock)
time.sleep=lambda seconds: None
"""
    result = subprocess.run(
        (sys.executable, "-I", "-S", "-c", prelude + documentation_command_runner._SUBREAPER_SUPERVISOR, "3", "unused"),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 124
    assert "unfinished descendant" in result.stderr


def test_run_allows_spawn_resource_tracker_to_finish(tmp_path: Path) -> None:
    result = documentation_command_runner._run(
        tmp_path,
        (
            "uv",
            "run",
            "python",
            "-c",
            "import multiprocessing; semaphore=multiprocessing.get_context('spawn').Semaphore(); print('spawn finished')",
        ),
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "spawn finished"


def test_run_rejects_descendant_that_outlives_cleanup_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_command_runner, "_DESCENDANT_GRACE_SECONDS", 0.2)
    result = documentation_command_runner._run_bounded(
        tmp_path,
        (
            sys.executable,
            "-I",
            "-c",
            "import os,time; pid=os.fork(); os._exit(0) if pid else (os.setsid(), time.sleep(30))",
        ),
        timeout=10,
    )
    assert result.exit_code == 124, result.stderr
    assert "unfinished descendant" in result.stderr


def test_run_uses_tmpdir_for_its_isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller can move disposable verification state off an exhausted checkout filesystem."""
    temporary_parent = tmp_path / "temporary"
    temporary_parent.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary_parent))
    captured: dict[str, object] = {}

    def popen(argv: tuple[str, ...], **kwargs: object) -> _SuccessfulProcess:
        process = _SuccessfulProcess(argv, **kwargs)
        captured.update(kwargs)
        return process

    monkeypatch.setattr("scripts.documentation_command_runner.subprocess.Popen", popen)

    result = documentation_command_runner._run(tmp_path, ("example", "--check"))

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert Path(environment["TMPDIR"]).parent == temporary_parent
    assert environment["PYTEST_XDIST_WORKERS"] == "4"
    assert result.exit_code == 0


def test_run_gives_the_artifact_smoke_its_dedicated_bounded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slowest fixed scenario has a cap matching its clean artifact build."""
    processes: list[_SuccessfulProcess] = []

    def popen(argv: tuple[str, ...], **kwargs: object) -> _SuccessfulProcess:
        process = _SuccessfulProcess(argv, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("scripts.documentation_command_runner.subprocess.Popen", popen)
    artifact_command = documentation_commands.EXAMPLE_COMMANDS[check_documentation_examples._INSTALLED_BASE_ARTIFACT][0]

    documentation_command_runner._run(tmp_path, artifact_command)

    assert processes[0].timeout == documentation_command_runner._ARTIFACT_SCENARIO_TIMEOUT_SECONDS


def test_run_streams_only_bounded_output_tails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Large child output is drained without retaining an unbounded transcript."""

    class NoisyProcess(_SuccessfulProcess):
        def __init__(self, argv: tuple[str, ...], **kwargs: object) -> None:
            super().__init__(argv, **kwargs)
            self.stdout = BytesIO(b"x" * 100_000)
            self.stderr = BytesIO(b"y" * 100_000)

    monkeypatch.setattr("scripts.documentation_command_runner.subprocess.Popen", NoisyProcess)

    result = documentation_command_runner._run(tmp_path, ("example", "--check"))

    assert result.stdout == "x" * documentation_command_runner._OUTPUT_LIMIT
    assert result.stderr == "y" * documentation_command_runner._OUTPUT_LIMIT


def test_run_kills_the_entire_scenario_process_group_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out example cannot leave workers or artifact installers behind."""

    class TimedOutProcess(_SuccessfulProcess):
        pid = 42

        def __init__(self, argv: tuple[str, ...], **kwargs: object) -> None:
            super().__init__(argv, **kwargs)
            self.pid = 42
            self.calls = 0

        def poll(self) -> int | None:
            return None if self.calls == 1 else self.returncode

        def wait(self, timeout: int | None = None) -> int:
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(self.argv, timeout or 0)
            self.returncode = -signal.SIGKILL
            self.stdout = BytesIO(b"partial stdout")
            self.stderr = BytesIO(b"partial stderr")
            return self.returncode

    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr("scripts.documentation_command_runner.subprocess.Popen", TimedOutProcess)
    monkeypatch.setattr("scripts.documentation_command_runner.os.killpg", lambda pid, sig: killed.append((pid, sig)))

    result = documentation_command_runner._run(tmp_path, ("example", "--check"))

    assert result.exit_code == 124
    assert killed == [(42, signal.SIGKILL)]


def test_structural_examples_remain_pending_after_successful_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural checks do not prove the behavior described by a block or table."""
    write_example_verification_fixture(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)

    results, pending = check_documentation_examples.check(tmp_path)

    assert all(result.exit_code == 0 for result in results)
    assert "example-claims:readme.compatibility" in pending
    assert "example-claims:readme.forecast" in pending
    assert "example-claims:readme.installed" not in pending


def test_example_owner_rejects_evidence_that_does_not_match_executed_argv() -> None:
    """A registered owner cannot execute a different command than its evidence."""
    documents = {"README.md": {"tables": [{"id": "example", "verification_id": "automated.exit-code-contract"}]}}
    verifications = {"automated.exit-code-contract": {"mode": "automated", "evidence": "uv run pytest unrelated.py -q"}}

    with pytest.raises(ValueError, match="evidence does not match fixed argv"):
        check_documentation_examples._automated_verification_identifiers(documents, verifications)


def test_check_bounds_parallel_owners_and_preserves_failed_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owners share four resource slots without losing plan ordering or failures."""
    write_example_verification_fixture(tmp_path)
    barrier = Barrier(3, timeout=5)
    lock = Lock()
    active = 0
    peak = 0
    started = 0

    def run(repo_root: Path, argv: tuple[str, ...], **_options: object) -> documentation_command_runner.CommandResult:
        nonlocal active, peak, started
        if argv == documentation_commands.DOCUMENT_COMMANDS["source_contract"][0]:
            return passing_documentation_run(repo_root, argv)
        weight = 2 if argv == documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0] else 1
        with lock:
            active += weight
            started += 1
            ordinal = started
            peak = max(peak, active)
        if ordinal <= 3:
            barrier.wait()
        with lock:
            active -= weight
        return documentation_command_runner.CommandResult(argv, 1, "retained output", "failure")

    monkeypatch.setattr(check_documentation_examples, "_run", run)
    results, pending = check_documentation_examples.check(tmp_path)

    assert len(results) == 7
    assert peak == 4
    assert pending == (*_STRUCTURAL_PENDING, "readme.release")
    assert [result.argv for result in results] == [
        command
        for identifier in sorted(
            json.loads((tmp_path / "docs/documentation-contract.json").read_text(encoding="utf-8"))[
                "example_verifications"
            ]
        )
        if identifier.startswith("automated.")
        for command in documentation_commands.EXAMPLE_COMMANDS[identifier]
    ]
    assert all(result.exit_code == 1 and result.stdout == "retained output" for result in results)


@pytest.mark.parametrize("mutation", ["deleted", "unknown", "reclassified", "digest"])
def test_catalog_mismatch_rejected_before_any_owner_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    write_example_verification_fixture(tmp_path)
    path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    blocks = contract["documents"]["README.md"]["fenced_blocks"]
    block = blocks[-1]
    if mutation == "deleted":
        blocks.pop()
    elif mutation == "unknown":
        block["id"] = "candidate.unknown"
    elif mutation == "reclassified":
        block["verification_id"] = "automated.documentation-contract"
        block["classification"] = "structural_assertion"
    else:
        block["sha256"] = "0" * 64
    path.write_text(json.dumps(contract), encoding="utf-8")
    owner = pytest.fail
    monkeypatch.setattr(check_documentation_examples, "_run", owner)

    with pytest.raises(ValueError, match="manual scenario registry"):
        check_documentation_examples.check(tmp_path)


def test_outer_registration_retains_pending_fixed_assertions() -> None:
    assert len(OUTER_SCENARIOS) == 10
    assert len({scenario.identifier for scenario in OUTER_SCENARIOS}) == 10
    assert all(scenario.behavioral_verifier is None for scenario in OUTER_SCENARIOS)
    docs_only = next(
        scenario for scenario in OUTER_SCENARIOS if scenario.identifier == "external-contributor-docs-only"
    )
    assert docs_only.assertions == ()
    assert docs_only.argv == ("git", "diff", "--check")
    offline_base = next(scenario for scenario in OUTER_SCENARIOS if scenario.identifier == "external-user-offline-base")
    assert offline_base.argv == ("fieldkit", "doctor", "--json")
    assert offline_base.assertions == (
        documentation_commands.TranscriptAssertion(stream="stdout", contains='"configuration_state"'),
    )


def test_owner_plan_rejects_missing_registered_manual_block(tmp_path: Path) -> None:
    write_example_verification_fixture(tmp_path)
    path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["fenced_blocks"].pop()
    path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="manual scenario registry"):
        check_documentation_examples._owner_commands(tmp_path)


def test_check_runs_fixed_owner_and_reports_manual_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual blocks remain visible while fixed automated owners execute once."""
    write_example_verification_fixture(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)

    results, pending = check_documentation_examples.check(tmp_path)

    assert [result.argv for result in results] == [
        ("uv", "run", "python", "scripts/check_compatibility_policy.py"),
        (
            "uv",
            "run",
            "pytest",
            "tests/test_documentation_configuration_examples.py",
            "tests/test_config_xdg.py",
            "-q",
            "-n",
            "0",
        ),
        (
            "uv",
            "run",
            "pytest",
            "tests/test_cli_exit.py",
            "tests/test_documentation_exit_examples.py",
            "-q",
            "-n",
            "0",
        ),
        ("uv", "run", "python", "scripts/generate_dep_map.py", "--check"),
        ("uv", "run", "python", "scripts/generate_cli_docs.py", "--check"),
        ("uv", "run", "python", "scripts/check_documentation_example_scenarios.py", "--json"),
        (
            "uv",
            "run",
            "pytest",
            "tests/test_forecast.py",
            "tests/test_pipeline_quota.py",
            "tests/test_documentation_pursuit_workflow.py",
            "-k",
            "documentation",
            "-q",
            "-n",
            "0",
        ),
    ]
    assert pending == (*_STRUCTURAL_PENDING, "readme.release")


def test_automated_owner_without_fixed_command_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared automated owner cannot disappear from the executable gate."""
    write_example_verification_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["example_verifications"]["automated.unmapped"] = {
        "classification": "structural_assertion",
        "evidence": "missing fixed command",
        "mode": "automated",
    }
    contract["documents"]["README.md"]["fenced_blocks"].append(
        {"id": "readme.unmapped", "verification_id": "automated.unmapped"}
    )
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)

    with pytest.raises(ValueError, match="no fixed command"):
        check_documentation_examples.check(tmp_path)


def test_meeting_template_owner_has_one_fixed_test_command() -> None:
    """Meeting table claims execute their focused semantic assertions."""
    assert documentation_commands.EXAMPLE_COMMANDS["automated.meeting-template-contract"] == (
        (
            "uv",
            "run",
            "pytest",
            "tests/test_tool_routing_cli_first.py",
            "-k",
            "qbr_output_template or meeting_brief_template",
            "-q",
            "-n",
            "0",
        ),
    )


def test_workstream_discovery_owner_has_one_fixed_test_command() -> None:
    """The workstream output template executes its focused semantic assertions."""
    assert documentation_commands.EXAMPLE_COMMANDS["automated.workstream-discover-structure-contract"] == (
        (
            "uv",
            "run",
            "pytest",
            "tests/test_workstream_discover_skill_documentation.py",
            "-q",
            "-n",
            "0",
        ),
    )


def test_document_semantic_owner_runs_its_fixed_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Document-level semantic ownership is executed, not merely recorded."""
    write_example_verification_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["verification"] = {
        "skill_semantic_contract": {
            "evidence": "uv run python -m scripts.check_skill_documentation_contract --json",
            "paths": ["README.md"],
        }
    }
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    observed: list[tuple[str, ...]] = []

    def run(repo_root: Path, argv: tuple[str, ...], **_options: object) -> documentation_command_runner.CommandResult:
        observed.append(argv)
        return passing_documentation_run(repo_root, argv)

    monkeypatch.setattr(check_documentation_examples, "_run", run)

    check_documentation_examples.check(tmp_path)

    assert ("uv", "run", "python", "-m", "scripts.check_skill_documentation_contract", "--json") in observed


def test_document_owner_plan_is_executable_or_explicitly_pending() -> None:
    """Every supported page owner is either fixed argv or non-passing pending work."""
    contract = json.loads((Path(__file__).parents[1] / "docs/documentation-contract.json").read_text(encoding="utf-8"))

    commands, pending = check_documentation_examples._document_verification_plan(contract["verification"])

    assert commands
    assert pending
    planned = set(documentation_commands.DOCUMENT_COMMANDS)
    planned.update(check_documentation_examples._PENDING_DOCUMENT_VERIFICATIONS)
    assert planned == set(contract["verification"])
    assert all(identifier.startswith(("document:", "document-claims:")) for identifier in pending)


@pytest.mark.parametrize(
    ("owner", "path"),
    [
        ("sf_agent_instruction_contract", "src/fieldkit/sf/AGENTS.md"),
        ("llm_agent_instruction_contract", "src/fieldkit/llm/AGENTS.md"),
    ],
)
def test_domain_instruction_owner_is_unique_fixed_and_explicitly_pending(owner: str, path: str) -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    verification = contract["verification"]
    assert owner in verification
    assert [name for name, entry in verification.items() if path in entry["paths"]] == [owner]
    commands, pending = check_documentation_examples._document_verification_plan({owner: verification[owner]})

    assert commands == documentation_commands.DOCUMENT_COMMANDS[owner]
    assert pending == (f"document-claims:{owner}:{path}",)


def test_privacy_page_owner_is_unique_fixed_and_explicitly_pending() -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    owner = "privacy_guide_contract"
    verification = contract["verification"]
    assert [name for name, entry in verification.items() if "docs/privacy.md" in entry["paths"]] == [owner]

    commands, pending = check_documentation_examples._document_verification_plan({owner: verification[owner]})

    assert commands == documentation_commands.DOCUMENT_COMMANDS[owner]
    assert commands[0][3] == "tests/test_privacy_guide_contract.py"
    assert commands[0][-3:] == ("-q", "-n", "0")
    assert pending == ("document-claims:privacy_guide_contract:docs/privacy.md",)


@pytest.mark.parametrize(
    "owner",
    [
        "adr_architecture_contract",
        "adr_persistence_release_contract",
        "public_concepts_design_contract",
        "overview_page_contract",
        "integrations_page_contract",
        "contributor_patterns_contract",
        "threat_model_contract",
        "shadowbot_auth_guide_contract",
        "ingest_agent_instruction_contract",
        "pursuit_agent_instruction_contract",
        "watch_agent_instruction_contract",
        "gmail_command_readme_contract",
    ],
)
def test_new_page_owner_keeps_unproven_claims_pending(owner: str) -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    entry = contract["verification"][owner]

    commands, pending = check_documentation_examples._document_verification_plan({owner: entry})

    assert commands == documentation_commands.DOCUMENT_COMMANDS[owner]
    assert pending == tuple(f"document-claims:{owner}:{path}" for path in entry["paths"])


def test_new_fixed_page_owner_is_pending_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = "future_page_contract"
    argv = ("uv", "run", "pytest", "tests/test_future_page.py", "-q", "-n", "0")
    monkeypatch.setitem(documentation_commands.DOCUMENT_COMMANDS, owner, (argv,))
    monkeypatch.setitem(documentation_commands.OWNER_PHASES, owner, "leaf")

    commands, pending = check_documentation_examples._document_verification_plan(
        {owner: {"evidence": " ".join(argv), "paths": ["docs/future-page.md"]}}
    )

    assert commands == (argv,)
    assert pending == ("document-claims:future_page_contract:docs/future-page.md",)


def test_whole_page_qualification_is_a_reviewed_subset_of_fixed_owners() -> None:
    assert not (
        check_documentation_examples._QUALIFIED_DOCUMENT_VERIFICATIONS - set(documentation_commands.DOCUMENT_COMMANDS)
    )


def test_source_contract_is_only_a_structural_control_not_a_page_owner() -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))

    assert contract["verification"]["source_contract"]["paths"] == []


@pytest.mark.parametrize(
    ("owner", "paths", "module", "count"),
    [
        (
            "first_user_guides_contract",
            ("README.md", "docs/getting-started.md"),
            "tests/test_first_user_guides_contract.py",
            43,
        ),
        ("sf_auth_guide_contract", ("docs/guides/salesforce-auth.md",), "tests/test_sf_auth_guide_contract.py", 16),
        ("gmail_guide_contract", ("docs/guides/gmail.md",), "tests/test_gmail_guide_contract.py", 20),
    ],
)
def test_pending_guide_owner_has_literal_complete_evidence(
    owner: str, paths: tuple[str, ...], module: str, count: int
) -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    verification = contract["verification"]
    assert all([name for name, entry in verification.items() if path in entry["paths"]] == [owner] for path in paths)
    commands, pending = check_documentation_examples._document_verification_plan({owner: verification[owner]})
    argv = commands[0]
    assert commands == documentation_commands.DOCUMENT_COMMANDS[owner]
    assert argv[:4] == ("uv", "run", "pytest", module)
    assert len(argv[3:-3]) == count
    assert len(set(argv[3:-3])) == count
    assert argv[-3:] == ("-q", "-n", "0")
    assert pending == tuple(f"document-claims:{owner}:{path}" for path in paths)


def test_pending_guide_selectors_match_frozen_inventories() -> None:
    from tests.test_first_user_guides_contract import FIRST_USER_GUIDE_NODES
    from tests.test_gmail_guide_contract import MANIFEST as GMAIL_GUIDE_NODES
    from tests.test_sf_auth_guide_contract import SF_AUTH_GUIDE_NODES

    assert documentation_commands.DOCUMENT_COMMANDS["first_user_guides_contract"][0][3:-3] == FIRST_USER_GUIDE_NODES
    assert documentation_commands.DOCUMENT_COMMANDS["sf_auth_guide_contract"][0][3:-3] == SF_AUTH_GUIDE_NODES
    assert documentation_commands.DOCUMENT_COMMANDS["gmail_guide_contract"][0][3:-3] == GMAIL_GUIDE_NODES
    assert "artifact_smoke" not in documentation_commands.DOCUMENT_COMMANDS
    assert "artifact_smoke" not in check_documentation_examples._QUALIFIED_DOCUMENT_VERIFICATIONS


def test_gmail_guide_owner_preserves_example_and_manual_routes_with_scoped_sources() -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    guide = contract["documents"]["docs/guides/gmail.md"]
    blocks = guide["fenced_blocks"]
    assert len(blocks) == 13
    assert [block["verification_id"] for block in blocks] == [
        "manual.credentialed-integration",
        "manual.credentialed-integration",
        "manual.credentialed-integration",
        "manual.credentialed-integration",
        "automated.installed-base-artifact",
        "automated.gmail-example-contract",
        "automated.installed-base-artifact",
        "automated.installed-base-artifact",
        "manual.credentialed-integration",
        "manual.credentialed-integration",
        "automated.installed-base-artifact",
        "manual.credentialed-integration",
        "automated.gmail-import-scenario",
    ]
    assert all(
        block["classification"] == "credentialed_manual_integration"
        for block in blocks
        if block["verification_id"] == "manual.credentialed-integration"
    )
    assert all(
        block["classification"] == "safe_automated_command"
        for block in blocks
        if block["verification_id"] in {"automated.installed-base-artifact", "automated.gmail-import-scenario"}
    )
    assert (
        next(block for block in blocks if block["verification_id"] == "automated.gmail-example-contract")[
            "classification"
        ]
        == "structural_assertion"
    )
    assert "docs/guides/gmail.md" not in contract["verification"]["source_contract"]["paths"]
    sources = set(guide["sources"])
    from tests.test_gmail_guide_contract import MANIFEST

    assert {selector.split("::")[0] for selector in MANIFEST} <= sources
    assert {
        "tests/conftest.py",
        "tests/documentation_workflow_support.py",
        "scripts/smoke_artifact.py",
        "src/fieldkit/gmail/schema.sql",
        "src/fieldkit/ingest/schema.sql",
        "src/fieldkit/commands/_sqlite_snapshot.py",
        "src/fieldkit/_sqlite_snapshot_worker.py",
        "docs/release-readiness/dependency-ownership.json",
        "docs/release-readiness/dependency-security-policy.json",
        "pyproject.toml",
        "uv.lock",
    } <= sources
    assert "scripts/**" not in sources
    assert "tests/**" not in sources


def test_generic_artifact_page_owner_is_rejected() -> None:
    with pytest.raises(ValueError, match="no execution plan"):
        check_documentation_examples._document_verification_plan(
            {
                "artifact_smoke": {
                    "evidence": "uv run python scripts/check_documentation_example_scenarios.py --json",
                    "paths": ["README.md"],
                }
            }
        )


def test_installed_artifact_fences_keep_their_example_owner() -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    fences = [
        block
        for path in ("README.md", "docs/getting-started.md")
        for block in contract["documents"][path]["fenced_blocks"]
        if block["verification_id"] == "automated.installed-base-artifact"
    ]
    assert len(fences) == 7
    assert all(block["classification"] == "safe_automated_command" for block in fences)
    assert documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"]


@pytest.mark.parametrize("owner", ["first_user_guides_contract", "sf_auth_guide_contract", "gmail_guide_contract"])
@pytest.mark.parametrize("mutation", ["drop", "substitute", "shell"])
def test_pending_guide_owner_rejects_changed_evidence(owner: str, mutation: str) -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    entry = dict(contract["verification"][owner])
    argv = list(documentation_commands.DOCUMENT_COMMANDS[owner][0])
    if mutation == "drop":
        argv.pop(3)
    elif mutation == "substitute":
        argv[3] = "tests/test_config_xdg.py"
    else:
        argv.append(";")
    entry["evidence"] = " ".join(argv)
    with pytest.raises(ValueError, match="does not match fixed argv"):
        check_documentation_examples._document_verification_plan({owner: entry})


@pytest.mark.parametrize("mutation", ["drop-owner-test", "substitute-test", "add-shell-token"])
def test_privacy_owner_rejects_incomplete_or_substituted_evidence(mutation: str) -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    entry = dict(contract["verification"]["privacy_guide_contract"])
    argv = list(documentation_commands.DOCUMENT_COMMANDS["privacy_guide_contract"][0])
    if mutation == "drop-owner-test":
        argv.pop(3)
    elif mutation == "substitute-test":
        argv[3] = "tests/test_config_xdg.py"
    else:
        argv.append(";")
    entry["evidence"] = " ".join(argv)

    with pytest.raises(ValueError, match="does not match fixed argv"):
        check_documentation_examples._document_verification_plan({"privacy_guide_contract": entry})


@pytest.mark.parametrize(
    "owner",
    [
        "configuration_contract",
        "watcher_guide_contract",
        "pipeline_workflow_guide_contract",
        "init_guide_contract",
        "troubleshooting_guide_contract",
        "morning_brief_guide_contract",
    ],
)
def test_qualified_page_owner_requires_its_complete_fixed_command(owner: str) -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    commands, pending = check_documentation_examples._document_verification_plan(
        {owner: contract["verification"][owner]}
    )

    assert commands == documentation_commands.DOCUMENT_COMMANDS[owner]
    assert pending == ()


@pytest.mark.parametrize(
    ("owner", "paths"),
    [
        ("first_user_guides_contract", ["README.md", "docs/getting-started.md"]),
        ("gmail_guide_contract", ["docs/guides/gmail.md"]),
    ],
)
def test_pending_owner_retains_fixed_commands_without_qualifying_whole_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, paths: list[str]
) -> None:
    """Passing fixed commands cannot close unproven whole-page claims."""
    verification = {
        owner: {
            "evidence": " ".join(documentation_commands.DOCUMENT_COMMANDS[owner][0]),
            "paths": paths,
        }
    }
    commands, pending = check_documentation_examples._document_verification_plan(verification)

    assert commands == documentation_commands.DOCUMENT_COMMANDS[owner]
    assert pending == tuple(sorted(f"document-claims:{owner}:{path}" for path in paths))

    write_example_verification_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["verification"] = verification
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)

    results, observed_pending = check_documentation_examples.check(tmp_path)

    assert all(result.exit_code == 0 for result in results)
    assert sum(result.argv == commands[0] for result in results) == 1
    assert set(pending) <= set(observed_pending)
    assert "example-claims:readme.installed" not in observed_pending
    assert "readme.release" in observed_pending


def test_driver_document_owner_has_fixed_argv_and_pending_behavioral_claims() -> None:
    evidence = "uv run pytest tests/test_driver_executor_documentation.py tests/test_driver_prompt_contract.py tests/test_driver_opencode.py tests/test_driver_change_authority.py tests/test_driver_done_check_executor.py -q -n 0"
    commands, pending = check_documentation_examples._document_verification_plan(
        {"driver_executor_contract": {"evidence": evidence, "paths": ["src/fieldkit/_data/driver-executor.md"]}}
    )

    assert commands == (tuple(evidence.split()),)
    assert pending == ("document-claims:driver_executor_contract:src/fieldkit/_data/driver-executor.md",)


def test_opened_semantic_page_remains_pending_without_claim_assertion_proof() -> None:
    """Opening a page while asserting unrelated text cannot close its behavioral claims."""
    verification = {
        "skill_semantic_contract": {
            "evidence": "uv run python -m scripts.check_skill_documentation_contract --json",
            "paths": ["src/fieldkit/skills/example/SKILL.md"],
        }
    }

    commands, pending = check_documentation_examples._document_verification_plan(verification)

    assert commands == documentation_commands.DOCUMENT_COMMANDS["skill_semantic_contract"]
    assert pending == ("document-claims:skill_semantic_contract:src/fieldkit/skills/example/SKILL.md",)


def test_document_owner_rejects_evidence_text_that_differs_from_fixed_argv() -> None:
    """A prose registry value cannot name one command while the gate runs another."""
    verification = {
        "generated_reference": {
            "evidence": "uv run python scripts/not-the-generator.py --check",
            "paths": ["docs/cli-reference.md"],
        }
    }

    with pytest.raises(ValueError, match="does not match fixed argv"):
        check_documentation_examples._document_verification_plan(verification)


@pytest.mark.usefixtures("prepared_diagnostic_fixture")
def test_main_fails_completion_when_evidence_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Release completion cannot convert an explicit pending proof into a pass."""
    write_example_verification_fixture(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)

    result = check_documentation_examples.main(["--repo-root", str(tmp_path), "--require-complete", "--report", "-"])

    captured = capsys.readouterr()
    assert result == 1
    report = json.loads(captured.out)
    assert report["status"] == "fail"
    assert len(report["pending"]) == 6
    assert "readme.release" in report["pending"]
    assert "FAIL:" in captured.err
    assert "--diagnostic-dirty" in captured.err


@pytest.mark.usefixtures("prepared_diagnostic_fixture")
def test_main_never_accepts_dirty_results_as_release_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even otherwise complete results from an unbound worktree remain diagnostic."""
    write_example_verification_fixture(tmp_path)

    def bound_results(
        *_args: object, **kwargs: object
    ) -> tuple[list[documentation_command_runner.CommandResult], tuple[str, ...]]:
        commands = kwargs["execution_commands"]
        assert isinstance(commands, tuple)
        artifact_command = documentation_commands.EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]
        return [
            passing_documentation_run(
                tmp_path, (*command, "--diagnostic-dirty") if command == artifact_command else command
            )
            for command in commands
        ], ()

    monkeypatch.setattr(check_documentation_examples, "check", bound_results)

    result = check_documentation_examples.main(["--repo-root", str(tmp_path), "--require-complete", "--report", "-"])

    captured = capsys.readouterr()
    assert result == 1
    report = json.loads(captured.out)
    assert report["status"] == "fail"
    assert report["candidate"]["clean"] is False
    assert report["candidate"]["evidence_kind"] == "diagnostic"
    assert "--diagnostic-dirty" in captured.err


@pytest.mark.usefixtures("prepared_diagnostic_fixture")
def test_main_preserves_pending_manual_evidence_when_dirty_owner_is_nonpassing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-final check does not misrepresent pending proofs as passing."""
    write_example_verification_fixture(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)

    result = check_documentation_examples.main(["--repo-root", str(tmp_path), "--report", "-"])

    captured = capsys.readouterr()
    assert result == 1
    assert "--diagnostic-dirty" in captured.err
    report = json.loads(captured.out)
    assert report["status"] == "fail"
    assert len(report["pending"]) == 6
    assert "readme.release" in report["pending"]
    assert report["candidate"]["evidence_kind"] == "diagnostic"
    assert report["candidate"]["clean"] is False
    assert (
        report["candidate"]["documentation_contract_sha256"]
        == sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    )
    assert report["artifacts"] == artifact_summary_fixture(diagnostic=True)["artifacts"]


def test_check_reports_manual_table_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A structured table needs the same final rehearsal as a manual command block."""
    write_example_verification_fixture(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["tables"] = [
        {
            "id": "readme.table",
            "classification": "exact_release_cutover_proof",
            "verification_id": "manual.release-cutover",
            "sha256": "b" * 64,
        }
    ]
    monkeypatch.setattr(
        documentation_manual,
        "MANUAL_SCENARIOS",
        (
            *documentation_manual.MANUAL_SCENARIOS,
            ManualScenario(
                identifier="readme.table",
                document="README.md",
                field="tables",
                sha256="b" * 64,
                proof_type="release-cutover",
                precondition_id="fixture-release-table",
            ),
        ),
    )
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)

    _, pending = check_documentation_examples.check(tmp_path)

    assert pending == (*_STRUCTURAL_PENDING, "readme.release", "readme.table")


def test_check_binds_contract_and_every_owner_to_explicit_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every child receives the same runtime and immutable candidate identity."""
    write_example_verification_fixture(tmp_path)
    runtime_root = tmp_path / "runtime"
    revision = "a" * 40
    observed: list[tuple[tuple[str, ...], object, object]] = []

    def run(repo_root: Path, argv: tuple[str, ...], **options: object) -> documentation_command_runner.CommandResult:
        observed.append((argv, options.get("runtime_root"), options.get("source_revision")))
        return passing_documentation_run(repo_root, argv)

    monkeypatch.setattr(check_documentation_examples, "_run", run)

    results, _ = check_documentation_examples.check(
        tmp_path,
        runtime_root=runtime_root,
        source_revision=revision,
    )

    assert results
    assert observed
    assert observed[0][0] == documentation_commands.DOCUMENT_COMMANDS["source_contract"][0]
    assert all(runtime == runtime_root and source == revision for _, runtime, source in observed)


def test_main_fails_when_clean_source_changes_during_snapshot_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fixed snapshot cannot conceal concurrent changes to its source checkout."""
    write_example_verification_fixture(tmp_path)
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "."),
        ("git", "-c", "user.name=fieldkit Test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate"),
    ):
        subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True, timeout=10)

    def changed_source(
        _snapshot: Path,
        _runtime_root: Path,
        _revision: str,
        commands: tuple[tuple[str, ...], ...],
        _uv: documentation_candidate_execution.TrustedUv,
        _python: documentation_candidate_execution.TrustedInterpreter,
        _runner: documentation_candidate_execution.SupervisedRunner,
        **_options: object,
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        (tmp_path / "changed.md").write_text("changed\n", encoding="utf-8")
        return [{"argv": command, "exit_code": 0, "stdout": "", "stderr": ""} for command in commands], ()

    monkeypatch.setattr(documentation_candidate_execution, "_run_bound_plan", changed_source)
    monkeypatch.setattr(
        documentation_candidate_execution,
        "_trusted_uv",
        lambda _digest: nullcontext(documentation_candidate_execution.TrustedUv(-1, "a" * 64, 1)),
    )
    monkeypatch.setattr(
        documentation_candidate_execution,
        "_trusted_interpreter",
        lambda: nullcontext(documentation_candidate_execution.TrustedInterpreter(-1, "b" * 64, 2)),
    )

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--report",
            "-",
            "--trusted-uv-sha256",
            "a" * 64,
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 1
    assert report["status"] == "fail"
    assert report["candidate"]["immutable_snapshot"] is True
    assert report["candidate"]["source_unchanged"] is False
    assert report["candidate"]["clean"] is False
    assert "source changed during verification" in captured.err


@pytest.mark.usefixtures("prepared_diagnostic_fixture")
def test_main_propagates_automated_owner_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed fixed scenario fails the documentation example gate."""
    write_example_verification_fixture(tmp_path)

    def failing_run(
        repo_root: Path, argv: tuple[str, ...], **_options: object
    ) -> documentation_command_runner.CommandResult:
        exit_code = 0 if argv == documentation_commands.DOCUMENT_COMMANDS["source_contract"][0] else 1
        return documentation_command_runner.CommandResult(argv=argv, exit_code=exit_code, stdout="bad", stderr="")

    monkeypatch.setattr(check_documentation_examples, "_run", failing_run)

    result = check_documentation_examples.main(["--repo-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "generate_cli_docs.py --check" in captured.err


def test_integration_owners_share_exact_existing_page_command() -> None:
    expected = (
        "uv",
        "run",
        "pytest",
        "tests/test_integrations_page_contract.py",
        "tests/test_documentation_integration_profiles.py",
        "tests/test_installation_profiles.py",
        "-q",
        "-n",
        "0",
    )
    page_commands = documentation_commands.DOCUMENT_COMMANDS["integrations_page_contract"]
    profile_commands = documentation_commands.EXAMPLE_COMMANDS["automated.integration-profile-contract"]

    assert page_commands == (expected,)
    assert profile_commands == page_commands
    assert profile_commands[0] is page_commands[0]


def test_integration_owners_execute_once_without_qualifying_page_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", _PRODUCTION_MANUAL_SCENARIOS)
    root = Path(__file__).parents[1]
    contract = json.loads((root / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    owner = "integrations_page_contract"

    commands = check_documentation_examples._owner_commands(root)
    page_commands, pending = check_documentation_examples._document_verification_plan(
        {owner: contract["verification"][owner]}
    )

    assert commands.count(page_commands[0]) == 1
    assert pending == ("document-claims:integrations_page_contract:docs/integrations.md",)


def test_expensive_owner_priorities_are_unique_and_preserve_integration_position() -> None:
    commands = check_documentation_examples._EXPENSIVE_OWNER_COMMANDS

    assert len(commands) == len(set(commands))
    assert commands[2] == documentation_commands.DOCUMENT_COMMANDS["integrations_page_contract"][0]


def test_document_sources_cover_every_owned_pytest_module() -> None:
    root = Path(__file__).parents[1]
    contract = json.loads((root / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    registry = {**documentation_commands.DOCUMENT_COMMANDS, **documentation_commands.EXAMPLE_COMMANDS}
    missing: list[tuple[str, str, str]] = []
    checked = 0
    for path, document in contract["documents"].items():
        owners = {owner for owner, entry in contract["verification"].items() if path in entry["paths"]}
        owners.update(
            block["verification_id"] for field in ("fenced_blocks", "tables") for block in document.get(field, [])
        )
        sources = {
            source.relative_to(root).as_posix()
            for source in check_documentation_contract._source_files(root, document["sources"])
        }
        for owner in sorted(owners):
            for argv in registry.get(owner, ()):
                if argv[:3] != ("uv", "run", "pytest"):
                    continue
                modules = {
                    selector.split("::")[0]
                    for selector in argv[3:]
                    if selector.startswith("tests/") and selector.split("::")[0].endswith(".py")
                }
                checked += len(modules)
                missing.extend((path, owner, module) for module in sorted(modules - sources))

    assert checked > 0
    assert missing == []
