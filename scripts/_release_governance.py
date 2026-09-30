"""Fail-closed validation for candidate-bound release-governance policy."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from scripts.public_history_source import ApprovedCutoverAnchor, PublicHistorySource

from scripts import _release_bundle_evidence
from scripts.json_policy import load_json_bytes

_POLICY_KEYS = {"schema_version", "candidate", "roles", "support", "external_controls"}
_PLANNED_CANDIDATE_KEYS = {"repository", "package", "planned_tag"}
_EVIDENCE_CANDIDATE_KEYS = {"repository", "revision", "package", "planned_tag"}
_ROLE_KEYS = {"preparer", "approver", "approver_login", "incident"}
_SUPPORT_KEYS = {"compatibility_policy", "supported_line"}
_CONTROL_KEYS = {"id", "status", "evidence"}
_EVIDENCE_KEYS = {"candidate", "record"}
_REVISION = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_CONTROL_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_REQUIRED_CONTROL_IDS = frozenset({"github-release-environment", "pypi-trusted-publisher"})
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


@dataclass(frozen=True)
class PublicHistoryGovernanceReport(GovernanceReport):
    """Successor preparation identity with unimplemented approval kept pending."""

    source: PublicHistorySource
    source_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            **super().to_dict(),
            "source_kind": "public-history",
            "source_sha256": self.source_sha256,
            "anchor": asdict(self.source.anchor),
            "repository": self.source.repository,
            "repository_id": self.source.repository_id,
            "source_commit": self.source.source_commit,
            "source_tree": self.source.source_tree,
            "version": self.source.version,
            "planned_tag": self.source.planned_tag,
        }


def _load_bytes(path: Path) -> bytes:
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
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc
    return raw_bytes


def _load_json(path: Path) -> dict[str, object]:
    return _object(load_json_bytes(_load_bytes(path)), "release governance JSON")


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


def _candidate_report(path: Path, candidate: PlannedCandidate) -> CandidateIdentity:
    inputs = _release_bundle_evidence._candidate_inputs(_load_bytes(path))
    raw = inputs.report
    if raw["expected_repository"] != candidate.repository:
        raise ValueError("candidate report repository does not bind the policy candidate")
    if raw["planned_tag"] != candidate.planned_tag:
        raise ValueError("candidate report planned tag does not bind the policy candidate")
    if raw["package"] != candidate.package:
        raise ValueError("candidate report package does not bind the policy candidate")
    manifest = _object(raw["export_manifest"], "candidate export manifest")
    revision = _string(manifest["source_commit"], "candidate source commit")
    return CandidateIdentity(candidate.repository, revision, candidate.package, candidate.planned_tag)


def load_policy(path: Path) -> GovernancePolicy:
    """Load and strictly validate one checked-in release-governance policy."""
    raw = _load_json(path)
    _exact_keys(raw, _POLICY_KEYS, str(path))
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("release governance policy schema_version must be 1")
    candidate = _planned_candidate(raw["candidate"], "candidate")

    raw_roles = _object(raw["roles"], "roles")
    _exact_keys(raw_roles, _ROLE_KEYS, "roles")
    roles = {name: _string(raw_roles[name], f"roles.{name}") for name in sorted(_ROLE_KEYS)}
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", roles["approver_login"]) is None:
        raise ValueError("roles.approver_login must be a GitHub login")

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


def validate(
    policy_path: Path,
    candidate_report_path: Path,
    *,
    expected_anchor: ApprovedCutoverAnchor | None = None,
    cutover_record: bytes | None = None,
) -> GovernanceReport:
    """Validate policy and candidate evidence without contacting external services."""
    policy = load_policy(policy_path)
    prepared = None
    successor_pending_controls: tuple[str, ...] = ()
    if expected_anchor is not None or cutover_record is not None:
        if expected_anchor is None or cutover_record is None:
            raise ValueError("successor governance requires independent anchor and exact cutover record")
        from scripts import _public_history_bundle, public_history_source, release_bundle

        successor_pending_controls = _public_history_bundle.PENDING_CONTROLS
        descriptor = release_bundle._open_directory(candidate_report_path.parent)
        try:
            prepared = _public_history_bundle.candidate_inputs(
                release_bundle._safe_bytes_at(descriptor, candidate_report_path.name),
                release_bundle._safe_bytes_at(
                    descriptor,
                    "manifest.json",
                    maximum_bytes=public_history_source.MAX_SOURCE_BYTES,
                ),
                expected_anchor=expected_anchor,
                cutover_record=cutover_record,
            )
        finally:
            os.close(descriptor)
        report = prepared.inputs.report
        if (report["expected_repository"], report["package"], report["planned_tag"]) != (
            policy.candidate.repository,
            policy.candidate.package,
            policy.candidate.planned_tag,
        ):
            raise ValueError("successor report does not bind the policy candidate")
        candidate = CandidateIdentity(
            policy.candidate.repository,
            prepared.source.source_commit,
            policy.candidate.package,
            policy.candidate.planned_tag,
        )
    else:
        candidate = _candidate_report(candidate_report_path, policy.candidate)
    for control in policy.controls:
        if control.status == "evidenced" and control.evidence != candidate:
            raise ValueError(f"external control {control.identifier} evidence does not bind the policy candidate")
    pending = tuple(sorted(control.identifier for control in policy.controls if control.status == "pending"))
    if prepared is not None:
        from scripts import _release_bundle_evidence

        return PublicHistoryGovernanceReport(
            2,
            "pending",
            False,
            tuple(sorted((*pending, *successor_pending_controls))),
            prepared.source,
            _release_bundle_evidence._digest(prepared.source_bytes),
        )
    return GovernanceReport(1, "pending" if pending else "pass", not pending, pending)
