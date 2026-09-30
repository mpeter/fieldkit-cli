"""Canonical candidate evidence and asset projections for local bundles."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from scripts.json_policy import load_json_bytes
from scripts.runtime_license_inventory import OBSERVATIONS_NAME, PLATFORM_REQUIREMENTS_NAME, validate_marker_environment

_CHECKSUMS_NAME = "SHA256SUMS"
_PROVENANCE_NAME = "bundle-provenance.json"
_SBOM_NAME = "locked-graph.cdx.json"
_RUNTIME_REQUIREMENTS_NAME = "runtime-requirements.txt"
_RELEASE_BUILD_REQUIREMENTS_NAME = "release-build-requirements.txt"
_RUNTIME_WHEELHOUSE_NAME = "runtime-wheelhouse.zip"
_DIGEST_LENGTH = 64
_BUNDLE_SCHEMA_VERSION = 2
_BUNDLE_MEMBER_COUNT = 10
_MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_WHEELHOUSE_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True, kw_only=True)
class CandidateInputs:
    """Validated report and independently named artifact/runtime digests."""

    report: dict[str, object]
    artifacts: tuple[tuple[str, str], ...]
    sbom_sha256: str
    runtime_requirements_sha256: str
    release_build_requirements_sha256: str
    runtime_wheelhouse_sha256: str


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_object(data: bytes, subject: str) -> dict[str, object]:
    """Parse one bounded JSON object with duplicate-key rejection."""
    value = load_json_bytes(data)
    if not isinstance(value, dict):
        raise ValueError(f"{subject} must be a JSON object")
    return value


def _string(value: object, subject: str) -> str:
    """Require one non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{subject} must be a non-empty string")
    return value


def _digest_string(value: object, subject: str) -> str:
    """Require a lowercase SHA-256 digest."""
    digest = _string(value, subject)
    if len(digest) != _DIGEST_LENGTH or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{subject} must be a lowercase SHA-256 digest")
    return digest


def _basename(value: object, subject: str) -> str:
    """Require a plain safe filename."""
    name = _string(value, subject)
    if Path(name).name != name or name in {".", ".."} or any(character in name for character in "\0\r\n"):
        raise ValueError(f"{subject} must be a plain filename")
    return name


def _object(value: object, subject: str) -> dict[str, object]:
    """Require a JSON object."""
    if not isinstance(value, dict):
        raise ValueError(f"{subject} must be an object")
    return value


def _descriptor_digest(value: object, name: str, subject: str, *, scope: str | None = None) -> str:
    descriptor = _object(value, subject)
    required = {"name", "sha256"} | ({"scope"} if scope is not None else set())
    if (
        set(descriptor) != required
        or descriptor["name"] != name
        or (scope is not None and descriptor["scope"] != scope)
    ):
        raise ValueError(f"{subject} has an unsupported schema")
    return _digest_string(descriptor["sha256"], f"{subject} digest")


