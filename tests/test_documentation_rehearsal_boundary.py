"""Documentation caller boundaries; receipts are never release approval."""

import json
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
from packaging.version import Version

from scripts import (
    check_documentation_contract,
    check_documentation_examples,
    documentation_command_runner,
    documentation_commands,
    documentation_manual,
)
from scripts.documentation_candidate_execution import candidate_binding
from tests import release_approval_support
from tests.documentation_contract_support import artifact_summary_fixture, write_example_verification_fixture
from tests.documentation_contract_support import prepared_diagnostic_fixture as prepared_diagnostic_fixture
from tests.test_documentation_examples import _PRODUCTION_MANUAL_SCENARIOS, passing_documentation_run
from tests.test_documentation_examples import fixture_manual_registry as fixture_manual_registry

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "mutation,message",
    (
        ("missing", "supported set"),
        ("extra", "supported set"),
        ("evidence", "enforced route"),
        ("phase", "unsupported document verification phase"),
    ),
)
def test_document_owner_registration_rejects_incomplete_or_altered_routes(mutation: str, message: str) -> None:
    """Fixture synchronization preserves exact production owner and route rejection."""
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    verification = contract["verification"]
    if mutation == "missing":
        verification.pop("morning_brief_guide_contract")
    elif mutation == "extra":
        verification["invented-owner"] = {"evidence": "fieldkit --help", "paths": [], "phase": "leaf"}
    elif mutation == "evidence":
        verification["morning_brief_guide_contract"]["evidence"] = "fieldkit --help"
    else:
        verification["morning_brief_guide_contract"]["phase"] = "contributor_journey"

    with pytest.raises(ValueError, match=message):
        check_documentation_contract._validate_verification_ownership(
            contract, check_documentation_contract._documents(contract)
        )


@pytest.mark.parametrize("reader", ["owners", "check"])
@pytest.mark.parametrize("invalid", ["duplicate", "deep", "oversized", "symlink"])
def test_contract_intake_is_bounded_and_rejected_before_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str, invalid: str
) -> None:
    contract = tmp_path / "docs/documentation-contract.json"
    contract.parent.mkdir()
    if invalid == "duplicate":
        contract.write_bytes(b'{"synthetic-private-key":1,"synthetic-private-key":2}')
    elif invalid == "deep":
        contract.write_bytes(b"[" * 5000 + b"0" + b"]" * 5000)
    elif invalid == "oversized":
        contract.write_bytes(b" " * (5 * 1024 * 1024 + 1))
    else:
        target = tmp_path / "fictional-private-control.json"
        target.write_bytes(b"{}")
        contract.symlink_to(target)

    def reject_owner(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid contract reached automated owners")

    monkeypatch.setattr(check_documentation_examples, "_run", reject_owner)
    with pytest.raises(ValueError, match=r"^documentation contract is invalid or unavailable$") as caught:
        if reader == "owners":
            check_documentation_examples._owner_commands(tmp_path)
        else:
            check_documentation_examples.check(tmp_path)

    assert "synthetic-private-key" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_excessively_nested_artifact_output_is_nonpassing_not_an_exception() -> None:
    argv = documentation_commands.EXAMPLE_COMMANDS[check_documentation_examples._INSTALLED_BASE_ARTIFACT][0]
    result = documentation_command_runner.CommandResult(argv, 0, "[" * 5000 + "0" + "]" * 5000, "")

    artifacts = check_documentation_examples._artifact_digests(
        [result], expected_name="fieldkit-cli", expected_version=Version("1.0.0")
    )

    assert artifacts == []


@pytest.mark.parametrize("require_complete", [False, True])
@pytest.mark.usefixtures("prepared_diagnostic_fixture")
def test_actual_rehearsal_acquisition_cannot_approve_documentation_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    require_complete: bool,
) -> None:
    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", _PRODUCTION_MANUAL_SCENARIOS)
    release_approval_support.write_real_input(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)
    repo_root = tmp_path / "diagnostic-source"
    production_root = Path(__file__).parents[1]
    for relative in (
        "pyproject.toml",
        "docs/documentation-contract.json",
        "docs/release-readiness/rehearsal-evidence.schema.json",
    ):
        target = repo_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((production_root / relative).read_bytes())
    argv = [
        "--report",
        "-",
        "--repo-root",
        str(repo_root),
        "--rehearsal-evidence",
        str(tmp_path / "evidence/support/rehearsal.json"),
        "--candidate-report",
        str(tmp_path / "public-candidate/report.json"),
        "--private-candidate-report",
        str(tmp_path / "private-candidate-report.json"),
        "--rehearsal-member-root",
        str(tmp_path / "evidence"),
        "--candidate-bundle",
        str(tmp_path / "public-candidate/bundle"),
        *(["--require-complete"] if require_complete else []),
    ]

    result = check_documentation_examples.main(argv)

    assert result == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["status"] == "fail"
    assert {scenario.identifier for scenario in _PRODUCTION_MANUAL_SCENARIOS}.issubset(report["pending"])
    assert report["candidate"]["evidence_kind"] == "diagnostic"
    assert report["candidate"]["clean"] is False
    assert report["artifacts"] == artifact_summary_fixture(diagnostic=True)["artifacts"]
    assert "PASS" not in captured.out + captured.err


