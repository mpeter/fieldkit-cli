#!/usr/bin/env python3
"""Read-only verification of live GitHub repository settings against policy."""

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

API_VERSION = "2026-03-10"
API_TIMEOUT_SECONDS = 30
MAX_SNAPSHOT_AGE_SECONDS = 900

Phase = Literal["pre-cutover", "post-cutover"]
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PLAN_MESSAGE = "Upgrade to GitHub Pro or make this repository public to enable this feature."
_REVISION = re.compile(r"[0-9a-f]{40}")
_COLLECTED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SURFACES = frozenset(
    {
        "repository",
        "rulesets",
        "actions_permissions",
        "actions_selected",
        "workflow_permissions",
        "vulnerability_alerts",
        "automated_security_fixes",
        "private_vulnerability_reporting",
        "security_and_analysis",
        "environments",
    }
)
# Release environments deploy only from SemVer tags: a branch-protection
# policy would reject the tag-triggered release jobs.
_TAG_ONLY_DEPLOYMENTS = {"protected_branches": False, "custom_branch_policies": True}


@dataclass(frozen=True)
class ApiObservation:
    """One authoritative API response."""

    status: int
    data: Any


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Repository-attributable observations from one collection instant."""

    repository: str
    collected_at: str
    api_version: str
    default_branch_sha: str
    observations: dict[str, ApiObservation]


@dataclass(frozen=True, order=True)
class Finding:
    """One unmet or not-yet-applicable repository control."""

    control: str
    message: str


@dataclass(frozen=True)
class VerificationReport:
    """Machine-readable comparison result."""

    schema_version: int
    phase: Phase
    status: Literal["pass", "pending", "fail"]
    failures: tuple[Finding, ...]
    pending: tuple[Finding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "phase": self.phase,
            "status": self.status,
            "failures": [asdict(finding) for finding in self.failures],
            "pending": [asdict(finding) for finding in self.pending],
        }


def validate_repository(repository: str) -> str:
    """Reject values that are not a literal GitHub OWNER/REPO pair."""
    if _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("repository must use the literal OWNER/REPO form")
    return repository


def _observation(raw: object) -> ApiObservation:
    if not isinstance(raw, dict) or not isinstance(raw.get("status"), int) or "data" not in raw:
        raise ValueError("every observation must contain integer status and data fields")
    return ApiObservation(status=raw["status"], data=raw["data"])


def load_snapshot(path: Path, repository: str) -> EvidenceSnapshot:
    """Load an attributable API snapshot used for offline verification."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("snapshot must be a JSON object")
    if raw.get("schema_version") != 1:
        raise ValueError("snapshot must use schema version 1")
    repository = validate_repository(repository)
    if raw.get("repository") != repository:
        raise ValueError("snapshot repository does not match requested repository")
    collected_at = raw.get("collected_at")
    if not isinstance(collected_at, str) or _COLLECTED_AT.fullmatch(collected_at) is None:
        raise ValueError("snapshot collected_at must be a UTC second timestamp")
    if raw.get("api_version") != API_VERSION:
        raise ValueError(f"snapshot api_version must be {API_VERSION}")
    default_branch_sha = raw.get("default_branch_sha")
    if not isinstance(default_branch_sha, str) or _REVISION.fullmatch(default_branch_sha) is None:
        raise ValueError("snapshot default_branch_sha must be a full lowercase Git SHA")
    raw_observations = raw.get("observations")
    if not isinstance(raw_observations, dict):
        raise ValueError("snapshot observations must be a JSON object")
    missing = sorted(_SURFACES - raw_observations.keys())
    if missing:
        raise ValueError(f"missing observation surfaces: {', '.join(missing)}")
    observations = {name: _observation(raw_observations[name]) for name in sorted(_SURFACES)}
    return EvidenceSnapshot(repository, collected_at, API_VERSION, default_branch_sha, observations)


def _api(repository: str, suffix: str) -> ApiObservation:
    command = [
        "gh",
        "api",
        "--method",
        "GET",
        "-H",
        f"X-GitHub-Api-Version: {API_VERSION}",
        f"repos/{repository}/{suffix}".rstrip("/"),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=API_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"GitHub API request failed for {suffix or 'repository'}: {exc}") from exc

    body = completed.stdout.strip()
    if completed.returncode != 0 and not body:
        body = next((line for line in reversed(completed.stderr.splitlines()) if line.strip().startswith("{")), "")
    try:
        data = json.loads(body) if body else None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub API returned non-JSON data for {suffix or 'repository'}") from exc
    if completed.returncode == 0:
        status = 204 if not body else 200
    else:
        match = re.search(r"HTTP (\d{3})", completed.stderr)
        payload_status = data.get("status") if isinstance(data, dict) else None
        if match:
            status = int(match.group(1))
        elif isinstance(payload_status, int) or (isinstance(payload_status, str) and payload_status.isdigit()):
            status = int(payload_status)
        else:
            status = 0
    return ApiObservation(status=status, data=data)


