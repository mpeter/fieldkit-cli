"""Tests for fixed documentation-example verification routing."""

import json
import signal
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from scripts import check_documentation_examples

pytestmark = pytest.mark.unit


class _SuccessfulProcess:
    """Minimal Popen double for isolated-command environment assertions."""

    returncode = 0

    def __init__(self, argv: tuple[str, ...], **kwargs: object) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 1
        self.timeout: int | None = None

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        self.timeout = timeout
        return "", ""


def _write_contract(repo: Path) -> None:
    contract = {
        "example_verifications": {
            "automated.compatibility-policy": {
                "classification": "structural_assertion",
                "evidence": "uv run python scripts/check_compatibility_policy.py",
                "mode": "automated",
            },
            "automated.generated-reference": {
                "classification": "generated_reference",
                "evidence": "canonical documentation generator",
                "mode": "automated",
            },
            "automated.generated-dependency-map": {
                "classification": "generated_reference",
                "evidence": "dependency-map documentation generator",
                "mode": "automated",
            },
            "automated.installed-base-artifact": {
                "classification": "safe_automated_command",
                "evidence": "fixed installed-artifact documentation scenarios",
                "mode": "automated",
            },
            "automated.exit-code-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_cli_exit.py -q",
                "mode": "automated",
            },
            "automated.configuration-example-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_documentation_configuration_examples.py -q",
                "mode": "automated",
            },
            "manual.release-cutover": {
                "classification": "exact_release_cutover_proof",
                "evidence": "docs/release-readiness/rehearsal-evidence.schema.json",
                "mode": "manual_evidence",
            },
        },
        "documents": {
            "README.md": {
                "fenced_blocks": [
                    {"id": "readme.compatibility", "verification_id": "automated.compatibility-policy"},
                    {"id": "readme.generated", "verification_id": "automated.generated-reference"},
                    {"id": "readme.dependency-map", "verification_id": "automated.generated-dependency-map"},
                    {"id": "readme.installed", "verification_id": "automated.installed-base-artifact"},
                    {"id": "readme.exit-codes", "verification_id": "automated.exit-code-contract"},
                    {"id": "readme.configuration", "verification_id": "automated.configuration-example-contract"},
                    {
                        "id": "readme.release",
                        "verification_id": "manual.release-cutover",
                        "sha256": "a" * 64,
                    },
                ]
            }
        },
    }
    path = repo / "docs/documentation-contract.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(contract), encoding="utf-8")
    schema_source = Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json"
    schema_path = repo / "docs/release-readiness/rehearsal-evidence.schema.json"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text(schema_source.read_text(encoding="utf-8"), encoding="utf-8")


def _passing_run(repo_root: Path, argv: tuple[str, ...]) -> check_documentation_examples.CommandResult:
    return check_documentation_examples.CommandResult(argv=argv, exit_code=0, stdout="", stderr="")