def test_artifact_result_retains_exact_digest_and_revision() -> None:
    """Installed-artifact observations retain their exact identity without approving proof."""
    argv = documentation_commands.EXAMPLE_COMMANDS[check_documentation_examples._INSTALLED_BASE_ARTIFACT][0]
    result = documentation_command_runner.CommandResult(
        argv,
        0,
        json.dumps(artifact_summary_fixture()),
        "",
    )

    artifacts = check_documentation_examples._artifact_digests(
        [result], expected_name="fieldkit-cli", expected_version=Version("1.0.0")
    )

    assert artifacts == artifact_summary_fixture()["artifacts"]


def test_candidate_binding_accepts_only_a_clean_exact_git_tree(tmp_path: Path) -> None:
    """Release-candidate status carries the immutable commit, tree, and contract digest."""
    write_example_verification_fixture(tmp_path)
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "."),
        ("git", "-c", "user.name=fieldkit Test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate"),
    ):
        subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True, timeout=10)

    binding = candidate_binding(tmp_path, [])

    assert binding["evidence_kind"] == "release_candidate"
    assert binding["clean"] is True
    assert isinstance(binding["source_revision"], str)
    assert isinstance(binding["source_tree"], str)
    assert (
        binding["documentation_contract_sha256"]
        == sha256((tmp_path / "docs/documentation-contract.json").read_bytes()).hexdigest()
    )


def test_diagnostic_rehearsal_cannot_remove_manual_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.rehearsal_evidence import ControlBinding, InitialExportSubject, RehearsalValidation

    write_example_verification_fixture(tmp_path)
    monkeypatch.setattr(check_documentation_examples, "_run", passing_documentation_run)
    diagnostic = RehearsalValidation(
        phase="private-candidate",
        subject=InitialExportSubject(
            private_source_sha="a" * 40,
            private_source_tree="b" * 40,
            exported_tree="c" * 40,
            documentation_contract_sha256=sha256(
                (tmp_path / "docs/documentation-contract.json").read_bytes()
            ).hexdigest(),
        ),
        control=ControlBinding(
            controller_revision="a" * 40,
            controller_set_sha256="a" * 64,
            registry_sha256="b" * 64,
            schema_sha256="c" * 64,
            toolchain_sha256="d" * 64,
            policy_sha256="e" * 64,
            selection_record_sha256="f" * 64,
        ),
        artifacts=(),
        scenarios=(),
        public=None,
        pending_scenarios=("credentialed-integration:fictional",),
        missing_scenarios=(),
        issues=(),
        schema_evaluation_performed=False,
    )

    results, pending = check_documentation_examples.check(tmp_path, rehearsal_validation=diagnostic)

    assert results
    assert "readme.release" in pending
    assert "credentialed-integration:fictional" in pending
    assert diagnostic.passing is False