def _validate_scan(
    scan: dict[str, object],
    bindings: dict[str, object],
    artifacts: tuple[tuple[str, str], ...],
    *,
    schema_version: int = 1,
) -> None:
    """Require scan evidence to describe this exact candidate and its artifacts."""
    required = {
        "schema_version",
        "source_commit",
        "source_tree",
        "exported_tree",
        "expected_repository",
        "planned_tag",
        "export_policy_oid",
        "export_policy_sha256",
        "scan_policy_oid",
        "scan_policy_sha256",
        "identity_policy_oid",
        "identity_policy_sha256",
        "scanned_entries",
        "scanned_artifact_entries",
        "scanned_text_entries",
        "approved_binary_entries",
        "classified_matches",
        "gitleaks_version",
        "gitleaks_findings",
        "artifacts",
        "findings",
        "status",
        "artifact_coverage",
    }
    if schema_version == 2:
        required |= {"source_kind", "anchor", "source_sha256"}
    if (
        set(scan) != required
        or type(scan.get("schema_version")) is not int
        or scan.get("schema_version") != schema_version
        or scan.get("status") != "pass"
    ):
        raise ValueError("candidate scan evidence has an unsupported schema or status")
    if any(scan[key] != value for key, value in bindings.items()):
        raise ValueError("candidate scan evidence does not bind the verified export")
    _oid(scan["scan_policy_oid"], "candidate scan policy object ID")
    _digest_string(scan["scan_policy_sha256"], "candidate scan policy digest")
    _oid(scan["identity_policy_oid"], "candidate identity policy object ID")
    _digest_string(scan["identity_policy_sha256"], "candidate identity policy digest")
    if scan["gitleaks_version"] != "8.30.1" or scan["gitleaks_findings"] != 0:
        raise ValueError("candidate scan evidence has unverified secret-scanner coverage")
    if scan.get("artifact_coverage") != "pass" or scan.get("findings") != []:
        raise ValueError("candidate scan evidence did not pass")
    numeric = {
        "scanned_entries",
        "scanned_artifact_entries",
        "scanned_text_entries",
        "approved_binary_entries",
        "classified_matches",
        "gitleaks_findings",
    }
    for key in numeric:
        value = scan[key]
        if type(value) is not int or value < 0:
            raise ValueError("candidate scan evidence counts are invalid")
    scanned_artifacts = scan.get("artifacts")
    if not isinstance(scanned_artifacts, list) or len(scanned_artifacts) != 2:
        raise ValueError("candidate scan evidence must contain exactly two artifacts")
    observed: dict[tuple[str, str], str] = {}
    for artifact in scanned_artifacts:
        entry = _object(artifact, "candidate scanned artifact")
        if set(entry) != {"name", "kind", "sha256", "member_count", "total_uncompressed_bytes"}:
            raise ValueError("candidate scanned artifact has an unsupported schema")
        kind = _string(entry["kind"], "candidate scanned artifact kind")
        name = _basename(entry["name"], "candidate scanned artifact name")
        if kind not in {"wheel", "sdist"} or (kind, name) in observed:
            raise ValueError("candidate scanned artifacts are ambiguous")
        if (
            type(entry["member_count"]) is not int
            or entry["member_count"] < 0
            or type(entry["total_uncompressed_bytes"]) is not int
            or entry["total_uncompressed_bytes"] < 0
        ):
            raise ValueError("candidate scanned artifact counts are invalid")
        observed[(kind, name)] = _digest_string(entry["sha256"], "candidate scanned artifact digest")
    expected = {("wheel" if name.endswith(".whl") else "sdist", name): digest for name, digest in artifacts}
    if observed != expected:
        raise ValueError("candidate scan artifacts do not match validation evidence")


def _candidate_inputs(report_bytes: bytes) -> CandidateInputs:
    """Return exact build inputs from a passing retained candidate report."""
    if len(report_bytes) > _MAX_FILE_BYTES:
        raise ValueError("candidate report exceeds size limit")
    report = _json_object(report_bytes, "candidate report")
    required = {
        "schema_version",
        "status",
        "expected_repository",
        "package",
        "planned_tag",
        "export_manifest",
        "artifact_validation",
        "license_evidence",
        "runtime_requirements",
        "release_build_requirements",
        "runtime_wheelhouse",
        "scan",
    }
    if (
        set(report) != required
        or type(report.get("schema_version")) is not int
        or report.get("schema_version") != 7
        or report.get("status") != "pass"
    ):
        raise ValueError("candidate report has an unsupported schema or status")
    manifest = _object(report["export_manifest"], "candidate export manifest")
    if (
        set(manifest)
        != {
            "schema_version",
            "source_commit",
            "source_tree",
            "policy_path",
            "policy_oid",
            "policy_sha256",
            "expected_repository",
            "planned_tag",
            "exported_tree",
            "included",
            "excluded",
        }
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
    ):
        raise ValueError("candidate export manifest has an unsupported schema_version")
    for field in ("source_commit", "source_tree", "policy_oid", "exported_tree"):
        _oid(manifest[field], f"candidate export manifest {field}")
    _digest_string(manifest["policy_sha256"], "candidate export manifest policy digest")
    if manifest["policy_path"] != "docs/release-readiness/public-tree-policy.json":
        raise ValueError("candidate export manifest policy path is unsupported")
    for field in ("included", "excluded"):
        values = manifest[field]
        if not isinstance(values, list):
            raise ValueError("candidate export manifest entries must be a list")
        for value in values:
            entry = _object(value, "candidate export manifest entry")
            if set(entry) != {"path", "mode", "oid", "category", "rule_id"}:
                raise ValueError("candidate export manifest entry has an unsupported schema")
            for key in ("path", "category", "rule_id"):
                _string(entry[key], f"candidate export manifest entry {key}")
            if not isinstance(entry["mode"], str) or entry["mode"] not in {"100644", "100755", "120000"}:
                raise ValueError("candidate export manifest entry mode is unsupported")
            _oid(entry["oid"], "candidate export manifest entry object ID")
    bindings = {
        key: manifest[field]
        for key, field in {
            "source_commit": "source_commit",
            "source_tree": "source_tree",
            "exported_tree": "exported_tree",
            "expected_repository": "expected_repository",
            "planned_tag": "planned_tag",
            "export_policy_oid": "policy_oid",
            "export_policy_sha256": "policy_sha256",
        }.items()
    }
    if report.get("expected_repository") != manifest.get("expected_repository") or report.get(
        "planned_tag"
    ) != manifest.get("planned_tag"):
        raise ValueError("candidate public identity does not bind the verified export")
    _string(report["expected_repository"], "candidate expected repository")
    _string(report["planned_tag"], "candidate planned tag")
    return _candidate_assets(
        report,
        source_commit=_string(manifest.get("source_commit"), "candidate source commit"),
        policy_sha256=manifest.get("policy_sha256"),
        scan_bindings=bindings,
    )


