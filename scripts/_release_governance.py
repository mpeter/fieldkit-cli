"""Fail-closed validation for candidate-bound release-governance policy."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_POLICY_KEYS = {"schema_version", "candidate", "roles", "support", "external_controls"}
_PLANNED_CANDIDATE_KEYS = {"repository", "package", "planned_tag"}
_EVIDENCE_CANDIDATE_KEYS = {"repository", "revision", "package", "planned_tag"}
_ROLE_KEYS = {"preparer", "approver", "incident"}
_SUPPORT_KEYS = {"compatibility_policy", "supported_line"}
_CONTROL_KEYS = {"id", "status", "evidence"}
_EVIDENCE_KEYS = {"candidate", "record"}
_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_CONTROL_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_REQUIRED_CONTROL_IDS = frozenset({"github-release-environment", "pypi-trusted-publisher"})
_CANDIDATE_REPORT_KEYS = {
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
_EXPORT_MANIFEST_KEYS = {
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
_ARTIFACT_VALIDATION_KEYS = {"schema_version", "status", "source_revision", "artifacts"}
_LICENSE_EVIDENCE_KEYS = {
    "schema_version",
    "status",
    "scope",
    "revision",
    "export_policy_sha256",
    "sbom_sha256",
    "observed_packages",
    "packages",
    "findings",
}
_SCAN_KEYS = {
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
_ARTIFACT_KEYS = {"name", "kind", "sha256", "criteria", "status"}
_CRITERION_KEYS = {"criterion_id", "status", "diagnostics"}
_SCAN_ARTIFACT_KEYS = {"name", "kind", "sha256", "member_count", "total_uncompressed_bytes"}
_TREE_ENTRY_KEYS = {"path", "mode", "oid", "category", "rule_id"}
_REQUIRED_ARTIFACT_KINDS = frozenset({"wheel", "sdist"})
_MAX_JSON_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class PlannedCandidate:
    """The repository/package/tag combination approved for future evidence."""

    repository: str
    package: str
    planned_tag: str


@dataclass(frozen=True)
class CandidateIdentity:
    """The immutable candidate constructed from policy and verified evidence."""

    repository: str
    revision: str
    package: str
    planned_tag: str


@dataclass(frozen=True)
class ExternalControl:
    """One control whose evidence is scoped to exactly one candidate."""

    identifier: str
    status: Literal["pending", "evidenced"]
    evidence: CandidateIdentity | None


@dataclass(frozen=True)
class GovernancePolicy:
    """Versioned governance commitments for one release candidate."""

    candidate: PlannedCandidate
    roles: dict[str, str]
    compatibility_policy: str
    supported_line: str
    controls: tuple[ExternalControl, ...]


@dataclass(frozen=True)
class GovernanceReport:
    """A deterministic release-governance decision."""

    schema_version: int
    status: Literal["pass", "pending"]
    publication_authorized: bool
    pending_controls: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Render the result for a maintainer or automation boundary."""
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "publication_authorized": self.publication_authorized,
            "pending_controls": list(self.pending_controls),
        }


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{path} must be a regular file")
            raw_bytes = stream.read(_MAX_JSON_BYTES + 1)
        if len(raw_bytes) > _MAX_JSON_BYTES:
            raise ValueError(f"{path} exceeds the {_MAX_JSON_BYTES}-byte JSON limit")
        raw = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc
    return _object(raw, str(path))


