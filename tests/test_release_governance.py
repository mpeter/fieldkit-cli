"""Contracts for candidate-bound release governance policy."""

import json
from pathlib import Path

import check_release_governance as cli
import pytest
from jsonschema import Draft202012Validator

from scripts import _release_governance as governance

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).parents[1]


def _candidate_report(*, revision: str = "a" * 40) -> dict[str, object]:
    artifacts = [
        {
            "name": "fieldkit_cli-1.0.0-py3-none-any.whl",
            "kind": "wheel",
            "sha256": "1" * 64,
        },
        {
            "name": "fieldkit_cli-1.0.0.tar.gz",
            "kind": "sdist",
            "sha256": "2" * 64,
        },
    ]
    return {
        "schema_version": 6,
        "status": "pass",
        "expected_repository": "example/fieldkit-cli",
        "package": "fieldkit-cli",
        "planned_tag": "v1.0.0",
        "export_manifest": {
            "schema_version": 1,
            "source_commit": revision,
            "source_tree": "b" * 40,
            "policy_path": "docs/release-readiness/public-tree-policy.json",
            "policy_oid": "c" * 40,
            "policy_sha256": "d" * 64,
            "expected_repository": "example/fieldkit-cli",
            "planned_tag": "v1.0.0",
            "exported_tree": "e" * 40,
            "included": [],
            "excluded": [],
        },
        "artifact_validation": {
            "schema_version": 1,
            "status": "pass",
            "source_revision": revision,
            "artifacts": [
                {
                    **artifact,
                    "criteria": [{"criterion_id": "ART001", "status": "pass", "diagnostics": []}],
                    "status": "pass",
                }
                for artifact in artifacts
            ],
        },
        "license_evidence": {
            "schema_version": 1,
            "status": "pass",
            "scope": "runtime-all-extras",
            "revision": revision,
            "export_policy_sha256": "d" * 64,
            "sbom_sha256": "e" * 64,
            "observed_packages": 0,
            "packages": [],
            "findings": [],
        },
        "runtime_requirements": {
            "name": "runtime-requirements.txt",
            "sha256": "f" * 64,
        },
        "release_build_requirements": {
            "name": "release-build-requirements.txt",
            "sha256": "e" * 64,
        },
        "runtime_wheelhouse": {"name": "runtime-wheelhouse.zip", "sha256": "a" * 64},
        "scan": {
            "schema_version": 1,
            "source_commit": revision,
            "source_tree": "b" * 40,
            "exported_tree": "e" * 40,
            "expected_repository": "example/fieldkit-cli",
            "planned_tag": "v1.0.0",
            "export_policy_oid": "c" * 40,
            "export_policy_sha256": "d" * 64,
            "scan_policy_oid": "f" * 40,
            "scan_policy_sha256": "a" * 64,
            "identity_policy_oid": "b" * 40,
            "identity_policy_sha256": "c" * 64,
            "scanned_entries": 1,
            "scanned_artifact_entries": 2,
            "scanned_text_entries": 1,
            "approved_binary_entries": 0,
            "classified_matches": 0,
            "gitleaks_version": "8.30.1",
            "gitleaks_findings": 0,
            "artifacts": [{**artifact, "member_count": 1, "total_uncompressed_bytes": 1} for artifact in artifacts],
            "findings": [],
            "status": "pass",
            "artifact_coverage": "pass",
        },
    }


def _policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate": {
            "repository": "example/fieldkit-cli",
            "package": "fieldkit-cli",
            "planned_tag": "v1.0.0",
        },
        "roles": {
            "preparer": "release-maintainer",
            "approver": "release-owner",
            "incident": "release-maintainer",
        },
        "support": {
            "compatibility_policy": "docs/release-readiness/compatibility-policy.json",
            "supported_line": "1.x best-effort community support",
        },
        "external_controls": [
            {"id": "pypi-trusted-publisher", "status": "pending", "evidence": None},
            {"id": "github-release-environment", "status": "pending", "evidence": None},
        ],
    }


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_pending_controls_are_valid_but_do_not_authorize_publication(tmp_path: Path) -> None:
    """Unratified live controls remain visible instead of silently passing."""
    policy_path = _write(tmp_path / "governance.json", _policy())
    candidate_path = _write(tmp_path / "candidate.json", _candidate_report())

    report = governance.validate(policy_path, candidate_path)

    assert report.status == "pending"
    assert report.publication_authorized is False
    assert report.pending_controls == ("github-release-environment", "pypi-trusted-publisher")