def test_selected_documentation_contract_must_match_rehearsal_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests import rehearsal_support

    monkeypatch.setattr(documentation_manual, "MANUAL_SCENARIOS", _PRODUCTION_MANUAL_SCENARIOS)
    receipt, members = rehearsal_support.fixture_receipt()
    diagnostic = rehearsal_support.validate(receipt, members)
    monkeypatch.setattr(check_documentation_examples, "_selected_file_bytes", lambda path: b"{}")
    monkeypatch.setattr(check_documentation_examples, "acquire_rehearsal", lambda *args, **kwargs: diagnostic)

    with pytest.raises(ValueError, match="rehearsal input is invalid or unavailable"):
        check_documentation_examples._acquire_rehearsal_from_paths(
            tmp_path,
            evidence_path=tmp_path / "receipt.json",
            candidate_report_path=tmp_path / "report.json",
            member_root=tmp_path,
            bundle_root=tmp_path,
        )


def test_invalid_rehearsal_is_rejected_before_automated_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_example_verification_fixture(tmp_path)
    evidence_path = tmp_path / "receipt.json"
    evidence_path.write_text('{"schema_version":1}', encoding="utf-8")
    report_path = tmp_path / "candidate.json"
    report_path.write_text("{}", encoding="utf-8")

    def reject_execution(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid evidence reached documentation owner execution")

    monkeypatch.setattr(check_documentation_examples, "execute_bound_candidate", reject_execution)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--rehearsal-evidence",
            str(evidence_path),
            "--candidate-report",
            str(report_path),
            "--rehearsal-member-root",
            str(tmp_path),
            "--candidate-bundle",
            str(tmp_path),
        ]
    )

    assert result == 2
    captured = capsys.readouterr()
    assert "rehearsal input is invalid or unavailable" in captured.err
    assert str(tmp_path) not in captured.out + captured.err


@pytest.mark.parametrize("supplied", ["report", "member-root", "bundle"])
def test_rehearsal_support_inputs_without_receipt_are_rejected_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied: str, capsys: pytest.CaptureFixture[str]
) -> None:
    write_example_verification_fixture(tmp_path)
    option = {
        "report": "--candidate-report",
        "member-root": "--rehearsal-member-root",
        "bundle": "--candidate-bundle",
    }[supplied]

    def reject_execution(*args: object, **kwargs: object) -> None:
        pytest.fail("unsupported input reached documentation owner execution")

    monkeypatch.setattr(check_documentation_examples, "execute_bound_candidate", reject_execution)

    result = check_documentation_examples.main(["--repo-root", str(tmp_path), option, str(tmp_path)])

    assert result == 2
    assert "rehearsal inputs require explicit evidence" in capsys.readouterr().err


@pytest.mark.parametrize("unsafe", ["receipt", "report", "member-root", "bundle"])
def test_rehearsal_symlink_inputs_fail_closed_without_private_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str, capsys: pytest.CaptureFixture[str]
) -> None:
    write_example_verification_fixture(tmp_path)
    selected = tmp_path / "selected"
    selected.mkdir()
    receipt = selected / "receipt.json"
    report = selected / "report.json"
    receipt.write_text("{}", encoding="utf-8")
    report.write_text("{}", encoding="utf-8")
    paths = {"receipt": receipt, "report": report, "member-root": selected, "bundle": selected}
    link = tmp_path / "fictional-private-selection"
    link.symlink_to(paths[unsafe], target_is_directory=unsafe in {"member-root", "bundle"})
    paths[unsafe] = link

    def reject_execution(*args: object, **kwargs: object) -> None:
        pytest.fail("unsafe evidence reached documentation owner execution")

    monkeypatch.setattr(check_documentation_examples, "execute_bound_candidate", reject_execution)

    result = check_documentation_examples.main(
        [
            "--repo-root",
            str(tmp_path),
            "--rehearsal-evidence",
            str(paths["receipt"]),
            "--candidate-report",
            str(paths["report"]),
            "--rehearsal-member-root",
            str(paths["member-root"]),
            "--candidate-bundle",
            str(paths["bundle"]),
        ]
    )

    assert result == 2
    captured = capsys.readouterr()
    assert "rehearsal input is invalid or unavailable" in captured.err
    assert str(tmp_path) not in captured.out + captured.err


def test_rehearsal_decoder_and_public_observer_have_no_legacy_aliases() -> None:
    assert not hasattr(check_documentation_examples, "validate_retained_rehearsal_semantics")
    assert not hasattr(check_documentation_examples, "_validated_rehearsal_blocks")
    assert not hasattr(check_documentation_examples, "_validate_public_rehearsal")
    assert not hasattr(check_documentation_examples, "_public_command")
