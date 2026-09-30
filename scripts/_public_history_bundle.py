"""Successor preparation only; retained receipts cannot approve a live release.

The producer must have performed current verify_source before retaining report8.
Offline validation binds that receipt, not current ancestry/classification or a
protected successful workflow. Canonical reader dependencies are needed in this
preparation environment; the successor consumer integration is still pending.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from scripts import _release_bundle_evidence as evidence
from scripts import public_history_source, public_tree_scan

SOURCE_NAME = "public-history-source.json"
PENDING_CONTROLS = ("successor-protected-workflow", "successor-release-approval")


@dataclass(frozen=True)
class PreparedCandidate:
    """Exact producer receipt and its independently anchored canonical source."""

    source: public_history_source.PublicHistorySource
    source_bytes: bytes
    inputs: evidence.CandidateInputs


@dataclass(frozen=True)
class PreparationBundleReport:
    """Local integrity success, explicitly not publication authorization."""

    source_commit: str
    files: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        """Return local preparation integrity, never release readiness."""
        return True

    def to_dict(self) -> dict[str, object]:
        """Keep the unimplemented authority transitions visible."""
        return {
            "schema_version": 3,
            "status": "prepared",
            "release_ready": False,
            "pending_controls": list(PENDING_CONTROLS),
            "source_kind": "public-history",
            "source_commit": self.source_commit,
            "files": [{"name": name, "sha256": digest} for name, digest in self.files],
        }


def candidate_inputs(
    payload: bytes,
    source_bytes: bytes,
    *,
    expected_anchor: public_history_source.ApprovedCutoverAnchor,
    cutover_record: bytes,
) -> PreparedCandidate:
    """Accept only pending report8 and its complete, exact canonical source1."""
    if len(payload) > evidence._MAX_FILE_BYTES:
        raise ValueError("candidate report exceeds size limit")
    report = evidence._json_object(payload, "candidate report")
    required = {
        "schema_version",
        "status",
        "checks_status",
        "bundle_status",
        "source_kind",
        "source",
        "source_sha256",
        "expected_repository",
        "package",
        "planned_tag",
        "artifact_validation",
        "license_evidence",
        "runtime_requirements",
        "release_build_requirements",
        "runtime_wheelhouse",
        "scan",
    }
    if (
        set(report) != required
        or type(report["schema_version"]) is not int
        or report["schema_version"] != 8
        or report["status"] != "pending"
        or report["checks_status"] != "pass"
        or report["bundle_status"] != "not-assembled"
        or report["source_kind"] != "public-history"
    ):
        raise ValueError("candidate report has an unsupported preparation schema or status")
    digest = evidence._digest(source_bytes)
    if report["source_sha256"] != digest:
        raise ValueError("candidate source digest does not match the source contract")
    source = public_history_source.source_from_bytes(
        source_bytes,
        expected_anchor=expected_anchor,
        cutover_record=cutover_record,
    )
    inline_source = evidence._object(report["source"], "candidate inline source")
    inline_anchor = evidence._object(inline_source.get("anchor"), "candidate inline source anchor")
    if any(
        type(value) is not int
        for value in (
            inline_source.get("schema_version"),
            inline_source.get("repository_id"),
            inline_anchor.get("repository_id"),
        )
    ):
        raise ValueError("candidate inline source identities must have exact integer types")
    if (
        report["source"] != public_history_source._source_dict(source)
        or report["source_sha256"] != digest
        or report["expected_repository"] != source.repository
        or report["planned_tag"] != source.planned_tag
    ):
        raise ValueError("candidate report does not bind its complete canonical source contract")
    scan = evidence._object(report["scan"], "candidate scan")
    scan_anchor = evidence._object(scan.get("anchor"), "candidate scan anchor")
    if type(scan_anchor.get("repository_id")) is not int:
        raise ValueError("candidate scan anchor identity must have an exact integer type")
    entries = {entry.path: entry for entry in source.entries}
    for field, path in (
        ("scan_policy_oid", public_tree_scan.POLICY_PATH.as_posix()),
        ("identity_policy_oid", public_tree_scan.IDENTITY_POLICY_PATH.as_posix()),
    ):
        if path not in entries or scan.get(field) != entries[path].oid:
            raise ValueError("candidate scan policy does not bind the complete source inventory")
    if type(scan.get("scanned_entries")) is not int or scan["scanned_entries"] != len(entries):
        raise ValueError("candidate scan does not cover the complete source inventory")
    bindings: dict[str, object] = {
        "source_commit": source.source_commit,
        "source_tree": source.source_tree,
        "exported_tree": source.source_tree,
        "expected_repository": source.repository,
        "planned_tag": source.planned_tag,
        "export_policy_oid": source.policy_oid,
        "export_policy_sha256": source.policy_sha256,
        "source_kind": "public-history",
        "source_sha256": digest,
        "anchor": asdict(expected_anchor),
    }
    inputs = evidence._candidate_assets(
        report,
        source_commit=source.source_commit,
        policy_sha256=source.policy_sha256,
        scan_bindings=bindings,
        scan_schema=2,
    )
    return PreparedCandidate(source, source_bytes, inputs)


def provenance(prepared: PreparedCandidate, report_digest: str) -> bytes:
    """Serialize exact source and assets without asserting hosted approval."""
    return evidence._canonical_provenance_bytes(_provenance_payload(prepared, report_digest))


def _provenance_payload(prepared: PreparedCandidate, report_digest: str) -> dict[str, object]:
    """Project one validated candidate for encoding and checksum validation."""
    source = prepared.source
    return {
        **evidence._asset_provenance(prepared.inputs),
        "schema_version": 3,
        "source_kind": "public-history",
        "status": "prepared",
        "release_ready": False,
        "pending_controls": list(PENDING_CONTROLS),
        "candidate_report_sha256": report_digest,
        "source_commit": source.source_commit,
        "source_tree": source.source_tree,
        "expected_repository": source.repository,
        "repository_id": source.repository_id,
        "version": source.version,
        "planned_tag": source.planned_tag,
        "anchor": asdict(source.anchor),
        "source_contract": {"name": SOURCE_NAME, "sha256": evidence._digest(prepared.source_bytes)},
        "export_policy_sha256": source.policy_sha256,
    }


def verify(
    provenance_bytes: bytes,
    checksums: tuple[tuple[str, str], ...],
    candidate_report: bytes,
    source_bytes: bytes,
    *,
    expected_anchor: public_history_source.ApprovedCutoverAnchor,
    cutover_record: bytes,
) -> PreparedCandidate:
    """Reproduce all provenance from independently supplied trust and receipt."""
    prepared = candidate_inputs(
        candidate_report,
        source_bytes,
        expected_anchor=expected_anchor,
        cutover_record=cutover_record,
    )
    provenance_value = _provenance_payload(prepared, evidence._digest(candidate_report))
    expected = evidence._canonical_provenance_bytes(provenance_value)
    if provenance_bytes != expected:
        raise ValueError("successor bundle provenance does not bind the exact preparation candidate")
    evidence._verify_provenance_assets(
        provenance_value,
        checksums,
        extra_files={SOURCE_NAME: evidence._digest(source_bytes)},
    )
    return prepared
