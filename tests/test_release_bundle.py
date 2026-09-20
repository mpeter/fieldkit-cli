"""Contracts for materializing and verifying closed release bundles."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import release_bundle

pytestmark = pytest.mark.unit


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    wheel = "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = "fieldkit_cli-1.0.0.tar.gz"
    wheel_digest = _write(candidate / "dist" / wheel, b"wheel payload")
    sdist_digest = _write(candidate / "dist" / sdist, b"sdist payload")
    sbom_digest = _write(candidate / "locked-graph.cdx.json", b'{"components": []}\n')
    requirements_digest = _write(
        candidate / "runtime-requirements.txt",
        b"click==8.5.0 --hash=sha256:" + b"a" * 64 + b"\n",
    )
    build_requirements_digest = _write(
        candidate / "release-build-requirements.txt",
        b"hatchling==1.26.3 --hash=sha256:" + b"b" * 64 + b"\n",
    )
    wheelhouse_digest = _write(candidate / "runtime-wheelhouse.zip", b"wheelhouse payload")
    revision = "a" * 40
    report = {
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
                    "name": wheel,
                    "kind": "wheel",
                    "sha256": wheel_digest,
                    "criteria": [{"criterion_id": "ART001", "status": "pass", "diagnostics": []}],
                    "status": "pass",
                },
                {
                    "name": sdist,
                    "kind": "sdist",
                    "sha256": sdist_digest,
                    "criteria": [{"criterion_id": "ART001", "status": "pass", "diagnostics": []}],
                    "status": "pass",
                },
            ],
        },
        "license_evidence": {
            "schema_version": 1,
            "status": "pass",
            "scope": "runtime-all-extras",
            "revision": revision,
            "export_policy_sha256": "d" * 64,
            "sbom_sha256": sbom_digest,
            "observed_packages": 0,
            "packages": [],
            "findings": [],
        },
        "runtime_requirements": {
            "name": "runtime-requirements.txt",
            "sha256": requirements_digest,
        },
        "release_build_requirements": {
            "name": "release-build-requirements.txt",
            "sha256": build_requirements_digest,
        },
        "runtime_wheelhouse": {"name": "runtime-wheelhouse.zip", "sha256": wheelhouse_digest},
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
            "scan_policy_sha256": "1" * 64,
            "identity_policy_oid": "2" * 40,
            "identity_policy_sha256": "3" * 64,
            "scanned_entries": 1,
            "scanned_artifact_entries": 2,
            "scanned_text_entries": 1,
            "approved_binary_entries": 0,
            "classified_matches": 0,
            "gitleaks_version": "8.30.1",
            "gitleaks_findings": 0,
            "artifacts": [
                {
                    "name": wheel,
                    "kind": "wheel",
                    "sha256": wheel_digest,
                    "member_count": 1,
                    "total_uncompressed_bytes": 1,
                },
                {
                    "name": sdist,
                    "kind": "sdist",
                    "sha256": sdist_digest,
                    "member_count": 1,
                    "total_uncompressed_bytes": 1,
                },
            ],
            "findings": [],
            "status": "pass",
            "artifact_coverage": "pass",
        },
    }
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return candidate


def _candidate_report(candidate: Path) -> bytes:
    return (candidate / "report.json").read_bytes()


def test_materialize_creates_a_closed_digest_bound_bundle(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    report = release_bundle.materialize(candidate)

    bundle = candidate / "bundle"
    assert report.ok is True
    assert sorted(path.name for path in bundle.iterdir()) == [
        "SHA256SUMS",
        "bundle-provenance.json",
        "fieldkit_cli-1.0.0-py3-none-any.whl",
        "fieldkit_cli-1.0.0.tar.gz",
        "locked-graph.cdx.json",
        "release-build-requirements.txt",
        "runtime-requirements.txt",
        "runtime-wheelhouse.zip",
    ]
    verification = release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))
    assert verification.ok is True
    assert verification.source_commit == "a" * 40


def test_materialize_and_verify_allow_the_bounded_runtime_wheelhouse(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    wheelhouse = candidate / "runtime-wheelhouse.zip"
    wheelhouse.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    report = json.loads((candidate / "report.json").read_text(encoding="utf-8"))
    report["runtime_wheelhouse"]["sha256"] = hashlib.sha256(wheelhouse.read_bytes()).hexdigest()
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")

    release_bundle.materialize(candidate)

    assert release_bundle.verify(candidate / "bundle", candidate_report=_candidate_report(candidate)).ok is True


@pytest.mark.parametrize("mutation", ["extra", "tamper", "missing"])
def test_verify_rejects_changed_bundle_members(tmp_path: Path, mutation: str) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    if mutation == "extra":
        (bundle / "unexpected.txt").write_text("no", encoding="utf-8")
    elif mutation == "tamper":
        (bundle / "locked-graph.cdx.json").write_text("changed", encoding="utf-8")
    else:
        (bundle / "SHA256SUMS").unlink()

    with pytest.raises(ValueError, match="bundle"):
        release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


def test_materialize_rejects_runtime_requirements_that_do_not_match_the_candidate_report(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    (candidate / "runtime-requirements.txt").write_text("click==8.5.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="runtime requirements digest"):
        release_bundle.materialize(candidate)


def test_verify_rejects_a_symlinked_bundle_member(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    replacement = candidate / "replacement-sbom.json"
    replacement.write_text('{"components": []}\n', encoding="utf-8")
    (bundle / "locked-graph.cdx.json").unlink()
    (bundle / "locked-graph.cdx.json").symlink_to(replacement)

    with pytest.raises(ValueError, match="bundle"):
        release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


def test_verify_rejects_a_symlinked_bundle_root(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    linked_bundle = tmp_path / "linked-bundle"
    linked_bundle.symlink_to(candidate / "bundle", target_is_directory=True)

    with pytest.raises(ValueError, match="bundle"):
        release_bundle.verify(linked_bundle, candidate_report=_candidate_report(candidate))


def test_materialize_rejects_a_symlinked_candidate_dist_directory(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    external_dist = tmp_path / "external-dist"
    (candidate / "dist").replace(external_dist)
    (candidate / "dist").symlink_to(external_dist, target_is_directory=True)

    with pytest.raises(ValueError, match="directory"):
        release_bundle.materialize(candidate)

    assert (candidate / "bundle").exists() is False


def test_materialize_rejects_report_that_disagrees_with_sbom(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["license_evidence"]["sbom_sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="SBOM"):
        release_bundle.materialize(candidate)

    assert (candidate / "bundle").exists() is False


def test_materialize_rejects_candidate_with_failed_scan_evidence(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["scan"]["status"] = "fail"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="scan"):
        release_bundle.materialize(candidate)


def test_materialize_rejects_candidate_without_a_package_identity(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["package"] = ""
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="package"):
        release_bundle.materialize(candidate)


@pytest.mark.parametrize("mutation", ["artifact", "license"])
def test_materialize_rejects_failure_details_hidden_behind_passing_summary(tmp_path: Path, mutation: str) -> None:
    candidate = _candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if mutation == "artifact":
        report["artifact_validation"]["artifacts"][0]["criteria"][0]["status"] = "fail"
    else:
        report["license_evidence"]["findings"] = [{"id": "LICENSE001"}]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match=r"(criteria|complete contract)"):
        release_bundle.materialize(candidate)


@pytest.mark.parametrize("mutation", ["package", "scan_digest"])
def test_materialize_rejects_malformed_nested_passing_evidence(tmp_path: Path, mutation: str) -> None:
    candidate = _candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if mutation == "package":
        report["license_evidence"]["packages"] = [42]
        report["license_evidence"]["observed_packages"] = 1
    else:
        report["scan"]["scan_policy_sha256"] = "not-a-digest"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match=r"(package|policy digest)"):
        release_bundle.materialize(candidate)


def test_materialize_rejects_candidate_artifact_name_that_collides_with_bundle_metadata(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    artifact = report["artifact_validation"]["artifacts"][0]
    original = candidate / "dist" / artifact["name"]
    replacement = candidate / "dist" / "SHA256SUMS"
    original.replace(replacement)
    artifact["name"] = replacement.name
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="reserved"):
        release_bundle.materialize(candidate)

    assert (candidate / "bundle").exists() is False


def test_verify_rejects_provenance_with_an_unknown_field_even_when_rechecksummed(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    provenance_path = bundle / "bundle-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["unreviewed_claim"] = "not allowed"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    checksums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(bundle.iterdir())
        if path.name != "SHA256SUMS"
    ]
    (bundle / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")

    with pytest.raises(ValueError, match="provenance"):
        release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


def test_verify_rejects_boolean_provenance_versions_even_when_rechecksummed(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    provenance_path = bundle / "bundle-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["schema_version"] = True
    provenance["checksum_manifest"]["format_version"] = True
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    checksums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(bundle.iterdir())
        if path.name != "SHA256SUMS"
    ]
    (bundle / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")

    with pytest.raises(ValueError, match="schema"):
        release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


def test_verify_rejects_a_bundle_bound_to_a_different_candidate_report(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"

    other_candidate = _candidate(tmp_path / "other")
    other_report = _candidate_report(other_candidate) + b"\n"

    with pytest.raises(ValueError, match="expected candidate report"):
        release_bundle.verify(bundle, candidate_report=other_report)


def test_verify_rejects_rechecksummed_provenance_that_disagrees_with_candidate(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    provenance_path = bundle / "bundle-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["source_commit"] = "f" * 40
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    checksums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(bundle.iterdir())
        if path.name != "SHA256SUMS"
    ]
    (bundle / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")

    with pytest.raises(ValueError, match="expected candidate report"):
        release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


def test_verify_rejects_a_renamed_sbom_even_when_rechecksummed(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    original = bundle / "locked-graph.cdx.json"
    renamed = bundle / "renamed-sbom.json"
    original.replace(renamed)
    provenance_path = bundle / "bundle-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["runtime_sbom"]["name"] = renamed.name
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    checksums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(bundle.iterdir())
        if path.name != "SHA256SUMS"
    ]
    (bundle / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")

    with pytest.raises(ValueError, match="SBOM"):
        release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


def test_verify_rejects_checksum_manifest_without_a_final_newline(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    checksums = candidate / "bundle/SHA256SUMS"
    checksums.write_bytes(checksums.read_bytes().removesuffix(b"\n"))

    with pytest.raises(ValueError, match="checksum"):
        release_bundle.verify(candidate / "bundle", candidate_report=_candidate_report(candidate))


def test_materialize_does_not_publish_a_bundle_when_final_validation_fails(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    invalid_commit = "a" * 39
    report["export_manifest"]["source_commit"] = invalid_commit
    report["artifact_validation"]["source_revision"] = invalid_commit
    report["license_evidence"]["revision"] = invalid_commit
    report["scan"]["source_commit"] = invalid_commit
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="Git object"):
        release_bundle.materialize(candidate)

    assert (candidate / "bundle").exists() is False


def test_materialized_provenance_satisfies_the_public_contract_schema(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)
    schema_path = Path(__file__).parents[1] / "docs/release-readiness/release-bundle-provenance.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    provenance = json.loads((candidate / "bundle/bundle-provenance.json").read_text(encoding="utf-8"))

    assert list(Draft202012Validator(schema).iter_errors(provenance)) == []


def test_verifier_cli_emits_machine_readable_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidate = _candidate(tmp_path)
    release_bundle.materialize(candidate)

    bundle = candidate / "bundle"
    assert release_bundle.main(["--json", "--candidate-report", str(candidate / "report.json"), str(bundle)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["source_commit"] == "a" * 40


def test_verifier_cli_imports_without_the_product_runtime() -> None:
    script = Path(__file__).parents[1] / "scripts/release_bundle.py"

    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"], capture_output=True, text=True, check=False, timeout=10
    )

    assert result.returncode == 0
    assert "candidate-report" in result.stdout