def _candidate_assets(
    report: dict[str, object],
    *,
    source_commit: str,
    policy_sha256: object,
    scan_bindings: dict[str, object],
    scan_schema: int = 1,
) -> CandidateInputs:
    """Validate shared artifact/runtime evidence against explicit source bindings."""
    artifact_validation = _object(report["artifact_validation"], "candidate artifact validation")
    license_evidence = _object(report["license_evidence"], "candidate license evidence")
    runtime_requirements = _object(report["runtime_requirements"], "candidate runtime requirements")
    build_requirements = _object(report["release_build_requirements"], "candidate release build requirements")
    runtime_wheelhouse = _object(report["runtime_wheelhouse"], "candidate runtime wheelhouse")
    scan = _object(report["scan"], "candidate scan")
    _string(report["package"], "candidate package")
    if set(artifact_validation) != {"schema_version", "status", "source_revision", "artifacts"}:
        raise ValueError("candidate artifact validation has an unsupported schema")
    if (
        type(artifact_validation.get("schema_version")) is not int
        or artifact_validation.get("schema_version") != 1
        or artifact_validation.get("status") != "pass"
        or artifact_validation.get("source_revision") != source_commit
    ):
        raise ValueError("candidate artifact validation has an unsupported schema or source commit")
    required_license_evidence = {
        "schema_version",
        "status",
        "scope",
        "revision",
        "export_policy_sha256",
        "sbom_sha256",
        "observed_packages",
        "packages",
        "findings",
        "observations",
        "platform_requirements",
        "marker_environment",
    }
    if set(license_evidence) != required_license_evidence:
        raise ValueError("candidate license evidence has an unsupported schema")
    if (
        type(license_evidence.get("schema_version")) is not int
        or license_evidence.get("schema_version") != 2
        or license_evidence.get("status") != "pass"
        or license_evidence.get("scope") != "runtime-all-extras"
        or license_evidence.get("revision") != source_commit
        or license_evidence.get("export_policy_sha256") != policy_sha256
    ):
        raise ValueError("candidate license evidence has an unsupported schema or source commit")
    validate_marker_environment(license_evidence["marker_environment"])
    for field, name in (("observations", OBSERVATIONS_NAME), ("platform_requirements", PLATFORM_REQUIREMENTS_NAME)):
        descriptor = _object(license_evidence[field], f"candidate license {field}")
        if set(descriptor) != {"name", "sha256"} or descriptor["name"] != name:
            raise ValueError(f"candidate license {field} has an unsupported schema")
        _digest_string(descriptor["sha256"], f"candidate license {field} digest")
    packages = license_evidence.get("packages")
    observed_packages = license_evidence.get("observed_packages")
    if (
        type(observed_packages) is not int
        or observed_packages < 0
        or not isinstance(packages, list)
        or observed_packages != len(packages)
        or license_evidence.get("findings") != []
    ):
        raise ValueError("candidate license evidence does not pass its complete contract")
    package_urls: list[str] = []
    for package in packages:
        observation = _object(package, "candidate license package")
        if set(observation) != {"package_url", "license_expression"}:
            raise ValueError("candidate license package has an unsupported schema")
        package_url = _string(observation["package_url"], "candidate license package URL")
        if not package_url.startswith("pkg:pypi/"):
            raise ValueError("candidate license package URL must use pkg:pypi/")
        _string(observation["license_expression"], "candidate license expression")
        package_urls.append(package_url)
    if package_urls != sorted(set(package_urls)):
        raise ValueError("candidate license package inventory must be canonical and unique")
    artifacts = artifact_validation.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("candidate artifacts must be a list")
    entries: list[tuple[str, str]] = []
    kinds: set[str] = set()
    names: set[str] = set()
    for artifact in artifacts:
        item = _object(artifact, "candidate artifact")
        if set(item) != {"name", "kind", "sha256", "criteria", "status"}:
            raise ValueError("candidate artifact has an unsupported schema")
        criteria = item.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("candidate artifact criteria are invalid")
        for criterion in criteria:
            result = _object(criterion, "candidate artifact criterion")
            if (
                set(result) != {"criterion_id", "status", "diagnostics"}
                or not isinstance(result.get("criterion_id"), str)
                or not result["criterion_id"]
                or result.get("status") != "pass"
                or not isinstance(result.get("diagnostics"), list)
                or result["diagnostics"]
            ):
                raise ValueError("candidate artifact criteria did not pass")
        if item.get("status") != "pass":
            raise ValueError("candidate artifact did not pass validation")
        kind = _string(item.get("kind"), "candidate artifact kind")
        if kind not in {"wheel", "sdist"} or kind in kinds:
            raise ValueError("candidate must contain exactly one wheel and one source distribution")
        kinds.add(kind)
        name = _basename(item.get("name"), "candidate artifact name")
        if name in {_CHECKSUMS_NAME, _PROVENANCE_NAME, _SBOM_NAME} or name in names:
            raise ValueError("candidate artifact name is reserved or duplicated")
        if (kind == "wheel" and not name.endswith(".whl")) or (kind == "sdist" and not name.endswith(".tar.gz")):
            raise ValueError("candidate artifact name does not match its kind")
        names.add(name)
        entries.append(
            (
                name,
                _digest_string(item.get("sha256"), "candidate artifact digest"),
            )
        )
    if kinds != {"wheel", "sdist"}:
        raise ValueError("candidate must contain exactly one wheel and one source distribution")
    _validate_scan(scan, scan_bindings, tuple(sorted(entries)), schema_version=scan_schema)
    sbom_digest = _digest_string(license_evidence.get("sbom_sha256"), "candidate SBOM digest")
    requirements_digest = _descriptor_digest(
        runtime_requirements, _RUNTIME_REQUIREMENTS_NAME, "candidate runtime requirements"
    )
    build_requirements_digest = _descriptor_digest(
        build_requirements, _RELEASE_BUILD_REQUIREMENTS_NAME, "candidate release build requirements"
    )
    wheelhouse_digest = _descriptor_digest(runtime_wheelhouse, _RUNTIME_WHEELHOUSE_NAME, "candidate runtime wheelhouse")
    return CandidateInputs(
        report=report,
        artifacts=tuple(sorted(entries)),
        sbom_sha256=sbom_digest,
        runtime_requirements_sha256=requirements_digest,
        release_build_requirements_sha256=build_requirements_digest,
        runtime_wheelhouse_sha256=wheelhouse_digest,
    )