def test_run_isolates_child_state_beside_checkout_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Automated examples do not inherit a host's temporary or config state."""
    monkeypatch.delenv("TMPDIR", raising=False)
    captured: dict[str, object] = {}

    def popen(argv: tuple[str, ...], **kwargs: object) -> _SuccessfulProcess:
        process = _SuccessfulProcess(argv, **kwargs)
        captured.update(kwargs)
        return process

    monkeypatch.setattr(check_documentation_examples.subprocess, "Popen", popen)

    result = check_documentation_examples._run(tmp_path, ("example", "--check"))

    environment = captured["env"]
    assert isinstance(environment, dict)
    temporary_root = Path(environment["TMPDIR"])
    assert temporary_root.parent == tmp_path.parent
    assert environment["HOME"] == str(temporary_root / "home")
    assert environment["UV_NO_CACHE"] == "1"
    assert environment["PYTEST_XDIST_WORKERS"] == "4"
    assert environment["XDG_CACHE_HOME"] == str(temporary_root / "xdg-cache")
    assert environment["XDG_CONFIG_HOME"] == str(temporary_root / "xdg-config")
    assert environment["XDG_DATA_HOME"] == str(temporary_root / "xdg-data")
    assert environment["XDG_STATE_HOME"] == str(temporary_root / "xdg-state")
    assert captured["start_new_session"] is True
    assert result.exit_code == 0
    assert not temporary_root.exists()


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

    monkeypatch.setattr(check_documentation_examples.subprocess, "Popen", popen)

    result = check_documentation_examples._run(tmp_path, ("example", "--check"))

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

    monkeypatch.setattr(check_documentation_examples.subprocess, "Popen", popen)
    artifact_command = check_documentation_examples._AUTOMATED_COMMANDS[
        check_documentation_examples._INSTALLED_BASE_ARTIFACT
    ][0]

    check_documentation_examples._run(tmp_path, artifact_command)

    assert processes[0].timeout == check_documentation_examples._ARTIFACT_SCENARIO_TIMEOUT_SECONDS


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

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(self.argv, timeout or 0)
            self.returncode = -signal.SIGKILL
            return "partial stdout", "partial stderr"

    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(check_documentation_examples.subprocess, "Popen", TimedOutProcess)
    monkeypatch.setattr(check_documentation_examples.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = check_documentation_examples._run(tmp_path, ("example", "--check"))

    assert result.exit_code == 124
    assert killed == [(42, signal.SIGKILL)]


def _command_evidence(identifier: str, argv: tuple[str, ...]) -> dict[str, object]:
    """Build one fixed command transcript matching its required observations."""
    assertions = [
        {"stream": stream, "contains": text}
        for stream, text in check_documentation_examples._FINAL_REHEARSAL_ASSERTIONS[identifier]
    ]
    stdout = "\n".join(assertion["contains"] for assertion in assertions if assertion["stream"] == "stdout")
    stderr = "\n".join(assertion["contains"] for assertion in assertions if assertion["stream"] == "stderr")
    return {
        "argv": list(argv),
        "exit_code": 0,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_sha256": sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": sha256(stderr.encode()).hexdigest(),
        "assertions": assertions,
    }


def _manual_command_evidence() -> dict[str, object]:
    """Build bounded evidence for one externally executed manual documentation block."""
    stdout = "verified"
    return {
        "argv": ["fieldkit", "--help"],
        "exit_code": 0,
        "stdout": stdout,
        "stderr": "",
        "stdout_sha256": sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": sha256(b"").hexdigest(),
        "assertions": [{"stream": "stdout", "contains": stdout}],
    }


def _write_rehearsal_evidence(repo: Path, *, verified_blocks: list[str], contract_sha256: str) -> Path:
    """Write schema-valid final rehearsal evidence for a documentation proof."""
    contract = json.loads((repo / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    manual_blocks = check_documentation_examples._manual_block_proofs(contract["documents"])
    evidence = {
        "schema_version": 1,
        "subject": {
            "source_sha": "a" * 40,
            "documentation_contract_sha256": contract_sha256,
            "clean_export_tree": "b" * 40,
            "repository": "mpeter/fieldkit-cli",
            "public_commit_sha": "c" * 40,
            "workflow_name": "Cutover verification",
            "workflow_path": ".github/workflows/cutover.yml",
            "workflow_event": "push",
            "workflow_run_id": 1,
            "workflow_run_attempt": 1,
        },
        "environment": {
            "os": "test-os",
            "architecture": "test-arch",
            "python": "3.11",
            "uv": "test",
            "git": "test",
            "make": "test",
        },
        "artifacts": [
            {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "sha256": "d" * 64},
            {"name": "fieldkit_cli-1.0.0.tar.gz", "sha256": "e" * 64},
        ],
        "verified_blocks": verified_blocks,
        "scenarios": [
            {
                "id": identifier,
                "actor": actor,
                "proof_type": "release-cutover",
                "documented_block_sha256": None,
                "status": "pass",
                "started_at": "2026-09-14T00:00:00Z",
                "duration_seconds": 1,
                "verified_blocks": [],
                "commands": [_command_evidence(identifier, argv)],
            }
            for identifier, (actor, argv) in check_documentation_examples._FINAL_REHEARSAL_SCENARIOS.items()
        ]
        + [
            {
                "id": f"{proof_type}:{identifier}",
                "actor": check_documentation_examples._MANUAL_PROOF_ACTORS[proof_type],
                "proof_type": proof_type,
                "documented_block_sha256": digest,
                "status": "pass",
                "started_at": "2026-09-14T00:00:00Z",
                "duration_seconds": 1,
                "verified_blocks": [identifier],
                "commands": [_manual_command_evidence()],
            }
            for identifier, (proof_type, digest) in manual_blocks.items()
        ],
        "review": {
            "reviewer": "release-manager",
            "reviewed_at": "2026-09-14T00:00:00Z",
            "immutable_url": "https://github.com/mpeter/fieldkit-cli/actions/runs/1",
        },
    }
    path = repo / "rehearsal-evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def _write_candidate_report(repo: Path) -> Path:
    """Write retained-candidate evidence matching the rehearsal fixture exactly."""
    report = {
        "status": "pass",
        "export_manifest": {
            "source_commit": "a" * 40,
            "exported_tree": "b" * 40,
            "expected_repository": "mpeter/fieldkit-cli",
        },
        "artifact_validation": {
            "source_revision": "a" * 40,
            "artifacts": [
                {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "sha256": "d" * 64, "status": "pass"},
                {"name": "fieldkit_cli-1.0.0.tar.gz", "sha256": "e" * 64, "status": "pass"},
            ],
        },
    }
    path = repo / "candidate-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_check_runs_fixed_owner_and_reports_manual_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual blocks remain visible while fixed automated owners execute once."""
    _write_contract(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)

    results, pending = check_documentation_examples.check(tmp_path)

    assert [result.argv for result in results] == [
        ("uv", "run", "python", "scripts/check_compatibility_policy.py"),
        ("uv", "run", "pytest", "tests/test_documentation_configuration_examples.py", "-q", "-n", "4"),
        ("uv", "run", "pytest", "tests/test_cli_exit.py", "-q", "-n", "4"),
        ("uv", "run", "python", "scripts/generate_dep_map.py", "--check"),
        ("uv", "run", "python", "scripts/generate_cli_docs.py", "--check"),
        ("uv", "run", "python", "scripts/check_documentation_example_scenarios.py", "--json"),
    ]
    assert pending == ("readme.release",)


def test_main_fails_completion_when_evidence_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Release completion cannot convert an explicit pending proof into a pass."""
    _write_contract(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)

    result = check_documentation_examples.main(["--repo-root", str(tmp_path), "--require-complete"])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "PENDING (1 blocks)" in captured.err


def test_main_reports_pending_when_manual_evidence_is_not_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-final check does not misrepresent pending proofs as passing."""
    _write_contract(tmp_path)
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)

    result = check_documentation_examples.main(["--repo-root", str(tmp_path), "--report", str(report_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert "PENDING (6 automated command(s), 1 pending block(s))" in captured.out
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "pending"


def test_check_reports_manual_table_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A structured table needs the same final rehearsal as a manual command block."""
    _write_contract(tmp_path)
    contract_path = tmp_path / "docs/documentation-contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"]["README.md"]["tables"] = [
        {"id": "readme.table", "verification_id": "manual.release-cutover", "sha256": "b" * 64}
    ]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)

    _, pending = check_documentation_examples.check(tmp_path)

    assert pending == ("readme.release", "readme.table")


def test_main_propagates_automated_owner_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed fixed scenario fails the documentation example gate."""
    _write_contract(tmp_path)

    def failing_run(repo_root: Path, argv: tuple[str, ...]) -> check_documentation_examples.CommandResult:
        exit_code = 0 if argv == check_documentation_examples._CONTRACT_COMMAND else 1
        return check_documentation_examples.CommandResult(argv=argv, exit_code=exit_code, stdout="bad", stderr="")

    monkeypatch.setattr(check_documentation_examples, "_run", failing_run)

    result = check_documentation_examples.main(["--repo-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "generate_cli_docs.py --check" in captured.err


def test_complete_check_accepts_current_schema_valid_rehearsal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual proofs pass only when final evidence covers their exact block identifiers."""
    _write_contract(tmp_path)
    contract_sha256 = sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    evidence_path = _write_rehearsal_evidence(
        tmp_path, verified_blocks=["readme.release"], contract_sha256=contract_sha256
    )
    candidate_report = _write_candidate_report(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)
    monkeypatch.setattr(check_documentation_examples, "_validate_public_rehearsal", lambda subject, repository: None)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
            "--public-repository",
            str(tmp_path),
        ]
    )

    assert result == 0


def test_public_rehearsal_rejects_a_public_commit_with_the_wrong_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema-valid proof cannot substitute a different public commit tree."""
    subject = {
        "public_commit_sha": "c" * 40,
        "clean_export_tree": "b" * 40,
        "workflow_name": "Cutover verification",
        "workflow_path": ".github/workflows/cutover.yml",
        "workflow_event": "push",
        "workflow_run_id": 1,
        "workflow_run_attempt": 1,
    }

    def public_command(repository: Path, argv: tuple[str, ...]) -> str:
        assert repository == tmp_path
        if argv[:2] == ("git", "status"):
            return ""
        if argv == ("git", "rev-parse", "HEAD"):
            return "c" * 40
        if "^{commit}" in argv[-1]:
            return "c" * 40
        if "^{tree}" in argv[-1]:
            return "d" * 40
        return "https://github.com/mpeter/fieldkit-cli.git"

    monkeypatch.setattr(check_documentation_examples, "_public_command", public_command)
    git_directory = tmp_path / ".git"
    git_directory.mkdir()

    with pytest.raises(ValueError, match="clean export tree"):
        check_documentation_examples._validate_public_rehearsal(subject, tmp_path)


def test_public_rehearsal_rejects_a_dirty_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transcript from a modified clone cannot prove the clean public candidate."""
    subject = {
        "public_commit_sha": "c" * 40,
        "clean_export_tree": "b" * 40,
        "workflow_name": "Cutover verification",
        "workflow_path": ".github/workflows/cutover.yml",
        "workflow_event": "push",
        "workflow_run_id": 1,
        "workflow_run_attempt": 1,
    }

    def public_command(repository: Path, argv: tuple[str, ...]) -> str:
        assert repository == tmp_path
        if argv[:3] == ("git", "remote", "get-url"):
            return "https://github.com/mpeter/fieldkit-cli.git"
        if argv[:2] == ("git", "status"):
            return " M README.md"
        pytest.fail(f"unexpected public-proof command: {argv}")

    monkeypatch.setattr(check_documentation_examples, "_public_command", public_command)
    (tmp_path / ".git").mkdir()

    with pytest.raises(ValueError, match="clean public checkout"):
        check_documentation_examples._validate_public_rehearsal(subject, tmp_path)


def test_public_rehearsal_rejects_a_workflow_for_another_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A green CI run only counts when it ran at the asserted public commit."""
    subject = {
        "public_commit_sha": "c" * 40,
        "clean_export_tree": "b" * 40,
        "workflow_name": "Cutover verification",
        "workflow_path": ".github/workflows/cutover.yml",
        "workflow_event": "push",
        "workflow_run_id": 1,
        "workflow_run_attempt": 1,
    }

    def public_command(repository: Path, argv: tuple[str, ...]) -> str:
        assert repository == tmp_path
        if argv[:3] == ("git", "remote", "get-url"):
            return "https://github.com/mpeter/fieldkit-cli.git"
        if argv[:2] == ("git", "status"):
            return ""
        if argv == ("git", "rev-parse", "HEAD"):
            return "c" * 40
        if "^{commit}" in argv[-1]:
            return "c" * 40
        if "^{tree}" in argv[-1]:
            return "b" * 40
        if argv == ("gh", "api", "repos/mpeter/fieldkit-cli"):
            return json.dumps({"full_name": "mpeter/fieldkit-cli", "private": False, "visibility": "public"})
        return json.dumps(
            {
                "id": 1,
                "run_attempt": 1,
                "head_sha": "d" * 40,
                "status": "completed",
                "conclusion": "success",
                "name": "Cutover verification",
                "event": "push",
                "path": ".github/workflows/cutover.yml@refs/heads/main",
            }
        )

    monkeypatch.setattr(check_documentation_examples, "_public_command", public_command)
    (tmp_path / ".git").mkdir()

    with pytest.raises(ValueError, match="public cutover workflow run"):
        check_documentation_examples._validate_public_rehearsal(subject, tmp_path)


def test_complete_check_rejects_a_review_url_unrelated_to_the_cutover_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An arbitrary HTTPS URL cannot stand in for the immutable cutover receipt."""
    _write_contract(tmp_path)
    contract_sha256 = sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    evidence_path = _write_rehearsal_evidence(
        tmp_path, verified_blocks=["readme.release"], contract_sha256=contract_sha256
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["review"]["immutable_url"] = "https://example.com/evidence/1"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    candidate_report = _write_candidate_report(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)
    monkeypatch.setattr(check_documentation_examples, "_validate_public_rehearsal", lambda subject, repository: None)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
            "--public-repository",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert "review receipt" in capsys.readouterr().err


def test_complete_check_rejects_stale_or_incomplete_rehearsal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A different contract or omitted proof never converts manual work into a pass."""
    _write_contract(tmp_path)
    evidence_path = _write_rehearsal_evidence(tmp_path, verified_blocks=[], contract_sha256="0" * 64)
    candidate_report = _write_candidate_report(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "rehearsal evidence" in captured.err


def test_complete_check_rejects_evidence_for_a_different_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A same-shaped rehearsal cannot clear a different exported candidate."""
    _write_contract(tmp_path)
    contract_sha256 = sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    evidence_path = _write_rehearsal_evidence(
        tmp_path, verified_blocks=["readme.release"], contract_sha256=contract_sha256
    )
    candidate_report = _write_candidate_report(tmp_path)
    report = json.loads(candidate_report.read_text(encoding="utf-8"))
    report["export_manifest"]["source_commit"] = "f" * 40
    candidate_report.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "retained public candidate" in captured.err


def test_complete_check_rejects_ambiguous_rehearsal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Duplicate JSON keys cannot make a release proof ambiguous."""
    _write_contract(tmp_path)
    evidence_path = tmp_path / "rehearsal-evidence.json"
    evidence_path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
    candidate_report = _write_candidate_report(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "duplicate JSON key" in captured.err


def test_complete_check_rejects_an_arbitrary_rehearsal_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-authored passing scenario cannot replace a required public journey."""
    _write_contract(tmp_path)
    contract_sha256 = sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    evidence_path = _write_rehearsal_evidence(
        tmp_path, verified_blocks=["readme.release"], contract_sha256=contract_sha256
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["scenarios"][0]["id"] = "made-up-success"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    candidate_report = _write_candidate_report(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)
    monkeypatch.setattr(check_documentation_examples, "_validate_public_rehearsal", lambda subject, repository: None)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
            "--public-repository",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert "required scenarios" in capsys.readouterr().err


def test_complete_check_rejects_a_rehearsal_scenario_without_its_canonical_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scenario labels cannot substitute for the command contract they claim to prove."""
    _write_contract(tmp_path)
    contract_sha256 = sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    evidence_path = _write_rehearsal_evidence(
        tmp_path, verified_blocks=["readme.release"], contract_sha256=contract_sha256
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["scenarios"][0]["commands"][0]["argv"] = ["fieldkit", "--help"]
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    candidate_report = _write_candidate_report(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)
    monkeypatch.setattr(check_documentation_examples, "_validate_public_rehearsal", lambda subject, repository: None)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
            "--public-repository",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert "canonical command evidence" in capsys.readouterr().err


def test_complete_check_rejects_a_rehearsal_transcript_that_does_not_match_its_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recorded output is evidence only when its retained bytes match the digest."""
    _write_contract(tmp_path)
    contract_sha256 = sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    evidence_path = _write_rehearsal_evidence(
        tmp_path, verified_blocks=["readme.release"], contract_sha256=contract_sha256
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["scenarios"][0]["commands"][0]["stdout"] = "substituted"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    candidate_report = _write_candidate_report(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)
    monkeypatch.setattr(check_documentation_examples, "_validate_public_rehearsal", lambda subject, repository: None)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
            "--public-repository",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert "transcript digest" in capsys.readouterr().err


def test_complete_check_rejects_a_manual_block_attached_to_a_generic_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cutover block cannot borrow proof from an unrelated contributor journey."""
    _write_contract(tmp_path)
    contract_sha256 = sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    evidence_path = _write_rehearsal_evidence(
        tmp_path, verified_blocks=["readme.release"], contract_sha256=contract_sha256
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["scenarios"] = [
        scenario for scenario in evidence["scenarios"] if scenario["id"] != "release-cutover:readme.release"
    ]
    evidence["scenarios"][0]["verified_blocks"] = ["readme.release"]
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    candidate_report = _write_candidate_report(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", _passing_run)
    monkeypatch.setattr(check_documentation_examples, "_validate_public_rehearsal", lambda subject, repository: None)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--require-complete",
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(candidate_report),
            "--public-repository",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert "required scenarios" in capsys.readouterr().err