def test_candidate_report_rejects_an_unbound_runtime_requirements_receipt(tmp_path: Path) -> None:
    policy_path = _write(tmp_path / "governance.json", _policy())
    candidate = _candidate_report()
    candidate["runtime_requirements"] = {"name": "other.txt", "sha256": "f" * 64}
    candidate_path = _write(tmp_path / "candidate.json", candidate)

    with pytest.raises(ValueError, match="runtime requirements filename"):
        governance.validate(policy_path, candidate_path)


def test_checked_in_policy_declares_the_intended_first_public_release() -> None:
    """The portable policy carries no operator identity or invented live proof."""
    policy = governance.load_policy(_REPO_ROOT / "docs/release-readiness/release-governance-policy.json")

    assert policy.candidate.repository.endswith("/fieldkit-cli")
    assert policy.candidate.package == "fieldkit-cli"
    assert policy.candidate.planned_tag == "v1.0.0"
    assert all(control.status == "pending" for control in policy.controls)


def test_checked_in_policy_matches_its_published_schema() -> None:
    """The portable schema remains a mechanically accurate policy contract."""
    policy = json.loads(
        (_REPO_ROOT / "docs/release-readiness/release-governance-policy.json").read_text(encoding="utf-8")
    )
    schema = json.loads(
        (_REPO_ROOT / "docs/release-readiness/release-governance-policy.schema.json").read_text(encoding="utf-8")
    )

    assert list(Draft202012Validator(schema).iter_errors(policy)) == []


def test_schema_rejects_evidenced_control_without_evidence() -> None:
    """Schema-only tooling cannot accept a policy the runtime rejects."""
    policy = _policy()
    controls = policy["external_controls"]
    assert isinstance(controls, list)
    control = controls[0]
    assert isinstance(control, dict)
    control["status"] = "evidenced"
    schema = json.loads(
        (_REPO_ROOT / "docs/release-readiness/release-governance-policy.schema.json").read_text(encoding="utf-8")
    )

    errors = list(Draft202012Validator(schema).iter_errors(policy))

    assert errors