def _provenance(
    inputs: CandidateInputs,
    report_digest: str,
) -> bytes:
    """Render canonical non-hosted provenance for the retained candidate."""
    manifest = _object(inputs.report["export_manifest"], "candidate export manifest")
    payload = {
        **_asset_provenance(inputs),
        "schema_version": _BUNDLE_SCHEMA_VERSION,
        "candidate_report_sha256": report_digest,
        "source_commit": manifest["source_commit"],
        "source_tree": manifest["source_tree"],
        "exported_tree": manifest["exported_tree"],
        "export_policy_sha256": manifest["policy_sha256"],
        "expected_repository": inputs.report["expected_repository"],
        "planned_tag": inputs.report["planned_tag"],
    }
    return _canonical_provenance_bytes(payload)


def _canonical_provenance_bytes(payload: dict[str, object]) -> bytes:
    """Keep the exact original provenance encoding for both source kinds."""
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _asset_provenance(inputs: CandidateInputs) -> dict[str, object]:
    """One authoritative runtime/artifact projection shared by both source kinds."""
    artifact_entries = [
        {"name": name, "kind": "wheel" if name.endswith(".whl") else "sdist", "sha256": digest}
        for name, digest in inputs.artifacts
    ]
    return {
        "artifacts": artifact_entries,
        "runtime_sbom": {"name": _SBOM_NAME, "sha256": inputs.sbom_sha256, "scope": "runtime-all-extras"},
        "runtime_requirements": {"name": _RUNTIME_REQUIREMENTS_NAME, "sha256": inputs.runtime_requirements_sha256},
        "release_build_requirements": {
            "name": _RELEASE_BUILD_REQUIREMENTS_NAME,
            "sha256": inputs.release_build_requirements_sha256,
        },
        "runtime_wheelhouse": {"name": _RUNTIME_WHEELHOUSE_NAME, "sha256": inputs.runtime_wheelhouse_sha256},
        "license_observations": _object(inputs.report["license_evidence"], "candidate license evidence")[
            "observations"
        ],
        "platform_requirements": _object(inputs.report["license_evidence"], "candidate license evidence")[
            "platform_requirements"
        ],
        "marker_environment": _object(inputs.report["license_evidence"], "candidate license evidence")[
            "marker_environment"
        ],
        "checksum_manifest": {"name": _CHECKSUMS_NAME, "format_version": 1},
    }