def _object(value: object, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{subject} must be an object with string keys")
    return {str(key): item for key, item in value.items()}


def _exact_keys(value: dict[str, Any], expected: set[str], subject: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{subject} keys must be exactly {sorted(expected)}")


def _string(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{subject} must be a non-empty string")
    return value.strip()


def _planned_candidate(value: object, subject: str) -> PlannedCandidate:
    raw = _object(value, subject)
    _exact_keys(raw, _PLANNED_CANDIDATE_KEYS, subject)
    repository = _string(raw["repository"], f"{subject}.repository")
    package = _string(raw["package"], f"{subject}.package")
    planned_tag = _string(raw["planned_tag"], f"{subject}.planned_tag")
    if _REPOSITORY.fullmatch(repository) is None:
        raise ValueError(f"{subject}.repository must use literal OWNER/REPO form")
    if not planned_tag.startswith("v"):
        raise ValueError(f"{subject}.planned_tag must start with v")
    return PlannedCandidate(repository, package, planned_tag)


def _evidence_candidate(value: object, subject: str) -> CandidateIdentity:
    raw = _object(value, subject)
    _exact_keys(raw, _EVIDENCE_CANDIDATE_KEYS, subject)
    repository = _string(raw["repository"], f"{subject}.repository")
    package = _string(raw["package"], f"{subject}.package")
    planned_tag = _string(raw["planned_tag"], f"{subject}.planned_tag")
    revision = _string(raw["revision"], f"{subject}.revision")
    if _REPOSITORY.fullmatch(repository) is None:
        raise ValueError(f"{subject}.repository must use literal OWNER/REPO form")
    if _REVISION.fullmatch(revision) is None:
        raise ValueError(f"{subject}.revision must be a full lowercase Git SHA")
    if not planned_tag.startswith("v"):
        raise ValueError(f"{subject}.planned_tag must start with v")
    return CandidateIdentity(repository, revision, package, planned_tag)


def _artifact_identities(value: object, subject: str, *, validation: bool) -> frozenset[tuple[str, str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{subject} must include artifacts")
    identities: set[tuple[str, str, str]] = set()
    for index, item in enumerate(value):
        artifact_subject = f"{subject}[{index}]"
        artifact = _object(item, artifact_subject)
        _exact_keys(artifact, _ARTIFACT_KEYS if validation else _SCAN_ARTIFACT_KEYS, artifact_subject)
        name = _string(artifact["name"], f"{artifact_subject}.name")
        kind = artifact["kind"]
        digest = artifact["sha256"]
        if not isinstance(kind, str) or kind not in _REQUIRED_ARTIFACT_KINDS:
            raise ValueError(f"{artifact_subject}.kind must be wheel or sdist")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError(f"{artifact_subject}.sha256 must be a lowercase SHA-256")
        if validation:
            if artifact["status"] != "pass":
                raise ValueError(f"{artifact_subject}.status must pass")
            criteria = artifact["criteria"]
            if not isinstance(criteria, list) or not criteria:
                raise ValueError(f"{artifact_subject}.criteria must be a non-empty list")
            for criterion_index, criterion_value in enumerate(criteria):
                criterion_subject = f"{artifact_subject}.criteria[{criterion_index}]"
                criterion = _object(criterion_value, criterion_subject)
                _exact_keys(criterion, _CRITERION_KEYS, criterion_subject)
                _string(criterion["criterion_id"], f"{criterion_subject}.criterion_id")
                if criterion["status"] != "pass":
                    raise ValueError(f"{criterion_subject}.status must pass")
                if not isinstance(criterion["diagnostics"], list) or criterion["diagnostics"]:
                    raise ValueError(f"{criterion_subject}.diagnostics must be an empty list")
        else:
            for field in ("member_count", "total_uncompressed_bytes"):
                if not isinstance(artifact[field], int) or isinstance(artifact[field], bool) or artifact[field] < 0:
                    raise ValueError(f"{artifact_subject}.{field} must be a non-negative integer")
        identities.add((kind, name, digest))
    if (
        len(value) != len(_REQUIRED_ARTIFACT_KINDS)
        or {identity[0] for identity in identities} != _REQUIRED_ARTIFACT_KINDS
    ):
        raise ValueError(f"{subject} must contain one wheel and one sdist")
    return frozenset(identities)


def _tree_entries(value: object, subject: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{subject} must be a list")
    for index, item in enumerate(value):
        entry_subject = f"{subject}[{index}]"
        entry = _object(item, entry_subject)
        _exact_keys(entry, _TREE_ENTRY_KEYS, entry_subject)
        for field in ("path", "category", "rule_id"):
            _string(entry[field], f"{entry_subject}.{field}")
        if not isinstance(entry["mode"], str) or entry["mode"] not in {"100644", "100755", "120000"}:
            raise ValueError(f"{entry_subject}.mode is unsupported")
        oid = entry["oid"]
        if not isinstance(oid, str) or _REVISION.fullmatch(oid) is None:
            raise ValueError(f"{entry_subject}.oid must be a full lowercase Git SHA")


def _candidate_report(path: Path, candidate: PlannedCandidate) -> CandidateIdentity:
    raw = _load_json(path)
    _exact_keys(raw, _CANDIDATE_REPORT_KEYS, "candidate report")
    if raw["schema_version"] != 6:
        raise ValueError("candidate report schema_version must be 6")
    if raw.get("status") != "pass":
        raise ValueError("candidate report must have pass status")
    if raw.get("expected_repository") != candidate.repository:
        raise ValueError("candidate report repository does not bind the policy candidate")
    if raw.get("planned_tag") != candidate.planned_tag:
        raise ValueError("candidate report planned tag does not bind the policy candidate")
    if raw.get("package") != candidate.package:
        raise ValueError("candidate report package does not bind the policy candidate")
    export_manifest = _object(raw["export_manifest"], "candidate report export_manifest")
    _exact_keys(export_manifest, _EXPORT_MANIFEST_KEYS, "candidate report export_manifest")
    revision = export_manifest["source_commit"]
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise ValueError("candidate report source_commit must be a full lowercase Git SHA")
    if export_manifest["schema_version"] != 1:
        raise ValueError("candidate report export manifest schema_version must be 1")
    for field in ("source_tree", "policy_oid", "exported_tree"):
        value = export_manifest[field]
        if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
            raise ValueError(f"candidate report export manifest {field} must be a full lowercase Git SHA")
    if export_manifest["policy_path"] != "docs/release-readiness/public-tree-policy.json":
        raise ValueError("candidate report export manifest policy path is unsupported")
    if (
        not isinstance(export_manifest["policy_sha256"], str)
        or _SHA256.fullmatch(export_manifest["policy_sha256"]) is None
    ):
        raise ValueError("candidate report export manifest policy SHA-256 is invalid")
    _tree_entries(export_manifest["included"], "candidate report export_manifest.included")
    _tree_entries(export_manifest["excluded"], "candidate report export_manifest.excluded")
    if (
        export_manifest["expected_repository"] != candidate.repository
        or export_manifest["planned_tag"] != candidate.planned_tag
    ):
        raise ValueError("candidate report export manifest does not bind the policy candidate")
    artifact_validation = _object(raw["artifact_validation"], "candidate report artifact_validation")
    _exact_keys(artifact_validation, _ARTIFACT_VALIDATION_KEYS, "candidate report artifact_validation")
    if artifact_validation["schema_version"] != 1 or artifact_validation["status"] != "pass":
        raise ValueError("candidate report artifact validation must pass")
    if artifact_validation["source_revision"] != revision:
        raise ValueError("candidate report artifact validation does not bind the source revision")
    validation_artifacts = _artifact_identities(
        artifact_validation["artifacts"], "candidate report artifact_validation.artifacts", validation=True
    )
    license_evidence = _object(raw["license_evidence"], "candidate report license_evidence")
    _exact_keys(license_evidence, _LICENSE_EVIDENCE_KEYS, "candidate report license_evidence")
    if license_evidence["schema_version"] != 1 or license_evidence["status"] != "pass":
        raise ValueError("candidate report license evidence must pass")
    if license_evidence["revision"] != revision:
        raise ValueError("candidate report license evidence does not bind the source revision")
    if license_evidence["scope"] != "runtime-all-extras":
        raise ValueError("candidate report license evidence scope is unsupported")
    if license_evidence["export_policy_sha256"] != export_manifest["policy_sha256"]:
        raise ValueError("candidate report license evidence does not bind the export policy")
    if (
        not isinstance(license_evidence["sbom_sha256"], str)
        or _SHA256.fullmatch(license_evidence["sbom_sha256"]) is None
    ):
        raise ValueError("candidate report license evidence SBOM SHA-256 is invalid")
    if (
        not isinstance(license_evidence["observed_packages"], int)
        or isinstance(license_evidence["observed_packages"], bool)
        or license_evidence["observed_packages"] < 0
        or not isinstance(license_evidence["packages"], list)
        or not all(isinstance(package, dict) for package in license_evidence["packages"])
        or not isinstance(license_evidence["findings"], list)
        or license_evidence["observed_packages"] != len(license_evidence["packages"])
        or license_evidence["findings"]
    ):
        raise ValueError("candidate report license evidence must be internally passing")
    runtime_requirements = _object(raw["runtime_requirements"], "candidate report runtime_requirements")
    _exact_keys(runtime_requirements, {"name", "sha256"}, "candidate report runtime_requirements")
    if runtime_requirements["name"] != "runtime-requirements.txt":
        raise ValueError("candidate report runtime requirements filename is unsupported")
    if not isinstance(runtime_requirements["sha256"], str) or _SHA256.fullmatch(runtime_requirements["sha256"]) is None:
        raise ValueError("candidate report runtime requirements SHA-256 is invalid")
    build_requirements = _object(raw["release_build_requirements"], "candidate report release build requirements")
    _exact_keys(build_requirements, {"name", "sha256"}, "candidate report release build requirements")
    if build_requirements["name"] != "release-build-requirements.txt":
        raise ValueError("candidate report release build requirements filename is unsupported")
    if not isinstance(build_requirements["sha256"], str) or _SHA256.fullmatch(build_requirements["sha256"]) is None:
        raise ValueError("candidate report release build requirements SHA-256 is invalid")
    runtime_wheelhouse = _object(raw["runtime_wheelhouse"], "candidate report runtime_wheelhouse")
    _exact_keys(runtime_wheelhouse, {"name", "sha256"}, "candidate report runtime_wheelhouse")
    if runtime_wheelhouse["name"] != "runtime-wheelhouse.zip":
        raise ValueError("candidate report runtime wheelhouse filename is unsupported")
    if not isinstance(runtime_wheelhouse["sha256"], str) or _SHA256.fullmatch(runtime_wheelhouse["sha256"]) is None:
        raise ValueError("candidate report runtime wheelhouse SHA-256 is invalid")
    scan = _object(raw["scan"], "candidate report scan")
    _exact_keys(scan, _SCAN_KEYS, "candidate report scan")
    if scan["schema_version"] != 1 or scan["status"] != "pass" or scan["artifact_coverage"] != "pass":
        raise ValueError("candidate report scan must pass with complete artifact coverage")
    if scan["source_commit"] != revision:
        raise ValueError("candidate report scan does not bind the source revision")
    if scan["expected_repository"] != candidate.repository or scan["planned_tag"] != candidate.planned_tag:
        raise ValueError("candidate report scan does not bind the policy candidate")
    for scan_field, manifest_field in (
        ("source_tree", "source_tree"),
        ("exported_tree", "exported_tree"),
        ("export_policy_oid", "policy_oid"),
        ("export_policy_sha256", "policy_sha256"),
    ):
        if scan[scan_field] != export_manifest[manifest_field]:
            raise ValueError(f"candidate report scan does not bind the export manifest {manifest_field}")
    if scan["findings"] or not isinstance(scan["findings"], list):
        raise ValueError("candidate report scan findings must be an empty list")
    if scan["gitleaks_version"] != "8.30.1" or scan["gitleaks_findings"] != 0:
        raise ValueError("candidate report scan must include a clean pinned secret scan")
    for field in (
        "scanned_entries",
        "scanned_artifact_entries",
        "scanned_text_entries",
        "approved_binary_entries",
        "classified_matches",
        "gitleaks_findings",
    ):
        if not isinstance(scan[field], int) or isinstance(scan[field], bool) or scan[field] < 0:
            raise ValueError(f"candidate report scan {field} must be a non-negative integer")
    scan_artifacts = _artifact_identities(scan["artifacts"], "candidate report scan.artifacts", validation=False)
    if scan_artifacts != validation_artifacts:
        raise ValueError("candidate report scan artifacts do not match artifact validation")
    return CandidateIdentity(candidate.repository, revision, candidate.package, candidate.planned_tag)


def load_policy(path: Path) -> GovernancePolicy:
    """Load and strictly validate one checked-in release-governance policy."""
    raw = _load_json(path)
    _exact_keys(raw, _POLICY_KEYS, str(path))
    if raw["schema_version"] != 1:
        raise ValueError("release governance policy schema_version must be 1")
    candidate = _planned_candidate(raw["candidate"], "candidate")

    raw_roles = _object(raw["roles"], "roles")
    _exact_keys(raw_roles, _ROLE_KEYS, "roles")
    roles = {name: _string(raw_roles[name], f"roles.{name}") for name in sorted(_ROLE_KEYS)}

    support = _object(raw["support"], "support")
    _exact_keys(support, _SUPPORT_KEYS, "support")
    compatibility_policy = _string(support["compatibility_policy"], "support.compatibility_policy")
    supported_line = _string(support["supported_line"], "support.supported_line")

    raw_controls = raw["external_controls"]
    if not isinstance(raw_controls, list) or not raw_controls:
        raise ValueError("external_controls must be a non-empty list")
    controls: list[ExternalControl] = []
    for index, raw_control in enumerate(raw_controls):
        subject = f"external_controls[{index}]"
        control = _object(raw_control, subject)
        _exact_keys(control, _CONTROL_KEYS, subject)
        identifier = _string(control["id"], f"{subject}.id")
        if _CONTROL_ID.fullmatch(identifier) is None:
            raise ValueError(f"{subject}.id must be lowercase kebab-case")
        status = control["status"]
        evidence_candidate: CandidateIdentity | None = None
        if status == "pending":
            if control["evidence"] is not None:
                raise ValueError(f"{subject}.pending control must not include evidence")
        elif status == "evidenced":
            evidence = _object(control["evidence"], f"{subject}.evidence")
            _exact_keys(evidence, _EVIDENCE_KEYS, f"{subject}.evidence")
            evidence_candidate = _evidence_candidate(evidence["candidate"], f"{subject}.evidence.candidate")
            record = _string(evidence["record"], f"{subject}.evidence.record")
            if not record.startswith("https://"):
                raise ValueError(f"{subject}.evidence.record must use https")
        else:
            raise ValueError(f"{subject}.status must be pending or evidenced")
        controls.append(ExternalControl(identifier, status, evidence_candidate))
    identifiers = tuple(control.identifier for control in controls)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("external control ids must be unique")
    if frozenset(identifiers) != _REQUIRED_CONTROL_IDS:
        raise ValueError(f"external control ids must be exactly {sorted(_REQUIRED_CONTROL_IDS)}")
    return GovernancePolicy(candidate, roles, compatibility_policy, supported_line, tuple(controls))


def validate(policy_path: Path, candidate_report_path: Path) -> GovernanceReport:
    """Validate policy and candidate evidence without contacting external services."""
    policy = load_policy(policy_path)
    candidate = _candidate_report(candidate_report_path, policy.candidate)
    for control in policy.controls:
        if control.status == "evidenced" and control.evidence != candidate:
            raise ValueError(f"external control {control.identifier} evidence does not bind the policy candidate")
    pending = tuple(sorted(control.identifier for control in policy.controls if control.status == "pending"))
    return GovernanceReport(1, "pending" if pending else "pass", not pending, pending)