def collect(repository: str) -> EvidenceSnapshot:
    """Refetch every authoritative settings surface without mutating the repository."""
    repository = validate_repository(repository)
    repo = _api(repository, "")
    branch_sha = _branch_sha(_api(repository, "branches/main"))
    rulesets = _api(repository, "rulesets")
    if rulesets.status == 200 and isinstance(rulesets.data, list):
        # The collection response omits rule parameters and GitHub has no batch-detail endpoint.
        details: list[object] = []
        for summary in rulesets.data:
            if not isinstance(summary, dict) or not isinstance(summary.get("id"), int):
                raise RuntimeError("GitHub ruleset summary omitted its numeric id")
            detail = _api(repository, f"rulesets/{summary['id']}")
            if detail.status != 200:
                raise RuntimeError(f"GitHub ruleset detail returned HTTP {detail.status}")
            details.append(detail.data)
        rulesets = ApiObservation(200, details)

    security_data = (
        repo.data.get("security_and_analysis") if repo.status == 200 and isinstance(repo.data, dict) else None
    )
    observations = {
        "repository": repo,
        "rulesets": rulesets,
        "actions_permissions": _api(repository, "actions/permissions"),
        "actions_selected": _api(repository, "actions/permissions/selected-actions"),
        "workflow_permissions": _api(repository, "actions/permissions/workflow"),
        "vulnerability_alerts": _api(repository, "vulnerability-alerts"),
        "automated_security_fixes": _api(repository, "automated-security-fixes"),
        "private_vulnerability_reporting": _api(repository, "private-vulnerability-reporting"),
        "security_and_analysis": ApiObservation(repo.status, security_data),
    }
    observations["environments"] = _environments(repository)
    if _branch_sha(_api(repository, "branches/main")) != branch_sha:
        raise RuntimeError("GitHub default branch changed during settings collection")
    collected_at = datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return EvidenceSnapshot(repository, collected_at, API_VERSION, branch_sha, observations)


def _single_page(data: dict[str, Any], key: str, subject: str) -> list[object]:
    """Return one listing page, failing closed when GitHub reports more items than it served."""
    items = data.get(key, [])
    if not isinstance(items, list) or data.get("total_count") != len(items):
        raise RuntimeError(f"GitHub {subject} listing is incomplete or exceeds one page")
    return items


def _environments(repository: str) -> ApiObservation:
    """Read every deployment environment with its protection and deployment-ref policies."""
    listing = _api(repository, "environments?per_page=100")
    if listing.status != 200 or not isinstance(listing.data, dict):
        return listing
    environments: list[object] = []
    for environment in _single_page(listing.data, "environments", "environment"):
        if not isinstance(environment, dict) or not isinstance(environment.get("name"), str):
            raise RuntimeError("GitHub environment listing omitted an environment name")
        policies = _api(repository, f"environments/{environment['name']}/deployment-branch-policies?per_page=100")
        if policies.status == 200 and isinstance(policies.data, dict):
            branch_policies = _single_page(policies.data, "branch_policies", "deployment policy")
        elif policies.status == 404:
            # GitHub serves no policy list unless custom deployment policies are enabled.
            branch_policies = []
        else:
            raise RuntimeError(f"GitHub deployment policies returned HTTP {policies.status}")
        environments.append({**environment, "deployment_branch_policies": branch_policies})
    return ApiObservation(200, {"environments": environments})


def _environment_findings(name: str, expected: object, actual: dict[str, Any]) -> list[str]:
    """Return why one release environment does not match its declared protection."""
    if not isinstance(expected, dict) or not isinstance(expected.get("required_reviewers"), bool):
        raise ValueError(f"manifest environment {name} must declare required_reviewers")
    patterns = expected.get("deployment_tag_patterns")
    if not isinstance(patterns, list) or not patterns or not all(isinstance(item, str) for item in patterns):
        raise ValueError(f"manifest environment {name} must declare deployment_tag_patterns")
    problems: list[str] = []
    if actual.get("deployment_branch_policy") != _TAG_ONLY_DEPLOYMENTS:
        problems.append("deployments are not limited to custom ref policies")
    policies = actual.get("deployment_branch_policies")
    observed = {
        (item.get("name"), item.get("type"))
        for item in (policies if isinstance(policies, list) else [])
        if isinstance(item, dict)
    }
    if observed != {(pattern, "tag") for pattern in patterns}:
        problems.append("deployment ref policies are not exactly the declared tag patterns")
    rules = actual.get("protection_rules")
    reviewer_rules = [
        rule
        for rule in (rules if isinstance(rules, list) else [])
        if isinstance(rule, dict) and rule.get("type") == "required_reviewers"
    ]
    if expected["required_reviewers"]:
        if len(reviewer_rules) != 1 or not reviewer_rules[0].get("reviewers"):
            problems.append("a required reviewer is missing")
        elif reviewer_rules[0].get("prevent_self_review") is not False:
            problems.append("self-review prevention would block the sole maintainer")
    elif reviewer_rules:
        problems.append("an undeclared required reviewer adds an approval")
    return problems


