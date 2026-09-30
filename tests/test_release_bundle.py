"""Contracts for materializing and verifying closed release bundles."""

import hashlib
import inspect
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import _release_bundle_evidence, release_bundle
from tests import release_bundle_support

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("key", ["synthetic-secret\x1b[31m\n", "x" * 8192], ids=["control", "large"])
def test_duplicate_candidate_keys_are_not_reflected_in_errors(tmp_path: Path, key: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    encoded = json.dumps(key)
    (candidate / "report.json").write_text(f"{{{encoded}:1,{encoded}:2}}", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON input") as caught:
        release_bundle.materialize(candidate)
    assert str(caught.value) == "invalid JSON input"


def _candidate_report(candidate: Path) -> bytes:
    return (candidate / "report.json").read_bytes()


def test_candidate_inputs_names_each_distinct_validated_asset(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    payload = _candidate_report(candidate)
    report = json.loads(payload)
    inputs = _release_bundle_evidence._candidate_inputs(payload)
    assert inputs.report == report
    assert inputs.artifacts == tuple(
        sorted((item["name"], item["sha256"]) for item in report["artifact_validation"]["artifacts"])
    )
    assert inputs.sbom_sha256 == report["license_evidence"]["sbom_sha256"]
    assert inputs.runtime_requirements_sha256 == report["runtime_requirements"]["sha256"]
    assert inputs.release_build_requirements_sha256 == report["release_build_requirements"]["sha256"]
    assert inputs.runtime_wheelhouse_sha256 == report["runtime_wheelhouse"]["sha256"]
    assert (
        len(
            {
                inputs.sbom_sha256,
                inputs.runtime_requirements_sha256,
                inputs.release_build_requirements_sha256,
                inputs.runtime_wheelhouse_sha256,
            }
        )
        == 4
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in inspect.signature(type(inputs)).parameters.values()
    )
    with pytest.raises(FrozenInstanceError, match="report"):
        inputs.__setattr__("report", {})


def test_initial_provenance_preserves_exact_canonical_bytes(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    payload = _candidate_report(candidate)
    report = json.loads(payload)
    expected = {
        **release_bundle_support.expected_asset_provenance(report),
        "schema_version": 2,
        "candidate_report_sha256": hashlib.sha256(payload).hexdigest(),
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "exported_tree": "e" * 40,
        "export_policy_sha256": "d" * 64,
        "expected_repository": "example/fieldkit-cli",
        "planned_tag": "v1.0.0",
    }
    result = release_bundle.materialize(candidate)
    assert result.ok is True
    assert (candidate / "bundle/bundle-provenance.json").read_bytes() == (
        json.dumps(expected, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def test_materialize_creates_a_closed_digest_bound_bundle(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)

    report = release_bundle.materialize(candidate)

    bundle = candidate / "bundle"
    assert report.ok is True
    assert sorted(path.name for path in bundle.iterdir()) == [
        "SHA256SUMS",
        "bundle-provenance.json",
        "fieldkit_cli-1.0.0-py3-none-any.whl",
        "fieldkit_cli-1.0.0.tar.gz",
        "locked-graph.cdx.json",
        "platform-all-extras-requirements.txt",
        "release-build-requirements.txt",
        "runtime-license-observations.json",
        "runtime-requirements.txt",
        "runtime-wheelhouse.zip",
    ]
    verification = release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))
    assert verification.ok is True
    assert verification.source_commit == "a" * 40


def test_materialize_keeps_using_the_pinned_candidate_if_its_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    pinned = tmp_path / "pinned-candidate"
    attacker = tmp_path / "attacker-candidate"
    original_provenance = _release_bundle_evidence._provenance

    def swap_candidate(*args: object, **kwargs: object) -> bytes:
        result = original_provenance(*args, **kwargs)  # type: ignore[arg-type]
        candidate.rename(pinned)
        attacker.mkdir()
        candidate.symlink_to(attacker, target_is_directory=True)
        return result

    monkeypatch.setattr(release_bundle, "_provenance", swap_candidate)

    release_bundle.materialize(candidate)

    assert (pinned / "bundle/SHA256SUMS").is_file()
    assert list(attacker.iterdir()) == []


def test_materialize_never_replaces_a_bundle_created_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    original_rename = release_bundle._rename_no_replace_at
    competing_identity: tuple[int, int] | None = None

    def race_bundle(parent_fd: int, source: str, destination: str) -> None:
        nonlocal competing_identity
        os.mkdir(destination, dir_fd=parent_fd)
        metadata = os.stat(destination, dir_fd=parent_fd, follow_symlinks=False)
        competing_identity = (metadata.st_dev, metadata.st_ino)
        original_rename(parent_fd, source, destination)

    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", race_bundle)

    with pytest.raises(ValueError, match="already exists"):
        release_bundle.materialize(candidate)

    bundle_metadata = (candidate / "bundle").stat()
    assert competing_identity == (bundle_metadata.st_dev, bundle_metadata.st_ino)
    retained = [path for path in candidate.iterdir() if path.name.startswith(".bundle-")]
    assert len(retained) == 1
    assert (retained[0] / "SHA256SUMS").is_file()


def test_materialize_rejects_staging_substitution_inside_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    original_rename = release_bundle._rename_no_replace_at
    retained = candidate / "retained-staging"

    def substitute(parent_fd: int, source: str, destination: str) -> None:
        os.rename(source, retained.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.mkdir(source, dir_fd=parent_fd)
        original_rename(parent_fd, source, destination)

    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", substitute)

    with pytest.raises(ValueError, match="published bundle identity"):
        release_bundle.materialize(candidate)

    assert (retained / "SHA256SUMS").is_file()
    assert list((candidate / "bundle").iterdir()) == []


def test_materialize_rejects_destination_substitution_during_published_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    original_verify = release_bundle._verify_open_bundle
    retained = candidate / "retained-bundle"
    calls = 0

    def verify(
        directory_fd: int,
        *,
        candidate_report: bytes,
        expected_anchor: None = None,
        cutover_record: None = None,
    ) -> release_bundle.BundleReport:
        nonlocal calls
        assert expected_anchor is None and cutover_record is None
        result = original_verify(directory_fd, candidate_report=candidate_report)
        calls += 1
        if calls == 2:
            (candidate / "bundle").rename(retained)
            (candidate / "bundle").mkdir()
        return result

    monkeypatch.setattr(release_bundle, "_verify_open_bundle", verify)

    with pytest.raises(ValueError, match="identity changed during verification"):
        release_bundle.materialize(candidate)

    assert (retained / "SHA256SUMS").is_file()
    assert list((candidate / "bundle").iterdir()) == []


def test_materialize_reverifies_published_bundle_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    original_rename = release_bundle._rename_no_replace_at

    def corrupt(parent_fd: int, source: str, destination: str) -> None:
        original_rename(parent_fd, source, destination)
        (candidate / "bundle/fieldkit_cli-1.0.0-py3-none-any.whl").write_bytes(b"substitute")

    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", corrupt)

    with pytest.raises(ValueError, match="digest"):
        release_bundle.materialize(candidate)

    assert (candidate / "bundle/SHA256SUMS").is_file()
    assert (candidate / "bundle/fieldkit_cli-1.0.0-py3-none-any.whl").read_bytes() == b"substitute"


def test_materialize_and_verify_allow_the_bounded_runtime_wheelhouse(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    wheelhouse = candidate / "runtime-wheelhouse.zip"
    wheelhouse.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    report = json.loads((candidate / "report.json").read_text(encoding="utf-8"))
    report["runtime_wheelhouse"]["sha256"] = hashlib.sha256(wheelhouse.read_bytes()).hexdigest()
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")

    release_bundle.materialize(candidate)

    assert release_bundle.verify(candidate / "bundle", candidate_report=_candidate_report(candidate)).ok is True


@pytest.mark.parametrize("mutation", ["extra", "tamper", "missing"])
@pytest.mark.parametrize("capture", [False, True])
def test_verify_rejects_changed_bundle_members(tmp_path: Path, mutation: str, capture: bool) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    if mutation == "extra":
        (bundle / "unexpected.txt").write_text("no", encoding="utf-8")
    elif mutation == "tamper":
        (bundle / "locked-graph.cdx.json").write_text("changed", encoding="utf-8")
    else:
        (bundle / "SHA256SUMS").unlink()

    if capture:
        directory_fd = release_bundle._open_directory(bundle)
        try:
            with pytest.raises(ValueError, match="bundle"):
                release_bundle.capture_open_bundle(directory_fd, candidate_report=_candidate_report(candidate))
        finally:
            os.close(directory_fd)
    else:
        with pytest.raises(ValueError, match="bundle"):
            release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


def test_captured_bundle_is_immutable_and_retains_exact_verified_members(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    expected = {path.name: path.read_bytes() for path in bundle.iterdir()}
    directory_fd = release_bundle._open_directory(bundle)
    try:
        captured = release_bundle.capture_open_bundle(directory_fd, candidate_report=_candidate_report(candidate))
    finally:
        os.close(directory_fd)
    assert captured.report == release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))
    assert captured.members == expected
    for name, digest in captured.report.files:
        assert hashlib.sha256(captured.members[name]).hexdigest() == digest
    (bundle / "locked-graph.cdx.json").write_bytes(b"changed after capture")
    replacement = candidate / "replacement-checksums"
    replacement.write_bytes(b"replaced after capture")
    replacement.replace(bundle / "SHA256SUMS")
    assert captured.members == expected
    with pytest.raises(TypeError, match="does not support item assignment"):
        captured.members["SHA256SUMS"] = b"changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        captured.members = {}  # type: ignore[misc]


def test_verify_rejects_checksums_substituted_after_outer_manifest_validation(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    expected_checksums = (bundle / "SHA256SUMS").read_bytes()
    (bundle / "SHA256SUMS").write_text("0" * 64 + "  locked-graph.cdx.json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outer approval manifest"):
        release_bundle.verify(
            bundle,
            candidate_report=_candidate_report(candidate),
            expected_checksums=expected_checksums,
        )


def test_materialize_rejects_runtime_requirements_that_do_not_match_the_candidate_report(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    (candidate / "runtime-requirements.txt").write_text("click==8.5.0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="runtime requirements digest"):
        release_bundle.materialize(candidate)


def test_verify_rejects_a_symlinked_bundle_member(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"
    replacement = candidate / "replacement-sbom.json"
    replacement.write_text('{"components": []}\n', encoding="utf-8")
    (bundle / "locked-graph.cdx.json").unlink()
    (bundle / "locked-graph.cdx.json").symlink_to(replacement)

    with pytest.raises(ValueError, match="bundle"):
        release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


def test_verify_rejects_a_symlinked_bundle_root(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)
    linked_bundle = tmp_path / "linked-bundle"
    linked_bundle.symlink_to(candidate / "bundle", target_is_directory=True)

    with pytest.raises(ValueError, match="bundle"):
        release_bundle.verify(linked_bundle, candidate_report=_candidate_report(candidate))


def test_materialize_rejects_a_symlinked_candidate_dist_directory(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    external_dist = tmp_path / "external-dist"
    (candidate / "dist").replace(external_dist)
    (candidate / "dist").symlink_to(external_dist, target_is_directory=True)

    with pytest.raises(ValueError, match="directory"):
        release_bundle.materialize(candidate)

    assert (candidate / "bundle").exists() is False


def test_materialize_rejects_report_that_disagrees_with_sbom(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["license_evidence"]["sbom_sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="SBOM"):
        release_bundle.materialize(candidate)

    assert (candidate / "bundle").exists() is False


def test_materialize_rejects_candidate_with_failed_scan_evidence(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["scan"]["status"] = "fail"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="scan"):
        release_bundle.materialize(candidate)


def test_materialize_rejects_candidate_without_a_package_identity(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    report_path = candidate / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["package"] = ""
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="package"):
        release_bundle.materialize(candidate)


@pytest.mark.parametrize("mutation", ["artifact", "license"])
def test_materialize_rejects_failure_details_hidden_behind_passing_summary(tmp_path: Path, mutation: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
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
    candidate = release_bundle_support.candidate(tmp_path)
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
    candidate = release_bundle_support.candidate(tmp_path)
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
    candidate = release_bundle_support.candidate(tmp_path)
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
    candidate = release_bundle_support.candidate(tmp_path)
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
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)
    bundle = candidate / "bundle"

    other_candidate = release_bundle_support.candidate(tmp_path / "other")
    other_report = _candidate_report(other_candidate) + b"\n"

    with pytest.raises(ValueError, match="expected candidate report"):
        release_bundle.verify(bundle, candidate_report=other_report)


def test_verify_rejects_rechecksummed_provenance_that_disagrees_with_candidate(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
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
    candidate = release_bundle_support.candidate(tmp_path)
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
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)
    checksums = candidate / "bundle/SHA256SUMS"
    checksums.write_bytes(checksums.read_bytes().removesuffix(b"\n"))

    with pytest.raises(ValueError, match="checksum"):
        release_bundle.verify(candidate / "bundle", candidate_report=_candidate_report(candidate))


def test_materialize_does_not_publish_a_bundle_when_final_validation_fails(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
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
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)
    schema_path = Path(__file__).parents[1] / "docs/release-readiness/release-bundle-provenance.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    provenance = json.loads((candidate / "bundle/bundle-provenance.json").read_text(encoding="utf-8"))

    assert list(Draft202012Validator(schema).iter_errors(provenance)) == []


def test_verifier_cli_emits_machine_readable_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    release_bundle.materialize(candidate)

    bundle = candidate / "bundle"
    assert release_bundle.main(["--json", "--candidate-report", str(candidate / "report.json"), str(bundle)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["source_commit"] == "a" * 40


def test_initial_candidate_deep_json_is_a_sanitized_failure(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    (candidate / "report.json").write_bytes(b"[" * 5000 + b"0" + b"]" * 5000)
    with pytest.raises(ValueError, match="JSON input exceeds nesting limit"):
        release_bundle.materialize(candidate)


def test_verifier_cli_deep_provenance_returns_invalid_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    result = release_bundle.materialize(candidate)
    assert result.ok is True
    bundle = candidate / "bundle"
    nested = b"[" * 5000 + b"0" + b"]" * 5000
    (bundle / "bundle-provenance.json").write_bytes(nested)
    files = tuple(
        (name, hashlib.sha256(nested).hexdigest() if name == "bundle-provenance.json" else digest)
        for name, digest in result.files
    )
    (bundle / "SHA256SUMS").write_bytes(release_bundle._checksum_manifest(files))
    status = release_bundle.main([str(bundle), "--candidate-report", str(candidate / "report.json")])
    assert status == 3
    captured = capsys.readouterr()
    assert captured.err == "Release bundle: ERROR: JSON input exceeds nesting limit\n"
    assert captured.out == ""


def test_verifier_cli_imports_without_the_product_runtime() -> None:
    script = Path(__file__).parents[1] / "scripts/release_bundle.py"

    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"], capture_output=True, text=True, check=False, timeout=10
    )

    assert result.returncode == 0
    assert "candidate-report" in result.stdout


def test_initial_verifier_uses_only_stdlib_without_successor_modules(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    prepared = release_bundle.materialize(candidate)
    assert prepared.ok is True
    scripts = release_bundle_support.standalone_scripts(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(scripts / "release_bundle.py"),
            str(candidate / "bundle"),
            "--candidate-report",
            str(candidate / "report.json"),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "pass"
    assert result.stderr == ""


@pytest.mark.parametrize("field", ["observations", "platform_requirements", "marker_environment"])
def test_materialize_rejects_license_evidence_missing_reconstructible_inputs(tmp_path: Path, field: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    report = json.loads(_candidate_report(candidate))
    del report["license_evidence"][field]
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported schema"):
        release_bundle.materialize(candidate)

    assert not (candidate / "bundle").exists()


@pytest.mark.parametrize("name", ["runtime-license-observations.json", "platform-all-extras-requirements.txt"])
@pytest.mark.parametrize("mutation", ["missing", "digest", "symlink"])
def test_materialize_rejects_unbound_runtime_license_inputs(tmp_path: Path, name: str, mutation: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    path = candidate / name
    if mutation == "digest":
        path.write_bytes(b"changed input")
    else:
        path.unlink()
        if mutation == "symlink":
            target = tmp_path / "untrusted"
            target.write_bytes(b"changed input")
            path.symlink_to(target)

    with pytest.raises(ValueError, match=r"(unavailable|digest does not match)"):
        release_bundle.materialize(candidate)

    assert not (candidate / "bundle").exists()


@pytest.mark.parametrize("mutation", ["markers", "license", "version", "missing_marker", "extra_marker"])
def test_materialize_rejects_self_hashed_observations_disagreeing_with_receipt(tmp_path: Path, mutation: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    raw = json.loads((candidate / "runtime-license-observations.json").read_bytes())
    if mutation == "markers":
        raw["marker_environment"]["python_version"] = "3.12"
    elif mutation == "missing_marker":
        del raw["marker_environment"]["python_version"]
    elif mutation == "extra_marker":
        raw["marker_environment"]["hostname"] = "example"
    else:
        raw["packages"][0]["license_expression" if mutation == "license" else "version"] = (
            "MIT" if mutation == "license" else "8.6.0"
        )
    raw_bytes = json.dumps(raw).encode()
    (candidate / "runtime-license-observations.json").write_bytes(raw_bytes)
    report = json.loads(_candidate_report(candidate))
    report["license_evidence"]["observations"]["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match=r"(observations do not match|every PEP 508 marker)"):
        release_bundle.materialize(candidate)

    assert not (candidate / "bundle").exists()


@pytest.mark.parametrize("name", ["runtime-license-observations.json", "platform-all-extras-requirements.txt"])
def test_verify_rejects_rechecksummed_license_input_swap(tmp_path: Path, name: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    result = release_bundle.materialize(candidate)
    assert result.ok
    bundle = candidate / "bundle"
    (bundle / name).write_bytes(b"swapped input")
    provenance = json.loads((bundle / "bundle-provenance.json").read_bytes())
    field = "license_observations" if name.startswith("runtime-license") else "platform_requirements"
    provenance[field]["sha256"] = hashlib.sha256(b"swapped input").hexdigest()
    (bundle / "bundle-provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    (bundle / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(bundle.iterdir())
            if path.name != "SHA256SUMS"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="license inputs do not match"):
        release_bundle.verify(bundle, candidate_report=_candidate_report(candidate))


@pytest.mark.parametrize("mutation", ["license", "package", "missing_marker", "extra_marker", "marker_type"])
def test_materialize_rejects_receipt_rows_and_markers_disagreeing_with_raw_inventory(
    tmp_path: Path, mutation: str
) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    report = json.loads(_candidate_report(candidate))
    receipt = report["license_evidence"]
    if mutation == "license":
        receipt["packages"][0]["license_expression"] = "MIT"
    elif mutation == "package":
        receipt["packages"][0]["package_url"] = "pkg:pypi/click@8.6.0"
    elif mutation == "missing_marker":
        del receipt["marker_environment"]["python_version"]
    elif mutation == "extra_marker":
        receipt["marker_environment"]["extra"] = "example"
    else:
        receipt["marker_environment"]["python_version"] = 3
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match=r"(observations do not match|every PEP 508 marker|unsafe or invalid)"):
        release_bundle.materialize(candidate)

    assert not (candidate / "bundle").exists()


@pytest.mark.parametrize("raw_expression,normalized", [(" BSD-3-Clause ", "BSD-3-Clause"), ("", "UNKNOWN")])
def test_bundle_uses_the_producer_license_expression_normalization(
    tmp_path: Path, raw_expression: str, normalized: str
) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    raw = json.loads((candidate / "runtime-license-observations.json").read_bytes())
    raw["packages"][0]["license_expression"] = raw_expression
    raw_bytes = json.dumps(raw).encode()
    (candidate / "runtime-license-observations.json").write_bytes(raw_bytes)
    report = json.loads(_candidate_report(candidate))
    report["license_evidence"]["observations"]["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    report["license_evidence"]["packages"][0]["license_expression"] = normalized
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")

    result = release_bundle.materialize(candidate)

    assert result.ok
    assert release_bundle.verify(candidate / "bundle", candidate_report=_candidate_report(candidate)).ok