def _oid(value: object, subject: str) -> str:
    """Require a lowercase Git object ID."""
    oid = _string(value, subject)
    if len(oid) != 40 or any(character not in "0123456789abcdef" for character in oid):
        raise ValueError(f"{subject} must be a lowercase Git object ID")
    return oid


def _verify_provenance(provenance: dict[str, object], checksums: tuple[tuple[str, str], ...]) -> str:
    """Require provenance to bind every checksum-listed release asset."""
    required = {
        "schema_version",
        "candidate_report_sha256",
        "source_commit",
        "source_tree",
        "exported_tree",
        "export_policy_sha256",
        "expected_repository",
        "planned_tag",
        "artifacts",
        "runtime_sbom",
        "runtime_requirements",
        "release_build_requirements",
        "runtime_wheelhouse",
        "checksum_manifest",
        "license_observations",
        "platform_requirements",
        "marker_environment",
    }
    if (
        set(provenance) != required
        or type(provenance.get("schema_version")) is not int
        or provenance.get("schema_version") != _BUNDLE_SCHEMA_VERSION
    ):
        raise ValueError("bundle provenance has an unsupported schema")
    _digest_string(provenance["candidate_report_sha256"], "bundle candidate report digest")
    source_commit = _oid(provenance["source_commit"], "bundle source commit")
    _oid(provenance["source_tree"], "bundle source tree")
    _oid(provenance["exported_tree"], "bundle exported tree")
    _digest_string(provenance["export_policy_sha256"], "bundle export policy digest")
    repository = _string(provenance["expected_repository"], "bundle expected repository")
    if re.fullmatch(r"[a-z0-9-]+/fieldkit-cli", repository) is None:
        raise ValueError("bundle expected repository is invalid")
    if provenance["planned_tag"] != "v1.0.0":
        raise ValueError("bundle planned tag is invalid")
    _verify_provenance_assets(provenance, checksums)
    return source_commit


