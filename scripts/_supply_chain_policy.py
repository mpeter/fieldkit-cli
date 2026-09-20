#!/usr/bin/env python3
"""Dependency policy validation and supply-chain evidence normalization."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = Path("docs/release-readiness/dependency-security-policy.json")
DEPENDABOT_PATH = Path(".github/dependabot.yml")
DEPENDENCY_REVIEW_WORKFLOW_PATH = Path(".github/workflows/dependency-review.yml")
DEPENDENCY_AUDIT_WORKFLOW_PATH = Path(".github/workflows/dependency-audit.yml")
CODEQL_WORKFLOW_PATH = Path(".github/workflows/codeql.yml")
SCORECARD_WORKFLOW_PATH = Path(".github/workflows/scorecard.yml")
CODEOWNERS_PATH = Path(".github/CODEOWNERS")
REPOSITORY_POLICY_PATHS = (
    POLICY_PATH,
    DEPENDABOT_PATH,
    DEPENDENCY_REVIEW_WORKFLOW_PATH,
    DEPENDENCY_AUDIT_WORKFLOW_PATH,
    CODEQL_WORKFLOW_PATH,
    SCORECARD_WORKFLOW_PATH,
    CODEOWNERS_PATH,
)

_POLICY_KEYS = {
    "schema_version",
    "vulnerability_threshold",
    "allowed_spdx_licenses",
    "unknown_license_policy",
    "license_exceptions",
}
_EXCEPTION_KEYS = {
    "package_url",
    "spdx_license",
    "owner",
    "evidence_url",
    "rationale",
    "expires_on",
    "review_condition",
}
_UNKNOWN_LICENSES = frozenset({"", "NOASSERTION", "NONE", "UNKNOWN"})
_SEVERITY_RANK = {"low": 0, "moderate": 1, "high": 2, "critical": 3}
_SPDX_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*")
_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PYPI_NAME = re.compile(r"[-_.]+")
_CODEQL_ACTION_REVISION = "cdf488f595d80d6e07e03d4674febd5ab45fa938"
_SCORECARD_ACTION_REVISION = "2d1146689b8cda280b9bc96326124645441f03bc"


@dataclass(frozen=True)
class LicenseException:
    """One exact-package license-metadata exception with review bounds."""

    package_url: str
    spdx_license: str
    owner: str
    evidence_url: str
    rationale: str
    expires_on: date
    review_condition: str


@dataclass(frozen=True)
class DependencyPolicy:
    """Reviewed dependency acceptance policy."""

    schema_version: int
    vulnerability_threshold: str
    allowed_spdx_licenses: tuple[str, ...]
    unknown_license_policy: str
    license_exceptions: tuple[LicenseException, ...]


@dataclass(frozen=True, order=True)
class Finding:
    """One deterministic dependency-policy finding."""

    criterion_id: str
    subject: str
    message: str


@dataclass(frozen=True)
class ReviewReport:
    """Versioned dependency-review supplement result."""

    schema_version: int
    reviewed_changes: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "reviewed_changes": self.reviewed_changes,
            "findings": [asdict(finding) for finding in self.findings],
        }


@dataclass(frozen=True)
class PolicyReport:
    """Versioned repository policy-alignment result."""

    schema_version: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "findings": [asdict(finding) for finding in self.findings],
        }


@dataclass(frozen=True)
class AuditFinding:
    """Stable subset of one uv audit vulnerability."""

    package: str
    version: str
    advisory: str
    aliases: tuple[str, ...]
    fixed_versions: tuple[str, ...]
    link: str | None


@dataclass(frozen=True)
class AuditEvidence:
    """Attributable locked-graph audit evidence."""

    schema_version: int
    status: str
    scope: str
    revision: str
    tool_version: str
    policy_threshold: str
    audited_packages: int
    findings: tuple[AuditFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "scope": self.scope,
            "revision": self.revision,
            "tool_version": self.tool_version,
            "policy_threshold": self.policy_threshold,
            "audited_packages": self.audited_packages,
            "findings": [asdict(finding) for finding in self.findings],
        }


@dataclass(frozen=True, order=True)
class LicenseObservation:
    """One package license expression collected from a resolved candidate environment."""

    package_url: str
    license_expression: str


@dataclass(frozen=True)
class LicenseEvidence:
    """Candidate-bound license evidence for the resolved locked dependency graph."""

    schema_version: int
    status: str
    scope: str
    revision: str
    export_policy_sha256: str
    sbom_sha256: str
    observed_packages: int
    packages: tuple[LicenseObservation, ...]
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, object]:
        """Render stable retained license evidence."""
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "scope": self.scope,
            "revision": self.revision,
            "export_policy_sha256": self.export_policy_sha256,
            "sbom_sha256": self.sbom_sha256,
            "observed_packages": self.observed_packages,
            "packages": [asdict(package) for package in self.packages],
            "findings": [asdict(finding) for finding in self.findings],
        }


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


def _strings(value: object, subject: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{subject} must be a non-empty string list")
    result = tuple(_string(item, subject) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{subject} must not contain duplicates")
    return result


def _date(value: object, subject: str) -> date:
    text = _string(value, subject)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{subject} must be an ISO date") from exc


def _action_revisions(document: object, *, action: str) -> tuple[str, ...] | None:
    revisions: list[str] = []
    active_containers: set[int] = set()
    pending: list[tuple[object, bool]] = [(document, False)]
    while pending:
        current, exiting = pending.pop()
        if not isinstance(current, (dict, list)):
            continue
        identity = id(current)
        if exiting:
            active_containers.remove(identity)
            continue
        if identity in active_containers:
            return None
        active_containers.add(identity)
        pending.append((current, True))
        if isinstance(current, dict):
            reference = current.get("uses")
            if isinstance(reference, str):
                referenced_action, separator, found_revision = reference.partition("@")
                if separator and referenced_action.casefold() == action.casefold():
                    revisions.append(found_revision)
            pending.extend((value, False) for value in current.values())
        else:
            pending.extend((value, False) for value in current)
    return tuple(revisions)


def _uses_only_action_revision(workflow: str, *, action: str, revision: str) -> bool:
    try:
        import yaml
    except ImportError:
        return False

    try:
        document = yaml.safe_load(workflow)
    except yaml.YAMLError:
        return False
    revisions = _action_revisions(document, action=action)
    return revisions is not None and bool(revisions) and all(found == revision for found in revisions)


def load_policy(path: Path = REPO_ROOT / POLICY_PATH, *, today: date | None = None) -> DependencyPolicy:
    """Load and fail-closed validate the checked-in dependency policy."""
    raw = _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    _exact_keys(raw, _POLICY_KEYS, str(path))
    if raw["schema_version"] != 1:
        raise ValueError("dependency policy schema_version must be 1")
    threshold = _string(raw["vulnerability_threshold"], "vulnerability_threshold")
    if threshold not in {"low", "moderate", "high", "critical"}:
        raise ValueError("vulnerability_threshold is unsupported")
    unknown_policy = _string(raw["unknown_license_policy"], "unknown_license_policy")
    if unknown_policy != "fail-unless-excepted":
        raise ValueError("unknown_license_policy must be fail-unless-excepted")
    allowed = _strings(raw["allowed_spdx_licenses"], "allowed_spdx_licenses")
    if any(_SPDX_TOKEN.fullmatch(identifier) is None for identifier in allowed):
        raise ValueError("allowed_spdx_licenses must contain SPDX identifiers")

    exception_values = raw["license_exceptions"]
    if not isinstance(exception_values, list):
        raise ValueError("license_exceptions must be a list")
    exceptions: list[LicenseException] = []
    for index, value in enumerate(exception_values):
        subject = f"license_exceptions[{index}]"
        item = _object(value, subject)
        _exact_keys(item, _EXCEPTION_KEYS, subject)
        exception = LicenseException(
            package_url=_string(item["package_url"], f"{subject}.package_url"),
            spdx_license=_string(item["spdx_license"], f"{subject}.spdx_license"),
            owner=_string(item["owner"], f"{subject}.owner"),
            evidence_url=_string(item["evidence_url"], f"{subject}.evidence_url"),
            rationale=_string(item["rationale"], f"{subject}.rationale"),
            expires_on=_date(item["expires_on"], f"{subject}.expires_on"),
            review_condition=_string(item["review_condition"], f"{subject}.review_condition"),
        )
        if not exception.package_url.startswith("pkg:pypi/") or "@" not in exception.package_url:
            raise ValueError(f"{subject}.package_url must be an exact-version PyPI purl")
        if exception.spdx_license not in allowed:
            raise ValueError(f"{subject}.spdx_license must be allowlisted")
        if not exception.owner.startswith("@"):
            raise ValueError(f"{subject}.owner must be a GitHub handle")
        if not exception.evidence_url.startswith("https://"):
            raise ValueError(f"{subject}.evidence_url must use https")
        exceptions.append(exception)
    package_urls = tuple(item.package_url for item in exceptions)
    if len(package_urls) != len(set(package_urls)):
        raise ValueError("license exception package URLs must be unique")
    current_date = today or datetime.now(tz=UTC).date()
    if any(item.expires_on < current_date for item in exceptions):
        raise ValueError("dependency policy contains an expired license exception")
    return DependencyPolicy(1, threshold, allowed, unknown_policy, tuple(exceptions))


def validate_repository(repo_root: Path = REPO_ROOT, *, today: date | None = None) -> PolicyReport:
    """Validate that update, review, audit, analysis, and ownership controls match policy."""
    policy = load_policy(repo_root / POLICY_PATH, today=today)
    findings: list[Finding] = []
    dependency_review = (repo_root / DEPENDENCY_REVIEW_WORKFLOW_PATH).read_text(encoding="utf-8")
    for expected in (
        f"fail-on-severity: {policy.vulnerability_threshold}",
        "fail-on-scopes: runtime, development, unknown",
        "license-check: false",
        "--changes-env DEPENDENCY_CHANGES",
        "--upstream-outcome",
        "if: github.event.repository.visibility == 'public'",
    ):
        if expected not in dependency_review:
            findings.append(Finding("DEP201", str(DEPENDENCY_REVIEW_WORKFLOW_PATH), f"missing {expected!r}"))

    dependency_audit = (repo_root / DEPENDENCY_AUDIT_WORKFLOW_PATH).read_text(encoding="utf-8")
    for expected in (
        'version: "0.12.5"',
        "uv audit --locked --no-dev --output-format json",
        "uv audit --locked --only-dev --output-format json",
        "--scanner-exit-code",
    ):
        if expected not in dependency_audit:
            findings.append(Finding("DEP202", str(DEPENDENCY_AUDIT_WORKFLOW_PATH), f"missing {expected!r}"))

    codeql = (repo_root / CODEQL_WORKFLOW_PATH).read_text(encoding="utf-8")
    for expected in (
        "language: [python, actions]",
        "if: github.event.repository.visibility == 'public'",
    ):
        if expected not in codeql:
            findings.append(Finding("DEP203", str(CODEQL_WORKFLOW_PATH), f"missing {expected!r}"))
    for action in ("github/codeql-action/init", "github/codeql-action/analyze"):
        expected = f"{action}@{_CODEQL_ACTION_REVISION}"
        if not _uses_only_action_revision(codeql, action=action, revision=_CODEQL_ACTION_REVISION):
            findings.append(Finding("DEP203", str(CODEQL_WORKFLOW_PATH), f"missing {expected!r}"))

    scorecard = (repo_root / SCORECARD_WORKFLOW_PATH).read_text(encoding="utf-8")
    for expected in (
        "if: github.event.repository.visibility == 'public'",
        "publish_results: true",
    ):
        if expected not in scorecard:
            findings.append(Finding("DEP204", str(SCORECARD_WORKFLOW_PATH), f"missing {expected!r}"))
    scorecard_action = "ossf/scorecard-action"
    expected = f"{scorecard_action}@{_SCORECARD_ACTION_REVISION}"
    if not _uses_only_action_revision(scorecard, action=scorecard_action, revision=_SCORECARD_ACTION_REVISION):
        findings.append(Finding("DEP204", str(SCORECARD_WORKFLOW_PATH), f"missing {expected!r}"))

    dependabot = (repo_root / DEPENDABOT_PATH).read_text(encoding="utf-8")
    for ecosystem in ('package-ecosystem: "uv"', 'package-ecosystem: "github-actions"'):
        if dependabot.count(ecosystem) != 1:
            findings.append(Finding("DEP205", str(DEPENDABOT_PATH), f"expected one {ecosystem!r} entry"))

    codeowners = (repo_root / CODEOWNERS_PATH).read_text(encoding="utf-8")
    for governed_path in (
        ".github/workflows/",
        ".github/dependabot.yml",
        "pyproject.toml",
        "uv.lock",
        "scripts/check_supply_chain_policy.py",
        "scripts/_supply_chain_policy.py",
        str(POLICY_PATH),
    ):
        if not any(line.split(maxsplit=1)[0] == governed_path for line in codeowners.splitlines() if line.strip()):
            findings.append(Finding("DEP206", str(CODEOWNERS_PATH), f"missing owner for {governed_path}"))
    return PolicyReport(1, tuple(sorted(findings)))


def _license_identifiers(expression: str) -> tuple[str, ...]:
    tokens = tuple(_SPDX_TOKEN.findall(expression))
    return tuple(token for token in tokens if token not in {"AND", "OR", "WITH"})


def package_url(name: str, version: str) -> str:
    """Return the canonical exact-version PyPI package URL for metadata evidence."""
    normalized_name = _PYPI_NAME.sub("-", name).casefold()
    return f"pkg:pypi/{normalized_name}@{version}"


def build_license_evidence(
    raw_packages: object,
    policy: DependencyPolicy,
    *,
    scope: str,
    revision: str,
    export_policy_sha256: str,
    sbom_sha256: str,
    expected_package_urls: tuple[str, ...],
    today: date | None = None,
) -> LicenseEvidence:
    """Evaluate every observed resolved dependency license against the one policy authority."""
    if scope not in {"locked-all-groups-all-extras", "runtime-all-extras", "runtime-default"}:
        raise ValueError("license evidence scope is unsupported")
    if _REVISION.fullmatch(revision) is None:
        raise ValueError("revision must be a 40-character lowercase Git SHA")
    if _SHA256.fullmatch(export_policy_sha256) is None:
        raise ValueError("export_policy_sha256 must be a 64-character lowercase SHA-256")
    if _SHA256.fullmatch(sbom_sha256) is None:
        raise ValueError("sbom_sha256 must be a 64-character lowercase SHA-256")
    if not isinstance(raw_packages, list):
        raise ValueError("resolved package inventory must be a list")
    if len(expected_package_urls) != len(set(expected_package_urls)):
        raise ValueError("expected package URLs must be unique")
    if any(not package_url.startswith("pkg:pypi/") or "@" not in package_url for package_url in expected_package_urls):
        raise ValueError("expected package URLs must be exact-version PyPI purls")

    observations: list[LicenseObservation] = []
    seen_urls: set[str] = set()
    for index, value in enumerate(raw_packages):
        package = _object(value, f"resolved_packages[{index}]")
        _exact_keys(package, {"name", "version", "license_expression"}, f"resolved_packages[{index}]")
        resolved_package_url = package_url(
            _string(package["name"], f"resolved_packages[{index}].name"),
            _string(package["version"], f"resolved_packages[{index}].version"),
        )
        if resolved_package_url in seen_urls:
            raise ValueError(f"resolved package inventory has duplicate package URL: {resolved_package_url}")
        seen_urls.add(resolved_package_url)
        raw_expression = package["license_expression"]
        expression = raw_expression.strip() if isinstance(raw_expression, str) else ""
        observations.append(LicenseObservation(resolved_package_url, expression or "UNKNOWN"))

    current_date = today or datetime.now(tz=UTC).date()
    exceptions = {item.package_url: item for item in policy.license_exceptions}
    findings: list[Finding] = []
    for observation in observations:
        if observation.license_expression.upper() in _UNKNOWN_LICENSES:
            exception = exceptions.get(observation.package_url)
            if exception is None:
                findings.append(
                    Finding("DEP302", observation.package_url, "license metadata is unknown and has no exception")
                )
            elif exception.expires_on < current_date:
                findings.append(
                    Finding(
                        "DEP303",
                        observation.package_url,
                        f"license exception expired on {exception.expires_on.isoformat()}",
                    )
                )
            continue
        identifiers = _license_identifiers(observation.license_expression)
        disallowed = sorted(set(identifiers) - set(policy.allowed_spdx_licenses))
        if not identifiers or disallowed:
            detail = ", ".join(disallowed) if disallowed else observation.license_expression
            findings.append(Finding("DEP301", observation.package_url, f"license is not allowlisted: {detail}"))
    observed_urls = {observation.package_url for observation in observations}
    expected_urls = set(expected_package_urls)
    for unexpected_package_url in sorted(observed_urls - expected_urls):
        findings.append(
            Finding("DEP305", unexpected_package_url, "resolved inventory package is absent from locked requirements")
        )
    ordered_observations = tuple(sorted(observations))
    ordered_findings = tuple(sorted(findings))
    return LicenseEvidence(
        1,
        "pass" if not ordered_findings else "fail",
        scope,
        revision,
        export_policy_sha256,
        sbom_sha256,
        len(ordered_observations),
        ordered_observations,
        ordered_findings,
    )


def review_dependency_changes(
    raw_changes: object,
    policy: DependencyPolicy,
    *,
    today: date | None = None,
    upstream_outcome: str = "success",
) -> ReviewReport:
    """Enforce allowlisted and explicitly reviewed license metadata."""
    if not isinstance(raw_changes, list):
        raise ValueError("dependency changes must be a list")
    current_date = today or datetime.now(tz=UTC).date()
    exceptions = {item.package_url: item for item in policy.license_exceptions}
    findings: list[Finding] = []
    reviewed = 0
    for index, value in enumerate(raw_changes):
        change = _object(value, f"dependency_changes[{index}]")
        if change.get("change_type") != "added":
            continue
        reviewed += 1
        package_url = _string(change.get("package_url"), f"dependency_changes[{index}].package_url")
        raw_license = change.get("license")
        license_expression = raw_license.strip() if isinstance(raw_license, str) else ""
        if license_expression.upper() in _UNKNOWN_LICENSES:
            exception = exceptions.get(package_url)
            if exception is None:
                findings.append(Finding("DEP102", package_url, "license metadata is unknown and has no exception"))
            elif exception.expires_on < current_date:
                findings.append(
                    Finding("DEP103", package_url, f"license exception expired on {exception.expires_on.isoformat()}")
                )
        else:
            identifiers = _license_identifiers(license_expression)
            disallowed = sorted(set(identifiers) - set(policy.allowed_spdx_licenses))
            if not identifiers or disallowed:
                detail = ", ".join(disallowed) if disallowed else license_expression
                findings.append(Finding("DEP101", package_url, f"license is not allowlisted: {detail}"))
        raw_vulnerabilities = change.get("vulnerabilities", [])
        if not isinstance(raw_vulnerabilities, list):
            raise ValueError(f"dependency_changes[{index}].vulnerabilities must be a list")
        for vulnerability_index, vulnerability_value in enumerate(raw_vulnerabilities):
            vulnerability = _object(
                vulnerability_value, f"dependency_changes[{index}].vulnerabilities[{vulnerability_index}]"
            )
            severity = _string(vulnerability.get("severity"), "vulnerability severity")
            if severity not in _SEVERITY_RANK:
                raise ValueError(f"unsupported vulnerability severity: {severity}")
            if _SEVERITY_RANK[severity] < _SEVERITY_RANK[policy.vulnerability_threshold]:
                continue
            advisory = _string(vulnerability.get("advisory_ghsa_id"), "vulnerability advisory")
            advisory_url = _string(vulnerability.get("advisory_url"), "vulnerability advisory_url")
            findings.append(Finding("DEP104", package_url, f"{severity} vulnerability {advisory}: {advisory_url}"))
    if upstream_outcome != "success" and not findings:
        findings.append(Finding("DEP100", "dependency-review-action", f"upstream outcome was {upstream_outcome}"))
    return ReviewReport(1, reviewed, tuple(sorted(findings)))


def build_audit_evidence(
    raw: object,
    policy: DependencyPolicy,
    *,
    scope: str,
    revision: str,
    tool_version: str,
    scanner_exit_code: int,
) -> AuditEvidence:
    """Validate uv audit output and retain a stable, attributable evidence subset."""
    if scanner_exit_code not in {0, 1}:
        raise ValueError(f"scanner exited with {scanner_exit_code}; no valid audit result is available")
    if scope not in {"runtime-all-extras", "development"}:
        raise ValueError("audit scope is unsupported")
    if _REVISION.fullmatch(revision) is None:
        raise ValueError("revision must be a 40-character lowercase Git SHA")
    tool = _string(tool_version, "tool_version")
    document = _object(raw, "uv audit result")
    schema = _object(document.get("schema"), "uv audit schema")
    if schema.get("version") != "preview":
        raise ValueError("uv audit schema version is unsupported")
    summary = _object(document.get("summary"), "uv audit summary")
    audited_packages = summary.get("audited_packages")
    if not isinstance(audited_packages, int) or audited_packages < 0:
        raise ValueError("uv audit audited_packages must be a non-negative integer")
    raw_vulnerabilities = document.get("vulnerabilities")
    if not isinstance(raw_vulnerabilities, list):
        raise ValueError("uv audit vulnerabilities must be a list")
    findings: list[AuditFinding] = []
    for index, value in enumerate(raw_vulnerabilities):
        item = _object(value, f"vulnerabilities[{index}]")
        dependency = _object(item.get("dependency"), f"vulnerabilities[{index}].dependency")
        aliases_value = item.get("aliases", [])
        fixes_value = item.get("fix_versions", [])
        aliases = () if aliases_value == [] else _strings(aliases_value, f"vulnerabilities[{index}].aliases")
        fixes = () if fixes_value == [] else _strings(fixes_value, f"vulnerabilities[{index}].fix_versions")
        link_value = item.get("link")
        if link_value is not None and not isinstance(link_value, str):
            raise ValueError(f"vulnerabilities[{index}].link must be a string or null")
        findings.append(
            AuditFinding(
                package=_string(dependency.get("name"), f"vulnerabilities[{index}].dependency.name"),
                version=_string(dependency.get("version"), f"vulnerabilities[{index}].dependency.version"),
                advisory=_string(item.get("display_id") or item.get("id"), f"vulnerabilities[{index}].id"),
                aliases=aliases,
                fixed_versions=fixes,
                link=link_value,
            )
        )
    expected_exit = 1 if findings else 0
    if scanner_exit_code != expected_exit:
        raise ValueError("scanner exit code and vulnerability count disagree")
    return AuditEvidence(
        1,
        "fail" if findings else "pass",
        scope,
        revision,
        tool,
        policy.vulnerability_threshold,
        audited_packages,
        tuple(findings),
    )