def test_cli_reports_pending_as_a_nonzero_stop_condition(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Automation can distinguish valid incomplete governance from malformed evidence."""
    policy_path = _write(tmp_path / "governance.json", _policy())
    candidate_path = _write(tmp_path / "candidate.json", _candidate_report())

    result = cli.main(["--policy", str(policy_path), "--candidate-report", str(candidate_path)])

    assert result == 1
    assert json.loads(capsys.readouterr().out)["status"] == "pending"


def test_valid_evidenced_controls_authorize_only_the_same_candidate(tmp_path: Path) -> None:
    """Every passing external control is attributable to the verified candidate."""
    policy = _policy()
    controls = policy["external_controls"]
    assert isinstance(controls, list)
    identity = {
        "repository": "example/fieldkit-cli",
        "revision": "a" * 40,
        "package": "fieldkit-cli",
        "planned_tag": "v1.0.0",
    }
    for control in controls:
        assert isinstance(control, dict)
        control["status"] = "evidenced"
        control["evidence"] = {"candidate": identity, "record": "https://example.com/evidence"}
    policy_path = _write(tmp_path / "governance.json", policy)
    candidate_path = _write(tmp_path / "candidate.json", _candidate_report())

    report = governance.validate(policy_path, candidate_path)

    assert report.status == "pass"
    assert report.publication_authorized is True
    assert report.pending_controls == ()


def test_unknown_policy_field_fails_closed(tmp_path: Path) -> None:
    """A typo cannot quietly weaken the governed release contract."""
    policy = _policy()
    policy["unexpected"] = True

    with pytest.raises(ValueError, match="keys must be exactly"):
        governance.validate(
            _write(tmp_path / "governance.json", policy), _write(tmp_path / "candidate.json", _candidate_report())
        )


def test_ambiguous_role_assignment_fails_closed(tmp_path: Path) -> None:
    """Each responsibility must resolve to exactly one named role."""
    policy = _policy()
    roles = policy["roles"]
    assert isinstance(roles, dict)
    roles["approver"] = ["release-owner", "backup-owner"]

    with pytest.raises(ValueError, match=r"roles\.approver must be a non-empty string"):
        governance.validate(
            _write(tmp_path / "governance.json", policy), _write(tmp_path / "candidate.json", _candidate_report())
        )


def test_evidenced_control_with_stale_candidate_identity_fails(tmp_path: Path) -> None:
    """Evidence cannot be reused after the candidate revision changes."""
    policy = _policy()
    controls = policy["external_controls"]
    assert isinstance(controls, list)
    control = controls[0]
    assert isinstance(control, dict)
    control["status"] = "evidenced"
    control["evidence"] = {
        "candidate": {
            "repository": "example/fieldkit-cli",
            "revision": "b" * 40,
            "package": "fieldkit-cli",
            "planned_tag": "v1.0.0",
        },
        "record": "https://example.com/evidence",
    }

    with pytest.raises(ValueError, match="does not bind the policy candidate"):
        governance.validate(
            _write(tmp_path / "governance.json", policy), _write(tmp_path / "candidate.json", _candidate_report())
        )


def test_policy_cannot_omit_a_required_external_control(tmp_path: Path) -> None:
    """Every release authorization requires both independently declared controls."""
    policy = _policy()
    controls = policy["external_controls"]
    assert isinstance(controls, list)
    controls.pop()

    with pytest.raises(ValueError, match="external control ids must be exactly"):
        governance.validate(
            _write(tmp_path / "governance.json", policy), _write(tmp_path / "candidate.json", _candidate_report())
        )


def test_candidate_report_requires_complete_passing_producer_evidence(tmp_path: Path) -> None:
    """A shallow report cannot be promoted to an authorization decision."""
    candidate = _candidate_report()
    candidate.pop("scan")

    with pytest.raises(ValueError, match="candidate report keys must be exactly"):
        governance.validate(
            _write(tmp_path / "governance.json", _policy()), _write(tmp_path / "candidate.json", candidate)
        )


def test_candidate_report_package_must_match_the_policy(tmp_path: Path) -> None:
    """Control evidence cannot relabel a report built for another distribution."""
    candidate = _candidate_report()
    candidate["package"] = "other-package"

    with pytest.raises(ValueError, match="package does not bind"):
        governance.validate(
            _write(tmp_path / "governance.json", _policy()), _write(tmp_path / "candidate.json", candidate)
        )


def test_candidate_report_rejects_unvalidated_artifact_evidence(tmp_path: Path) -> None:
    """An aggregate pass cannot replace concrete wheel and source-distribution evidence."""
    candidate = _candidate_report()
    artifact_validation = candidate["artifact_validation"]
    assert isinstance(artifact_validation, dict)
    artifact_validation["artifacts"] = [{"anything": True}]

    with pytest.raises(ValueError, match="keys must be exactly"):
        governance.validate(
            _write(tmp_path / "governance.json", _policy()), _write(tmp_path / "candidate.json", candidate)
        )


def test_candidate_report_rejects_unmatched_scanned_artifacts(tmp_path: Path) -> None:
    """The content scan must cover the exact artifacts that metadata validation approved."""
    candidate = _candidate_report()
    scan = candidate["scan"]
    assert isinstance(scan, dict)
    artifacts = scan["artifacts"]
    assert isinstance(artifacts, list)
    artifact = artifacts[0]
    assert isinstance(artifact, dict)
    artifact["sha256"] = "f" * 64

    with pytest.raises(ValueError, match="scan artifacts do not match"):
        governance.validate(
            _write(tmp_path / "governance.json", _policy()), _write(tmp_path / "candidate.json", candidate)
        )


def test_candidate_report_rejects_nonstring_artifact_kind(tmp_path: Path) -> None:
    """Malformed nested values remain ordinary validation failures for the CLI boundary."""
    candidate = _candidate_report()
    artifact_validation = candidate["artifact_validation"]
    assert isinstance(artifact_validation, dict)
    artifacts = artifact_validation["artifacts"]
    assert isinstance(artifacts, list)
    artifact = artifacts[0]
    assert isinstance(artifact, dict)
    artifact["kind"] = []

    with pytest.raises(ValueError, match="kind must be wheel or sdist"):
        governance.validate(
            _write(tmp_path / "governance.json", _policy()), _write(tmp_path / "candidate.json", candidate)
        )


@pytest.mark.parametrize(
    ("section", "field", "value", "match"),
    [
        ("export_manifest", "schema_version", 2, "schema_version must be 1"),
        ("license_evidence", "scope", "development", "scope is unsupported"),
        ("license_evidence", "packages", [None], "internally passing"),
        ("scan", "source_tree", "f" * 40, "does not bind the export manifest"),
    ],
)
def test_candidate_report_rejects_internally_inconsistent_evidence(
    tmp_path: Path, section: str, field: str, value: object, match: str
) -> None:
    """Every retained producer section must agree with the same export identity."""
    candidate = _candidate_report()
    report_section = candidate[section]
    assert isinstance(report_section, dict)
    report_section[field] = value

    with pytest.raises(ValueError, match=match):
        governance.validate(
            _write(tmp_path / "governance.json", _policy()), _write(tmp_path / "candidate.json", candidate)
        )


def test_candidate_report_rejects_malformed_export_entries(tmp_path: Path) -> None:
    """The full retained export manifest stays structurally trustworthy."""
    candidate = _candidate_report()
    manifest = candidate["export_manifest"]
    assert isinstance(manifest, dict)
    manifest["included"] = [None]

    with pytest.raises(ValueError, match="must be an object"):
        governance.validate(
            _write(tmp_path / "governance.json", _policy()), _write(tmp_path / "candidate.json", candidate)
        )


def test_candidate_report_rejects_extra_artifacts(tmp_path: Path) -> None:
    """The producer contract permits exactly one wheel and one source distribution."""
    candidate = _candidate_report()
    validation = candidate["artifact_validation"]
    scan = candidate["scan"]
    assert isinstance(validation, dict) and isinstance(scan, dict)
    validation_artifacts = validation["artifacts"]
    scan_artifacts = scan["artifacts"]
    assert isinstance(validation_artifacts, list) and isinstance(scan_artifacts, list)
    validation_artifacts.append(
        {
            "name": "fieldkit_cli-extra.whl",
            "kind": "wheel",
            "sha256": "f" * 64,
            "criteria": [{"criterion_id": "ART001", "status": "pass", "diagnostics": []}],
            "status": "pass",
        }
    )
    scan_artifacts.append(
        {
            "name": "fieldkit_cli-extra.whl",
            "kind": "wheel",
            "sha256": "f" * 64,
            "member_count": 1,
            "total_uncompressed_bytes": 1,
        }
    )

    with pytest.raises(ValueError, match="one wheel and one sdist"):
        governance.validate(
            _write(tmp_path / "governance.json", _policy()), _write(tmp_path / "candidate.json", candidate)
        )


def test_cli_maps_deep_json_to_data_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Parser recursion remains inside the documented malformed-input boundary."""
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text("[" * 2_000 + "]" * 2_000, encoding="utf-8")

    result = cli.main(
        ["--policy", str(_write(tmp_path / "governance.json", _policy())), "--candidate-report", str(candidate_path)]
    )

    assert result == 3
    assert "cannot load" in capsys.readouterr().err


def test_candidate_report_must_be_a_bounded_regular_file(tmp_path: Path) -> None:
    """A local validator rejects special and unexpectedly large report inputs."""
    report_path = tmp_path / "candidate.json"
    report_path.write_bytes(b"{" + b" " * (5 * 1024 * 1024))

    with pytest.raises(ValueError, match="exceeds the"):
        governance.validate(_write(tmp_path / "governance.json", _policy()), report_path)