def _require_environments(
    phase: Phase,
    expected: object,
    observation: ApiObservation,
    failures: list[Finding],
    pending: list[Finding],
) -> None:
    if not isinstance(expected, dict) or not expected:
        raise ValueError("manifest environments must be a non-empty object")
    data = observation.data
    listed = data.get("environments") if observation.status == 200 and isinstance(data, dict) else None
    if not isinstance(listed, list):
        failures.append(Finding("environments", f"unexpected HTTP {observation.status}"))
        return
    actual_by_name = {
        item["name"]: item for item in listed if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for name in sorted(expected):
        if name not in actual_by_name:
            _record_mismatch(phase, f"environments.{name}", "environment does not exist", failures, pending)
            continue
        for problem in _environment_findings(name, expected[name], actual_by_name[name]):
            _record_mismatch(phase, f"environments.{name}", problem, failures, pending)
    for name in sorted(actual_by_name.keys() - expected.keys()):
        _record_mismatch(
            phase, f"environments.{name}", "environment is not declared in the manifest", failures, pending
        )


def _branch_sha(observation: ApiObservation) -> str:
    data = observation.data
    commit = data.get("commit") if observation.status == 200 and isinstance(data, dict) else None
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or _REVISION.fullmatch(sha) is None:
        raise RuntimeError("GitHub default-branch response omitted a full lowercase Git SHA")
    return sha


def _matches(expected: object, actual: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        unmatched = list(actual)
        for item in expected:
            index = next((position for position, candidate in enumerate(unmatched) if _matches(item, candidate)), None)
            if index is None:
                return False
            unmatched.pop(index)
        return True
    return expected == actual


def _record_mismatch(
    phase: Phase,
    control: str,
    message: str,
    failures: list[Finding],
    pending: list[Finding],
) -> None:
    target = pending if phase == "pre-cutover" else failures
    target.append(Finding(control, message))


def _require_match(
    phase: Phase,
    control: str,
    expected: object,
    observation: ApiObservation,
    failures: list[Finding],
    pending: list[Finding],
) -> None:
    if observation.status != 200:
        if phase == "pre-cutover" and observation.status in {403, 404, 409}:
            pending.append(Finding(control, f"target state is unavailable: HTTP {observation.status}"))
        else:
            failures.append(Finding(control, f"unexpected HTTP {observation.status}"))
    elif not _matches(expected, observation.data):
        _record_mismatch(phase, control, "live state differs from manifest", failures, pending)


def _require_rulesets(
    phase: Phase,
    expected: object,
    observation: ApiObservation,
    failures: list[Finding],
    pending: list[Finding],
) -> None:
    if observation.status != 200 or not isinstance(observation.data, list):
        failures.append(Finding("rulesets", f"unexpected HTTP {observation.status}"))
        return
    if not isinstance(expected, list):
        raise ValueError("manifest rulesets must be a list")
    actual_by_name = {
        item["name"]: item for item in observation.data if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for ruleset in expected:
        if not isinstance(ruleset, dict) or not isinstance(ruleset.get("name"), str):
            raise ValueError("every manifest ruleset must have a name")
        name = ruleset["name"]
        if name not in actual_by_name or not _matches(ruleset, actual_by_name[name]):
            _record_mismatch(phase, f"rulesets.{name}", "live state differs from manifest", failures, pending)


def evaluate(
    manifest: dict[str, Any],
    observations: dict[str, ApiObservation],
    *,
    phase: Phase,
) -> VerificationReport:
    """Compare live or captured observations with the versioned contract."""
    missing = sorted(_SURFACES - observations.keys())
    if missing:
        raise ValueError(f"missing observation surfaces: {', '.join(missing)}")
    failures: list[Finding] = []
    pending: list[Finding] = []

    repository_expected = dict(manifest["repository"])
    repository_observed = observations["repository"]
    if phase == "pre-cutover":
        repository_expected.pop("private", None)
        repository_expected.pop("visibility", None)
        if repository_observed.status != 200 or not isinstance(repository_observed.data, dict):
            failures.append(Finding("repository", f"unexpected HTTP {repository_observed.status}"))
        else:
            if not _matches(repository_expected, repository_observed.data):
                pending.append(Finding("repository", "live state differs from manifest"))
            if repository_observed.data.get("private") is True:
                pending.append(Finding("repository.visibility", "source repository remains private"))
            else:
                failures.append(Finding("repository.visibility", "pre-cutover verifier expected a private source"))
    else:
        _require_match(phase, "repository", repository_expected, repository_observed, failures, pending)

    rulesets = observations["rulesets"]
    message = rulesets.data.get("message") if isinstance(rulesets.data, dict) else None
    if phase == "pre-cutover" and rulesets.status == 403 and message == _PLAN_MESSAGE:
        pending.append(Finding("rulesets", "private plan does not expose repository rulesets"))
    else:
        _require_rulesets(phase, manifest["rulesets"], rulesets, failures, pending)

    actions = manifest["actions"]
    _require_match(
        phase, "actions.permissions", actions["permissions"], observations["actions_permissions"], failures, pending
    )
    _require_match(
        phase,
        "actions.selected_actions",
        actions["selected_actions"],
        observations["actions_selected"],
        failures,
        pending,
    )
    _require_match(
        phase,
        "actions.workflow_permissions",
        actions["workflow_permissions"],
        observations["workflow_permissions"],
        failures,
        pending,
    )

    security = manifest["security"]
    alerts = observations["vulnerability_alerts"]
    if alerts.status != 204:
        _record_mismatch(phase, "security.vulnerability_alerts", "dependency alerts are not enabled", failures, pending)
    fixes = observations["automated_security_fixes"]
    if fixes.status != 200 or not _matches({"enabled": True, "paused": False}, fixes.data):
        _record_mismatch(
            phase, "security.automated_security_fixes", "Dependabot security updates are not active", failures, pending
        )

    reporting = observations["private_vulnerability_reporting"]
    if phase == "pre-cutover" and reporting.status == 404:
        pending.append(Finding("security.private_vulnerability_reporting", "unavailable until repository is public"))
    elif reporting.status != 200 or not _matches({"enabled": True}, reporting.data):
        failures.append(Finding("security.private_vulnerability_reporting", "private reporting is not enabled"))

    analysis = observations["security_and_analysis"]
    if phase == "pre-cutover" and analysis.status == 200 and analysis.data is None:
        pending.append(Finding("security.security_and_analysis", "feature state is unavailable while private"))
    else:
        _require_match(
            phase,
            "security.security_and_analysis",
            security["security_and_analysis"],
            analysis,
            failures,
            pending,
        )

    _require_environments(phase, manifest["environments"], observations["environments"], failures, pending)

    status: Literal["pass", "pending", "fail"] = "fail" if failures else "pending" if pending else "pass"
    return VerificationReport(1, phase, status, tuple(sorted(failures)), tuple(sorted(pending)))


def evaluate_snapshot(
    manifest: dict[str, Any],
    snapshot: EvidenceSnapshot,
    *,
    phase: Phase,
    expected_revision: str | None = None,
    now: datetime | None = None,
) -> VerificationReport:
    """Evaluate settings evidence, binding final proof to a fresh public revision."""
    report = evaluate(manifest, snapshot.observations, phase=phase)
    if phase == "pre-cutover":
        return report
    if not isinstance(expected_revision, str) or _REVISION.fullmatch(expected_revision) is None:
        raise ValueError("post-cutover verification requires an expected full lowercase Git SHA")
    observed_at = datetime.strptime(snapshot.collected_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    current_time = now or datetime.now(tz=UTC)
    age_seconds = (current_time - observed_at).total_seconds()
    failures = list(report.failures)
    if snapshot.default_branch_sha != expected_revision:
        failures.append(
            Finding("repository.default_branch", "default branch does not match the expected public revision")
        )
    if age_seconds < 0 or age_seconds > MAX_SNAPSHOT_AGE_SECONDS:
        failures.append(
            Finding(
                "repository.evidence_freshness",
                f"settings evidence must be collected within {MAX_SNAPSHOT_AGE_SECONDS} seconds of verification",
            )
        )
    return VerificationReport(
        1,
        phase,
        "fail" if failures else report.status,
        tuple(sorted(failures)),
        report.pending,
    )


def load_manifest(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("manifest must be a schema-version-1 JSON object")
    return raw