def _verify_provenance_assets(
    provenance: dict[str, object],
    checksums: tuple[tuple[str, str], ...],
    *,
    extra_files: dict[str, str] | None = None,
) -> None:
    """Validate one closed asset set without changing either source contract."""
    artifacts = provenance["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ValueError("bundle provenance artifacts must contain one wheel and one source distribution")
    expected: dict[str, str] = dict(extra_files or {})
    validate_marker_environment(provenance["marker_environment"])
    for field, name in (
        ("license_observations", OBSERVATIONS_NAME),
        ("platform_requirements", PLATFORM_REQUIREMENTS_NAME),
    ):
        descriptor = _object(provenance[field], f"bundle {field}")
        if set(descriptor) != {"name", "sha256"} or descriptor["name"] != name:
            raise ValueError(f"bundle {field} has an unsupported schema")
        expected[name] = _digest_string(descriptor["sha256"], f"bundle {field} digest")
    kinds: set[str] = set()
    for artifact in artifacts:
        entry = _object(artifact, "bundle provenance artifact")
        if set(entry) != {"name", "kind", "sha256"}:
            raise ValueError("bundle provenance artifact has an unsupported schema")
        name = _basename(entry["name"], "bundle provenance artifact name")
        kind = _string(entry["kind"], "bundle provenance artifact kind")
        if (
            kind not in {"wheel", "sdist"}
            or kind in kinds
            or name in expected
            or name in {_CHECKSUMS_NAME, _PROVENANCE_NAME, _SBOM_NAME}
            or (kind == "wheel" and not name.endswith(".whl"))
            or (kind == "sdist" and not name.endswith(".tar.gz"))
        ):
            raise ValueError("bundle provenance artifacts are ambiguous")
        kinds.add(kind)
        expected[name] = _digest_string(entry["sha256"], "bundle provenance artifact digest")
    for field, name, subject, scope in (
        ("runtime_sbom", _SBOM_NAME, "bundle runtime SBOM", "runtime-all-extras"),
        ("runtime_requirements", _RUNTIME_REQUIREMENTS_NAME, "bundle runtime requirements", None),
        ("release_build_requirements", _RELEASE_BUILD_REQUIREMENTS_NAME, "bundle release build requirements", None),
        ("runtime_wheelhouse", _RUNTIME_WHEELHOUSE_NAME, "bundle runtime wheelhouse", None),
    ):
        expected[name] = _descriptor_digest(provenance[field], name, subject, scope=scope)
    checksum_manifest = _object(provenance["checksum_manifest"], "bundle checksum manifest")
    if (
        set(checksum_manifest) != {"name", "format_version"}
        or checksum_manifest.get("name") != _CHECKSUMS_NAME
        or type(checksum_manifest.get("format_version")) is not int
        or checksum_manifest.get("format_version") != 1
    ):
        raise ValueError("bundle checksum manifest identity is invalid")
    observed = dict(checksums)
    if expected.keys() | {_PROVENANCE_NAME} != observed.keys():
        raise ValueError("bundle provenance and checksum manifest disagree")
    if any(observed[name] != digest for name, digest in expected.items()):
        raise ValueError("bundle provenance digests do not match checksums")


def _verify_candidate_binding(provenance: dict[str, object], candidate_report: bytes) -> CandidateInputs:
    """Require bundle provenance to reproduce one independently supplied candidate report."""
    inputs = _candidate_inputs(candidate_report)
    manifest = _object(inputs.report["export_manifest"], "candidate export manifest")
    license_evidence = _object(inputs.report["license_evidence"], "candidate license evidence")
    if (
        provenance["license_observations"] != license_evidence["observations"]
        or provenance["platform_requirements"] != license_evidence["platform_requirements"]
        or provenance["marker_environment"] != license_evidence["marker_environment"]
    ):
        raise ValueError("bundle license inputs do not match the expected candidate report")
    if provenance["candidate_report_sha256"] != _digest(candidate_report):
        raise ValueError("bundle provenance does not bind the expected candidate report")
    bindings = {
        "source_commit": "source_commit",
        "source_tree": "source_tree",
        "exported_tree": "exported_tree",
        "export_policy_sha256": "policy_sha256",
        "expected_repository": "expected_repository",
        "planned_tag": "planned_tag",
    }
    if any(provenance[key] != manifest[report_key] for key, report_key in bindings.items()):
        raise ValueError("bundle provenance does not match the expected candidate report")
    assets = _asset_provenance(inputs)
    if any(provenance[key] != value for key, value in assets.items()):
        raise ValueError("bundle provenance assets do not match the expected candidate report")
    return inputs
