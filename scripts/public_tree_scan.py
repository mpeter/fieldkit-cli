#!/usr/bin/env python3
"""Scan one independently verified public export without exposing matched payloads."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

if __package__:
    from scripts import check_artifacts, check_public_identity, export_public_tree, public_tree_artifacts
    from scripts.json_policy import reject_duplicate_json_keys
else:
    import check_artifacts
    import check_public_identity
    import export_public_tree
    import public_tree_artifacts
    from json_policy import reject_duplicate_json_keys

POLICY_PATH = Path("docs/release-readiness/public-tree-scan-policy.json")
IDENTITY_POLICY_PATH = check_public_identity.POLICY_PATH
_BINARY_CLASSIFICATIONS = frozenset({"public_product_asset"})
_REQUIRED_ARTIFACT_KINDS = frozenset({"wheel", "sdist"})
GITLEAKS_VERSION = "8.30.1"
GITLEAKS_LINUX_X64_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
_GITLEAKS_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class TextRule:
    """One content pattern that is forbidden unless narrowly classified."""

    rule_id: str
    category: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class TextAllowance:
    """One exact-path allowance for a text rule."""

    rule_id: str
    path: str
    pattern: re.Pattern[str]
    classification: str
    rationale: str
    expires_on: date | None


@dataclass(frozen=True)
class BinaryAllowance:
    """Cryptographic identity and review context for one public non-text file."""

    path: str
    blob_oid: str
    sha256: str
    media_type: str
    max_bytes: int
    classification: str
    rationale: str
    expires_on: date | None


@dataclass(frozen=True)
class ScanPolicy:
    """Validated public-tree scanning policy."""

    max_text_bytes: int
    archives: public_tree_artifacts.ArchiveLimits
    text_rules: tuple[TextRule, ...]
    text_allowances: tuple[TextAllowance, ...]
    binary_allowances: tuple[BinaryAllowance, ...]


@dataclass(frozen=True, order=True)
class Finding:
    """A payload-safe content finding."""

    rule_id: str
    category: str
    path: str
    line: int | None


@dataclass(frozen=True)
class ScanDocument:
    """One tree or artifact subject presented to the shared content rules."""

    subject_path: str
    policy_path: str
    data: bytes


@dataclass(frozen=True)
class ScanReport:
    """Machine-readable evidence bound to one verified public candidate."""

    schema_version: int
    source_commit: str
    source_tree: str
    exported_tree: str
    expected_repository: str
    planned_tag: str
    export_policy_oid: str
    export_policy_sha256: str
    scan_policy_oid: str
    scan_policy_sha256: str
    identity_policy_oid: str
    identity_policy_sha256: str
    scanned_entries: int
    scanned_artifact_entries: int
    scanned_text_entries: int
    approved_binary_entries: int
    classified_matches: int
    gitleaks_version: str
    gitleaks_findings: int
    artifacts: tuple[public_tree_artifacts.ArtifactEvidence, ...]
    findings: tuple[Finding, ...]

    @property
    def content_ok(self) -> bool:
        """Return whether all captured tree and artifact content passed policy."""
        return not self.findings

    @property
    def artifact_coverage(self) -> str:
        """Return whether both required release artifact kinds were scanned."""
        kinds = {artifact.kind for artifact in self.artifacts}
        return "pass" if kinds == _REQUIRED_ARTIFACT_KINDS else "pending"

    @property
    def ok(self) -> bool:
        """Return whether content and complete release-artifact coverage passed."""
        return self.content_ok and self.artifact_coverage == "pass"

    def to_dict(self) -> dict[str, object]:
        """Return the stable, payload-safe report shape."""
        status = "fail" if not self.content_ok else "pass" if self.ok else "pending"
        return {
            **asdict(self),
            "status": status,
            "artifact_coverage": self.artifact_coverage,
            "findings": [asdict(finding) for finding in self.findings],
        }


def _bind_artifact_evidence(
    manifest: export_public_tree.ExportManifest,
    artifacts: tuple[public_tree_artifacts.ArtifactEvidence, ...],
    validation: check_artifacts.ValidationReport | None,
) -> None:
    if not artifacts:
        if validation is not None:
            raise ValueError("artifact validation evidence was supplied without artifacts")
        return
    if validation is None:
        raise ValueError("artifact validation evidence is required when scanning artifacts")
    if validation.schema_version != 1 or not validation.ok:
        raise ValueError("artifact validation evidence must be a passing schema version 1 report")
    if validation.source_revision != manifest.source_commit:
        raise ValueError("artifact validation source revision does not match the verified export")
    expected = {(artifact.kind, artifact.name): artifact.sha256 for artifact in validation.artifacts}
    observed = {(artifact.kind, artifact.name): artifact.sha256 for artifact in artifacts}
    if len(expected) != len(validation.artifacts) or expected != observed:
        raise ValueError("artifact validation identities do not match the scanned artifacts")


def _object(value: object, subject: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{subject} must be an object with string keys")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _fields(entry: dict[str, object], required: set[str], optional: set[str], subject: str) -> None:
    missing = sorted(required - set(entry))
    unsupported = sorted(set(entry) - required - optional)
    if missing or unsupported:
        raise ValueError(f"{subject} fields are invalid: missing={missing}, unsupported={unsupported}")


def _string(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{subject} must be a non-empty string")
    return value


def _exact_path(value: object, subject: str) -> str:
    path = _string(value, subject)
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or path != pure.as_posix():
        raise ValueError(f"{subject} must be one canonical repository-relative path")
    return path


def _pattern(value: object, subject: str) -> re.Pattern[str]:
    try:
        return re.compile(_string(value, subject))
    except re.error as error:
        raise ValueError(f"{subject} is not a valid regular expression") from error


def _integer(value: object, subject: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{subject} must be a positive integer")
    return value


def _optional_date(value: object, subject: str) -> date | None:
    if value is None:
        return None
    raw = _string(value, subject)
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise ValueError(f"{subject} must be an ISO date") from error


def _load_policy(data: bytes, today: date) -> ScanPolicy:
    try:
        raw = _object(json.loads(data, object_pairs_hook=reject_duplicate_json_keys), str(POLICY_PATH))
    except UnicodeDecodeError as error:
        raise ValueError(f"{POLICY_PATH} must be UTF-8") from error
    if raw.get("schema_version") != 1:
        raise ValueError(f"{POLICY_PATH}: schema_version must be 1")
    _fields(
        raw,
        {"schema_version", "max_text_bytes", "archives", "text_rules", "text_allowances", "binary_allowances"},
        set(),
        str(POLICY_PATH),
    )
    raw_rules = raw.get("text_rules")
    raw_text_allowances = raw.get("text_allowances")
    raw_binary_allowances = raw.get("binary_allowances")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError(f"{POLICY_PATH}: text_rules must be a non-empty list")
    if not isinstance(raw_text_allowances, list) or not isinstance(raw_binary_allowances, list):
        raise ValueError(f"{POLICY_PATH}: allowances must be lists")
    archive_values = _object(raw.get("archives"), "archives")
    _fields(
        archive_values,
        {"max_members", "max_member_bytes", "max_total_bytes", "max_archive_bytes"},
        set(),
        "archives",
    )

    rules_list: list[TextRule] = []
    for index, value in enumerate(raw_rules):
        subject = f"text rule {index}"
        entry = _object(value, subject)
        _fields(entry, {"id", "category", "pattern"}, set(), subject)
        rules_list.append(
            TextRule(
                _string(entry["id"], f"{subject} id"),
                _string(entry["category"], f"{subject} category"),
                _pattern(entry["pattern"], f"{subject} pattern"),
            )
        )
    rules = tuple(rules_list)
    rule_ids = {rule.rule_id for rule in rules}
    if len(rule_ids) != len(rules):
        raise ValueError(f"{POLICY_PATH}: text rule IDs must be unique")

    text_allowance_list: list[TextAllowance] = []
    for index, value in enumerate(raw_text_allowances):
        subject = f"text allowance {index}"
        entry = _object(value, subject)
        _fields(entry, {"rule_id", "path", "pattern", "classification", "rationale"}, {"expires_on"}, subject)
        allowance = TextAllowance(
            _string(entry["rule_id"], f"{subject} rule_id"),
            _exact_path(entry["path"], f"{subject} path"),
            _pattern(entry["pattern"], f"{subject} pattern"),
            _string(entry["classification"], f"{subject} classification"),
            _string(entry["rationale"], f"{subject} rationale"),
            _optional_date(entry.get("expires_on"), f"{subject} expires_on"),
        )
        if allowance.expires_on is not None and allowance.expires_on < today:
            raise ValueError(f"{POLICY_PATH}: expired text allowance {allowance.rule_id}/{allowance.path}")
        text_allowance_list.append(allowance)
    text_allowances = tuple(text_allowance_list)
    if any(allowance.rule_id not in rule_ids for allowance in text_allowances):
        raise ValueError(f"{POLICY_PATH}: text allowance references an unknown rule")
    text_allowance_keys = {(allowance.rule_id, allowance.path) for allowance in text_allowances}
    if len(text_allowance_keys) != len(text_allowances):
        raise ValueError(f"{POLICY_PATH}: duplicate text allowances are not supported")

    binary_allowance_list: list[BinaryAllowance] = []
    for index, value in enumerate(raw_binary_allowances):
        subject = f"binary allowance {index}"
        entry = _object(value, subject)
        _fields(
            entry,
            {"path", "blob_oid", "sha256", "media_type", "max_bytes", "classification", "rationale"},
            {"expires_on"},
            subject,
        )
        allowance = BinaryAllowance(
            _exact_path(entry["path"], f"{subject} path"),
            _string(entry["blob_oid"], f"{subject} blob_oid"),
            _string(entry["sha256"], f"{subject} sha256"),
            _string(entry["media_type"], f"{subject} media_type"),
            _integer(entry["max_bytes"], f"{subject} max_bytes"),
            _string(entry["classification"], f"{subject} classification"),
            _string(entry["rationale"], f"{subject} rationale"),
            _optional_date(entry.get("expires_on"), f"{subject} expires_on"),
        )
        if allowance.expires_on is not None and allowance.expires_on < today:
            raise ValueError(f"{POLICY_PATH}: expired binary allowance {allowance.path}")
        binary_allowance_list.append(allowance)
    binary_allowances = tuple(binary_allowance_list)
    binary_paths = {allowance.path for allowance in binary_allowances}
    if len(binary_paths) != len(binary_allowances):
        raise ValueError(f"{POLICY_PATH}: duplicate binary allowance paths are not supported")
    if any(allowance.classification not in _BINARY_CLASSIFICATIONS for allowance in binary_allowances):
        raise ValueError(f"{POLICY_PATH}: unsupported binary allowance classification")
    if any(not re.fullmatch(r"[0-9a-f]{40}", allowance.blob_oid) for allowance in binary_allowances):
        raise ValueError(f"{POLICY_PATH}: binary blob_oid must be a Git SHA-1 object ID")
    if any(not re.fullmatch(r"[0-9a-f]{64}", allowance.sha256) for allowance in binary_allowances):
        raise ValueError(f"{POLICY_PATH}: binary sha256 must be lowercase hexadecimal")

    return ScanPolicy(
        max_text_bytes=_integer(raw.get("max_text_bytes"), "max_text_bytes"),
        archives=public_tree_artifacts.ArchiveLimits(
            max_members=_integer(archive_values["max_members"], "archives.max_members"),
            max_member_bytes=_integer(archive_values["max_member_bytes"], "archives.max_member_bytes"),
            max_total_bytes=_integer(archive_values["max_total_bytes"], "archives.max_total_bytes"),
            max_archive_bytes=_integer(archive_values["max_archive_bytes"], "archives.max_archive_bytes"),
        ),
        text_rules=rules,
        text_allowances=text_allowances,
        binary_allowances=binary_allowances,
    )


def _media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def _git_blob_oid(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def _is_text(data: bytes, max_bytes: int) -> bool:
    if len(data) > max_bytes or b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _gitleaks_findings(source: Path, *, subject_prefix: str) -> tuple[Finding, ...]:
    """Run the pinned external secret scanner without retaining matched payloads."""
    try:
        version = subprocess.run(["gitleaks", "version"], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"gitleaks is unavailable: {error}") from error
    if version.returncode != 0 or version.stdout.strip() != GITLEAKS_VERSION:
        raise ValueError(f"gitleaks must be exactly {GITLEAKS_VERSION}")
    with tempfile.TemporaryDirectory(prefix="fieldkit-gitleaks-") as temporary:
        report_path = Path(temporary) / "report.json"
        try:
            result = subprocess.run(
                [
                    "gitleaks",
                    "detect",
                    "--no-git",
                    "--source",
                    str(source),
                    "--max-archive-depth",
                    "2",
                    "--report-format",
                    "json",
                    "--report-path",
                    str(report_path),
                    "--redact",
                    "--exit-code",
                    "0",
                    "--no-banner",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_GITLEAKS_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"gitleaks scan failed: {error}") from error
        if result.returncode != 0 or not report_path.is_file():
            raise ValueError("gitleaks scan did not produce a report")
        try:
            findings = json.loads(report_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_json_keys)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise ValueError(f"gitleaks report is invalid: {error}") from error
    if not isinstance(findings, list):
        raise ValueError("gitleaks report must be a list")
    result_findings: list[Finding] = []
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("File"), str):
            raise ValueError("gitleaks report contains an invalid finding")
        line = finding.get("StartLine")
        if type(line) is not int or line < 1:
            line = None
        result_findings.append(Finding("GITLEAKS001", "secret", f"{subject_prefix}{Path(finding['File']).name}", line))
    return tuple(result_findings)


def _scan_with_gitleaks(destination: Path, artifacts: tuple[Path, ...]) -> tuple[Finding, ...]:
    """Scan the verified export plus the exact archive bytes promoted from it."""
    findings = list(_gitleaks_findings(destination, subject_prefix=""))
    if artifacts:
        with tempfile.TemporaryDirectory(prefix="fieldkit-gitleaks-artifacts-") as temporary:
            artifact_root = Path(temporary)
            for artifact in artifacts:
                shutil.copyfile(artifact, artifact_root / artifact.name)
            findings.extend(_gitleaks_findings(artifact_root, subject_prefix="artifact:"))
    return tuple(findings)


def scan_public_tree(
    repo: Path,
    destination: Path,
    manifest_path: Path,
    export_policy_path: Path,
    *,
    today: date | None = None,
    artifacts: tuple[Path, ...] = (),
    artifact_validation: check_artifacts.ValidationReport | None = None,
) -> ScanReport:
    """Verify and scan every included entry in one public export."""
    manifest = export_public_tree.verify_export(repo, destination, manifest_path, export_policy_path)
    entries = {entry.path: entry for entry in manifest.included}
    documents: dict[str, bytes] = {}
    for path, entry in sorted(entries.items()):
        data = (destination / path).read_bytes()
        if _git_blob_oid(data) != entry.oid:
            raise ValueError(f"verified export object mismatch after capture: {path}")
        documents[path] = data
    scan_policy_relative = POLICY_PATH.as_posix()
    identity_policy_relative = IDENTITY_POLICY_PATH.as_posix()
    if scan_policy_relative not in entries:
        raise ValueError(f"verified export does not include {POLICY_PATH}")
    if identity_policy_relative not in entries:
        raise ValueError(f"verified export does not include {IDENTITY_POLICY_PATH}")
    scan_policy_data = documents[scan_policy_relative]
    policy = _load_policy(scan_policy_data, today or datetime.now(tz=UTC).date())
    identity_policy_data = documents[identity_policy_relative]
    identity_policy = check_public_identity.parse_policy(identity_policy_data)
    artifact_results: list[public_tree_artifacts.ArtifactEvidence] = []
    scan_documents = [ScanDocument(path, path, data) for path, data in sorted(documents.items())]
    for artifact in artifacts:
        evidence, artifact_documents = public_tree_artifacts.read_artifact(artifact, policy.archives)
        artifact_results.append(evidence)
        scan_documents.extend(
            ScanDocument(document.subject_path, document.policy_path, document.data) for document in artifact_documents
        )
    artifact_kinds = [artifact.kind for artifact in artifact_results]
    if len(artifact_kinds) != len(set(artifact_kinds)):
        raise ValueError("provide at most one public artifact of each kind")
    _bind_artifact_evidence(manifest, tuple(artifact_results), artifact_validation)
    binary_allowances = {allowance.path: allowance for allowance in policy.binary_allowances}
    unknown_binary_allowances = sorted(set(binary_allowances) - set(entries))
    if unknown_binary_allowances:
        raise ValueError(f"{POLICY_PATH}: binary allowance path is not in the verified export")

    text_allowances = {(allowance.rule_id, allowance.path): allowance for allowance in policy.text_allowances}
    findings: list[Finding] = []
    used_text_allowances: set[tuple[str, str]] = set()
    used_binary_allowances: set[str] = set()
    scanned_text_entries = 0
    classified_matches = 0
    identity_documents: list[check_public_identity.Document] = []
    for document in scan_documents:
        path = document.subject_path
        policy_path = document.policy_path
        data = document.data
        for rule in policy.text_rules:
            if rule.pattern.search(policy_path):
                findings.append(Finding(rule.rule_id, rule.category, path, None))
        if not _is_text(data, policy.max_text_bytes):
            allowance = binary_allowances.get(policy_path)
            digest = hashlib.sha256(data).hexdigest()
            if (
                allowance is None
                or _git_blob_oid(data) != allowance.blob_oid
                or digest != allowance.sha256
                or len(data) > allowance.max_bytes
                or _media_type(data) != allowance.media_type
            ):
                findings.append(Finding("DATA001", "unapproved_binary_or_data", path, None))
            else:
                used_binary_allowances.add(policy_path)
            continue

        scanned_text_entries += 1
        text = data.decode("utf-8")
        if path != identity_policy_relative:
            identity_documents.append(check_public_identity.Document(path, policy_path, text))
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule in policy.text_rules:
                for match in rule.pattern.finditer(line):
                    allowance = text_allowances.get((rule.rule_id, policy_path))
                    allowed = allowance is not None and any(
                        candidate.start() <= match.start() and candidate.end() >= match.end()
                        for candidate in allowance.pattern.finditer(line)
                    )
                    if allowed:
                        classified_matches += 1
                        used_text_allowances.add((rule.rule_id, policy_path))
                    else:
                        findings.append(Finding(rule.rule_id, rule.category, path, line_number))

    identity_report = check_public_identity.scan_documents(
        identity_policy,
        identity_documents,
        frozenset(entries),
    )
    classified_matches += identity_report.classified_matches
    findings.extend(Finding(item.rule_id, item.category, item.path, item.line) for item in identity_report.findings)
    findings.extend(_scan_with_gitleaks(destination, artifacts))

    if set(binary_allowances) != used_binary_allowances:
        raise ValueError(f"{POLICY_PATH}: unused binary allowance")
    if set(text_allowances) != used_text_allowances:
        raise ValueError(f"{POLICY_PATH}: unused text allowance")

    return ScanReport(
        schema_version=1,
        source_commit=manifest.source_commit,
        source_tree=manifest.source_tree,
        exported_tree=manifest.exported_tree,
        expected_repository=manifest.expected_repository,
        planned_tag=manifest.planned_tag,
        export_policy_oid=manifest.policy_oid,
        export_policy_sha256=manifest.policy_sha256,
        scan_policy_oid=entries[scan_policy_relative].oid,
        scan_policy_sha256=hashlib.sha256(scan_policy_data).hexdigest(),
        identity_policy_oid=entries[identity_policy_relative].oid,
        identity_policy_sha256=hashlib.sha256(identity_policy_data).hexdigest(),
        scanned_entries=len(entries),
        scanned_artifact_entries=sum(artifact.member_count for artifact in artifact_results),
        scanned_text_entries=scanned_text_entries,
        approved_binary_entries=len(used_binary_allowances),
        classified_matches=classified_matches,
        gitleaks_version=GITLEAKS_VERSION,
        gitleaks_findings=sum(finding.rule_id == "GITLEAKS001" for finding in findings),
        artifacts=tuple(artifact_results),
        findings=tuple(sorted(set(findings), key=lambda finding: (finding.path, finding.line or 0, finding.rule_id))),
    )
