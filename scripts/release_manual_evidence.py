"""Acquire and validate one digest-closed, same-candidate manual evidence ledger."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from scripts import release_bundle, release_consumer, release_evidence_json, release_frontier_evidence
from scripts._release_governance import load_policy
from scripts.documentation_commands import OUTER_SCENARIOS
from scripts.rehearsal_acquisition import (
    acquire_manual_evidence,
    bind_rehearsal_candidate,
    canonical_relative_path,
    open_real_directory,
    read_root_bytes,
    rehearsal_member_names,
    verified_bundle_artifacts,
)
from scripts.rehearsal_evidence import MAX_EVIDENCE_BYTES, validate_rehearsal

GOVERNANCE_POLICY = Path("docs/release-readiness/release-governance-policy.json")
EXTERNAL_CONTROLS = (
    "github-release-environment",
    "pypi-trusted-publisher",
)
MANUAL_GATES = (
    "package-name-reservation",
    "repository-controls",
    "testpypi-rehearsal",
    "public-contributor-journeys",
    "public-user-journeys",
    "documentation-rehearsals",
    "frontier-final-review",
    "cutover-approval",
)
REQUIRED_EVIDENCE_IDS = (*EXTERNAL_CONTROLS, *MANUAL_GATES)
_MANUAL_EVIDENCE_SCHEMA = Path("docs/release-readiness/release-manual-evidence.schema.json")
_MANUAL_RECORD_SCHEMA = Path("docs/release-readiness/release-evidence-record.schema.json")
RECORD_PAYLOAD_FIELDS = {
    "github-release-environment": frozenset({"repository_id", "environment", "required_reviewers", "workflow_path"}),
    "pypi-trusted-publisher": frozenset({"project", "repository", "workflow_path", "environment"}),
    "package-name-reservation": frozenset({"project", "index_endpoint", "availability"}),
    "repository-controls": frozenset({"repository_id", "default_branch", "required_checks", "fork_ci"}),
    "testpypi-rehearsal": frozenset(
        {"index_endpoint", "consumer_evidence_path", "consumer_evidence_sha256", "artifacts"}
    ),
    "public-contributor-journeys": frozenset(
        {"workflow_run_id", "journeys", "artifacts", "rehearsal_evidence_path", "rehearsal_evidence_sha256"}
    ),
    "public-user-journeys": frozenset(
        {"workflow_run_id", "journeys", "artifacts", "rehearsal_evidence_path", "rehearsal_evidence_sha256"}
    ),
    "documentation-rehearsals": frozenset(
        {"workflow_run_id", "rehearsal_evidence_path", "rehearsal_evidence_sha256", "verified_blocks", "scenarios"}
    ),
    "frontier-final-review": frozenset(
        {
            "reviewed_revision",
            "reviewer_role",
            "model",
            "review_mode",
            "scope",
            "cli_version",
            "cli_version_path",
            "cli_version_sha256",
            "cli_argv",
            "prompt_path",
            "prompt_sha256",
            "response_path",
            "response_sha256",
            "findings",
            "unresolved_findings",
        }
    ),
    "cutover-approval": frozenset({"cutover_record_sha256", "initial_commit", "initial_tree", "workflow_runs"}),
}
SUPPORT_PATH_FIELDS = frozenset(
    {
        "consumer_evidence_path",
        "rehearsal_evidence_path",
        "prompt_path",
        "response_path",
        "cli_version_path",
    }
)
_CLOCK_SKEW = timedelta(minutes=5)
MUTABLE_OBSERVATION_MAX_AGE = timedelta(hours=24)
_MUTABLE_OBSERVATION_GATES = frozenset(
    {"github-release-environment", "pypi-trusted-publisher", "package-name-reservation", "repository-controls"}
)
_REQUIRED_CHECKS = frozenset({"Required checks", "Changelog fragment"})


@dataclass(frozen=True)
class Criterion:
    """One release criterion with its authoritative evidence source."""

    id: str
    status: str
    evidence: str
    record_sha256: str | None = None


def _load_json(path: Path) -> dict[str, Any]:
    return release_evidence_json.load_object(
        _read_regular_bytes(path, "release evidence JSON"), "release evidence JSON"
    )


def _read_regular_bytes(path: Path, subject: str) -> bytes:
    """Acquire one bounded regular file without following its final symlink."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"{subject} must be a regular file")
            raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    except OSError:
        raise ValueError(f"cannot acquire {subject}") from None
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError(f"{subject} exceeds the {MAX_EVIDENCE_BYTES}-byte limit")
    return raw


def _manual_record_bytes(path: Path, expected_sha256: object) -> dict[str, Any]:
    """Load one digest-bound evidence record without accepting an assertion alone."""
    record_bytes = _read_regular_bytes(path, "manual release evidence record")
    if not isinstance(expected_sha256, str) or hashlib.sha256(record_bytes).hexdigest() != expected_sha256:
        raise ValueError("manual release evidence record digest does not match")
    return release_evidence_json.load_object(record_bytes, "manual release evidence record")


def _record_from_bytes(raw: bytes, expected_sha256: object, subject: str) -> dict[str, Any]:
    """Load one digest-bound record from already acquired bytes."""
    if not isinstance(expected_sha256, str) or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("manual release evidence record digest does not match")
    return release_evidence_json.load_object(raw, subject)


def canonical_json_sha256(value: object) -> str:
    """Return the digest of one retained JSON value in canonical form."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _strings(payload: Mapping[str, object], *names: str) -> bool:
    return all(isinstance(payload[name], str) and bool(payload[name]) for name in names)


def _artifact_list(value: object) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    artifacts: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "sha256"}:
            return None
        name = item.get("name")
        digest = item.get("sha256")
        if not isinstance(name, str) or not name or not _digest(digest):
            return None
        artifacts.append({"name": name, "sha256": str(digest)})
    return sorted(artifacts, key=lambda item: item["name"])


def _expected_uri(gate: str, payload: Mapping[str, object], candidate: Mapping[str, object]) -> str | None:
    repository = candidate["repository"]
    project = str(repository).split("/", 1)[1]
    version = str(candidate["planned_tag"]).removeprefix("v")
    if gate == "github-release-environment":
        return f"https://api.github.com/repos/{repository}/environments/release-approval"
    if gate == "pypi-trusted-publisher":
        return f"https://github.com/{repository}/actions/workflows/release.yml"
    if gate == "package-name-reservation":
        return f"https://pypi.org/pypi/{project}/json"
    if gate == "repository-controls":
        return f"https://api.github.com/repos/{repository}"
    if gate == "testpypi-rehearsal":
        return f"https://test.pypi.org/pypi/{project}/{version}/json"
    if gate in {"public-contributor-journeys", "public-user-journeys", "documentation-rehearsals"}:
        return f"https://github.com/{repository}/actions/runs/{payload.get('workflow_run_id')}"
    if gate == "frontier-final-review":
        return None
    workflow_runs = payload.get("workflow_runs")
    if isinstance(workflow_runs, list) and len(workflow_runs) == 1:
        return f"https://github.com/{repository}/actions/runs/{workflow_runs[0]}"
    return None


def _valid_source_uri(
    gate: str, source_uri: object, payload: Mapping[str, object], candidate: Mapping[str, object]
) -> bool:
    if not isinstance(source_uri, str):
        return False
    try:
        parsed = urlsplit(source_uri)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    if gate == "frontier-final-review":
        return (
            parsed.netloc == "github.com"
            and re.fullmatch(rf"/{re.escape(str(candidate['repository']))}/pull/[1-9][0-9]*", parsed.path) is not None
        )
    return source_uri == _expected_uri(gate, payload, candidate)


def _support_bytes(
    payload: Mapping[str, object],
    *,
    path_field: str,
    digest_field: str,
    record_bytes: Mapping[str, bytes],
    used_paths: set[str],
) -> bytes | None:
    try:
        path = canonical_relative_path(payload[path_field], path_field)
    except (KeyError, ValueError):
        return None
    raw = record_bytes.get(path)
    if (
        raw is None
        or not _digest(payload.get(digest_field))
        or hashlib.sha256(raw).hexdigest() != payload[digest_field]
    ):
        return None
    used_paths.add(path)
    return raw


def _consumer_receipt_valid(
    controller_root: Path,
    raw: bytes,
    payload: Mapping[str, object],
    candidate: Mapping[str, object],
) -> bool:
    receipt = release_evidence_json.load_object(raw, "TestPyPI consumer evidence")
    if not release_evidence_json.schema_valid(
        _load_json(controller_root / "docs/release-readiness/release-consumer-evidence.schema.json"), receipt
    ):
        return False
    expected_artifacts = _artifact_list(candidate["artifacts"])
    receipt_artifacts = receipt.get("artifacts")
    if not isinstance(receipt_artifacts, list):
        return False
    observed = _artifact_list(
        [
            {"name": item.get("name"), "sha256": item.get("sha256")}
            for item in receipt_artifacts
            if isinstance(item, dict) and item.get("observed") is True and item.get("downloaded") is True
        ]
    )
    if (
        receipt.get("status") != "success"
        or receipt.get("repository") != candidate["repository"]
        or receipt.get("source_commit") != candidate["source_sha"]
        or receipt.get("planned_tag") != candidate["planned_tag"]
        or receipt.get("index_endpoint") != payload["index_endpoint"]
        or observed != expected_artifacts
        or _artifact_list(payload["artifacts"]) != expected_artifacts
        or receipt.get("findings") != []
    ):
        return False
    expected_scenarios = {
        (artifact["name"], identifier): list(argv)
        for artifact in expected_artifacts or []
        for identifier, argv in release_consumer._OFFLINE_SCENARIOS
    }
    scenarios = receipt.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != len(expected_scenarios):
        return False
    actual: dict[tuple[str, str], object] = {}
    for item in scenarios:
        if not isinstance(item, dict):
            return False
        artifact_name = item.get("artifact_name")
        scenario_id = item.get("id")
        if not isinstance(artifact_name, str) or not isinstance(scenario_id, str):
            return False
        key = (artifact_name, scenario_id)
        if key in actual:
            return False
        if (
            item.get("status") != "pass"
            or item.get("exit_status") != 0
            or item.get("argv") != expected_scenarios.get(key)
        ):
            return False
        actual[key] = item
    return set(actual) == set(expected_scenarios)


def journey_ids(actor: str) -> frozenset[str]:
    return frozenset(scenario.identifier for scenario in OUTER_SCENARIOS if scenario.actor == actor)


def _captured_at(value: object) -> datetime | None:
    """Accept only an offset-aware ISO-8601 observation timestamp."""
    if not isinstance(value, str):
        return None
    try:
        captured = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return captured.astimezone(UTC) if captured.tzinfo is not None else None
    except ValueError:
        return None


def _valid_captured_at(value: object, *, maximum_age: timedelta | None = None) -> bool:
    captured = _captured_at(value)
    now = datetime.now(UTC)
    return (
        captured is not None
        and captured <= now + _CLOCK_SKEW
        and (maximum_age is None or captured >= now - maximum_age)
    )


def _valid_payload(
    controller_root: Path,
    gate: str,
    payload: dict[str, Any],
    candidate: dict[str, Any],
    record_bytes: Mapping[str, bytes],
    used_paths: set[str],
) -> bool:
    expected_artifacts = _artifact_list(candidate["artifacts"])
    repository = candidate["repository"]
    project = str(repository).split("/", 1)[1]
    if gate == "github-release-environment":
        reviewers = payload["required_reviewers"]
        approver_login = load_policy(controller_root / GOVERNANCE_POLICY).roles["approver_login"]
        return (
            type(payload["repository_id"]) is int
            and payload["repository_id"] > 0
            and payload["environment"] == "release-approval"
            and payload["workflow_path"] == ".github/workflows/release-approval.yml"
            and reviewers == [approver_login]
        )
    if gate == "pypi-trusted-publisher":
        return bool(
            payload["project"] == project
            and payload["repository"] == repository
            and payload["workflow_path"] == ".github/workflows/release.yml"
            and payload["environment"] == "pypi"
        )
    if gate == "package-name-reservation":
        return bool(
            payload["project"] == project
            and payload["index_endpoint"] == f"https://pypi.org/pypi/{project}/json"
            and payload["availability"] == "reserved"
        )
    if gate == "repository-controls":
        checks = payload["required_checks"]
        return (
            type(payload["repository_id"]) is int
            and payload["repository_id"] > 0
            and payload["default_branch"] == "main"
            and isinstance(checks, list)
            and all(isinstance(item, str) for item in checks)
            and set(checks) == _REQUIRED_CHECKS
            and len(checks) == len(_REQUIRED_CHECKS)
            and payload["fork_ci"] is True
        )
    if gate == "testpypi-rehearsal":
        raw = _support_bytes(
            payload,
            path_field="consumer_evidence_path",
            digest_field="consumer_evidence_sha256",
            record_bytes=record_bytes,
            used_paths=used_paths,
        )
        version = str(candidate["planned_tag"]).removeprefix("v")
        return (
            raw is not None
            and payload["index_endpoint"] == f"https://test.pypi.org/pypi/{project}/{version}/json"
            and _artifact_list(payload["artifacts"]) == expected_artifacts
            and _consumer_receipt_valid(controller_root, raw, payload, candidate)
        )
    if gate in {"public-contributor-journeys", "public-user-journeys", "documentation-rehearsals"}:
        raw = _support_bytes(
            payload,
            path_field="rehearsal_evidence_path",
            digest_field="rehearsal_evidence_sha256",
            record_bytes=record_bytes,
            used_paths=used_paths,
        )
        return (
            raw is not None
            and type(payload["workflow_run_id"]) is int
            and payload["workflow_run_id"] > 0
            and ("artifacts" not in payload or _artifact_list(payload["artifacts"]) == expected_artifacts)
        )
    if gate == "frontier-final-review":
        prompt = _support_bytes(
            payload,
            path_field="prompt_path",
            digest_field="prompt_sha256",
            record_bytes=record_bytes,
            used_paths=used_paths,
        )
        response = _support_bytes(
            payload,
            path_field="response_path",
            digest_field="response_sha256",
            record_bytes=record_bytes,
            used_paths=used_paths,
        )
        version_bytes = _support_bytes(
            payload,
            path_field="cli_version_path",
            digest_field="cli_version_sha256",
            record_bytes=record_bytes,
            used_paths=used_paths,
        )
        return release_frontier_evidence.valid_proof(prompt, response, version_bytes, payload, candidate)
    if gate == "cutover-approval":
        runs = payload["workflow_runs"]
        return (
            _digest(payload["cutover_record_sha256"])
            and isinstance(payload["initial_commit"], str)
            and re.fullmatch(r"[0-9a-f]{40}", payload["initial_commit"]) is not None
            and payload["initial_tree"] == candidate["exported_tree"]
            and isinstance(runs, list)
            and len(runs) == 1
            and type(runs[0]) is int
            and runs[0] > 0
        )
    return False


def _validate_manual_record(
    controller_root: Path,
    gate: str,
    record_data: dict[str, Any],
    candidate: dict[str, Any],
    record_bytes: Mapping[str, bytes],
    used_paths: set[str],
) -> None:
    """Validate one criterion-specific, same-candidate evidence record."""
    if record_data.get("schema_version") != 1 or record_data.get("kind") != gate or record_data.get("status") != "pass":
        raise ValueError(f"manual release evidence {gate} record has an invalid identity or status")
    if record_data.get("candidate") != candidate:
        raise ValueError(f"manual release evidence {gate} record does not bind the retained public candidate")
    proof = record_data.get("proof")
    if not isinstance(proof, dict):
        raise ValueError(f"manual release evidence {gate} record has no typed proof")
    source_uri = proof.get("source_uri")
    maximum_age = MUTABLE_OBSERVATION_MAX_AGE if gate in _MUTABLE_OBSERVATION_GATES else None
    if not _valid_captured_at(proof.get("captured_at"), maximum_age=maximum_age):
        raise ValueError(f"manual release evidence {gate} record has an invalid capture time")
    payload = proof.get("payload")
    if not isinstance(payload, dict) or set(payload) != RECORD_PAYLOAD_FIELDS[gate]:
        raise ValueError(f"manual release evidence {gate} record has an incomplete typed proof payload")
    if not _valid_source_uri(gate, source_uri, payload, candidate):
        raise ValueError(f"manual release evidence {gate} record has an untrusted proof source")
    if not _valid_payload(controller_root, gate, payload, candidate, record_bytes, used_paths):
        raise ValueError(f"manual release evidence {gate} record has invalid typed proof values")
    if proof.get("payload_sha256") != canonical_json_sha256(payload):
        raise ValueError(f"manual release evidence {gate} record proof payload digest does not match")


def validate_bytes(
    controller_root: Path,
    *,
    documentation_contract_sha256: str,
    evidence_bytes: bytes,
    candidate_report_bytes: bytes,
    record_bytes: Mapping[str, bytes],
    artifact_bytes: Mapping[str, bytes] | None = None,
    private_candidate_report_bytes: bytes | None = None,
) -> tuple[tuple[Criterion, ...], str]:
    """Validate a ledger and its records entirely from caller-acquired bytes."""
    evidence = release_evidence_json.load_object(evidence_bytes, "manual release evidence ledger")
    schema = _load_json(controller_root / _MANUAL_EVIDENCE_SCHEMA)
    try:
        error = next(Draft202012Validator(schema).iter_errors(evidence), None)
    except RecursionError:
        raise ValueError("manual release evidence schema validation exceeds nesting limit") from None
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_schema_path) or "<root>"
        raise ValueError(f"manual release evidence schema violation at {location}: {error.validator}")
    record_schema = _load_json(controller_root / _MANUAL_RECORD_SCHEMA)
    report = release_evidence_json.load_object(candidate_report_bytes, "candidate report")
    policy_candidate = load_policy(controller_root / GOVERNANCE_POLICY).candidate
    manifest = report.get("export_manifest")
    validation = report.get("artifact_validation")
    if (
        report.get("status") != "pass"
        or report.get("expected_repository") != policy_candidate.repository
        or report.get("package") != policy_candidate.package
        or report.get("planned_tag") != policy_candidate.planned_tag
        or not isinstance(manifest, dict)
        or not isinstance(validation, dict)
    ):
        raise ValueError("manual release evidence requires a passing candidate report")
    artifacts = validation.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("candidate report must list validated artifacts")
    expected = {
        "repository": manifest.get("expected_repository"),
        "source_sha": manifest.get("source_commit"),
        "exported_tree": manifest.get("exported_tree"),
        "planned_tag": manifest.get("planned_tag"),
        "artifacts": sorted(
            [
                {"name": item.get("name"), "sha256": item.get("sha256")}
                for item in artifacts
                if isinstance(item, dict) and item.get("status") == "pass"
            ],
            key=lambda item: str(item["name"]),
        ),
    }
    candidate = evidence["candidate"]
    if not isinstance(candidate, dict) or any(
        candidate.get(field) != value for field, value in expected.items() if field != "artifacts"
    ):
        raise ValueError("manual release evidence does not bind the retained public candidate")
    recorded_artifacts = candidate.get("artifacts")
    if (
        not isinstance(recorded_artifacts, list)
        or sorted(recorded_artifacts, key=lambda item: str(item.get("name")) if isinstance(item, dict) else "")
        != expected["artifacts"]
    ):
        raise ValueError("manual release evidence artifacts do not match the retained public candidate")
    criteria = evidence["criteria"]
    if not isinstance(criteria, list):
        raise ValueError("manual release evidence criteria must be a list")
    by_id = {item.get("id"): item for item in criteria if isinstance(item, dict)}
    if set(by_id) != set(REQUIRED_EVIDENCE_IDS) or len(by_id) != len(criteria):
        raise ValueError("manual release evidence must cover every required criterion exactly once")
    used_paths: set[str] = set()
    pending_gates: set[str] = set()
    for gate in REQUIRED_EVIDENCE_IDS:
        record = by_id[gate]["record"]
        if not isinstance(record, dict):
            raise ValueError(f"manual release evidence {gate} has an invalid record")
        relative_path = canonical_relative_path(record.get("path"), f"manual release evidence {gate} record path")
        if relative_path in used_paths or relative_path not in record_bytes:
            raise ValueError(f"manual release evidence {gate} record bytes are missing or ambiguous")
        used_paths.add(relative_path)
        record_data = _record_from_bytes(
            record_bytes[relative_path], record.get("sha256"), f"manual release evidence {gate} record"
        )
        try:
            record_error = next(Draft202012Validator(record_schema).iter_errors(record_data), None)
        except RecursionError:
            raise ValueError("manual release evidence record schema validation exceeds nesting limit") from None
        if record_error is not None:
            location = ".".join(str(part) for part in record_error.absolute_schema_path) or "<root>"
            raise ValueError(
                f"manual release evidence {gate} record schema violation at {location}: {record_error.validator}"
            )
        if record.get("kind") != gate:
            raise ValueError(f"manual release evidence {gate} record kind does not match its criterion")
        _validate_manual_record(controller_root, gate, record_data, candidate, record_bytes, used_paths)
        if gate in {"public-contributor-journeys", "public-user-journeys", "documentation-rehearsals"}:
            payload = record_data["proof"]["payload"]
            receipt_path = canonical_relative_path(payload["rehearsal_evidence_path"], "rehearsal receipt path")
            raw = record_bytes[receipt_path]
            if artifact_bytes is None:
                raise ValueError("rehearsal diagnostic requires exact artifact bytes")
            names = rehearsal_member_names(raw)
            if not set(names) <= set(record_bytes):
                raise ValueError("rehearsal retained member catalog is incomplete")
            diagnostic = validate_rehearsal(
                raw,
                schema_bytes=read_root_bytes(controller_root, "docs/release-readiness/rehearsal-evidence.schema.json"),
                context=None,
                retained_members={name: record_bytes[name] for name in names},
                artifact_bytes=artifact_bytes,
            )
            bind_rehearsal_candidate(
                diagnostic,
                candidate_report_bytes=candidate_report_bytes,
                private_candidate_report_bytes=private_candidate_report_bytes,
            )
            used_paths.update(names)
            public = diagnostic.public
            if (
                diagnostic.phase != "public-release"
                or public is None
                or diagnostic.subject.documentation_contract_sha256 != documentation_contract_sha256
                or public.run_id != payload["workflow_run_id"]
            ):
                raise ValueError("rehearsal diagnostic does not bind the retained public candidate")
            expected_ids = {item.identifier for item in diagnostic.scenarios}
            if gate == "documentation-rehearsals":
                if payload["verified_blocks"] != list(diagnostic.verified_blocks) or (
                    not isinstance(payload["scenarios"], list)
                    or not all(isinstance(item, str) for item in payload["scenarios"])
                    or len(payload["scenarios"]) != len(expected_ids)
                    or set(payload["scenarios"]) != expected_ids
                ):
                    raise ValueError("rehearsal documentation payload does not match typed diagnostics")
            else:
                actor = "external_contributor" if gate == "public-contributor-journeys" else "external_user"
                journeys = payload["journeys"]
                if (
                    not isinstance(journeys, list)
                    or len(journeys) != len(journey_ids(actor))
                    or (not all(isinstance(item, str) for item in journeys) or set(journeys) != journey_ids(actor))
                ):
                    raise ValueError("rehearsal journey payload does not match its registered plan")
            pending_gates.add(gate)
        if record.get("uri") != record_data["proof"]["source_uri"]:
            raise ValueError(f"manual release evidence {gate} ledger URI does not match its retained proof")
    if set(record_bytes) != used_paths:
        raise ValueError("manual release evidence contains unreferenced record bytes")
    ledger_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    return (
        tuple(
            Criterion(
                gate,
                "pending" if gate in pending_gates else "pass",
                str(by_id[gate]["record"]["uri"]),
                str(by_id[gate]["record"]["sha256"]),
            )
            for gate in REQUIRED_EVIDENCE_IDS
        ),
        ledger_sha256,
    )


def validate_paths(
    repo: Path,
    evidence_path: Path,
    candidate_report_path: Path,
    *,
    candidate_bundle: Path | None = None,
    private_candidate_report: Path | None = None,
) -> tuple[tuple[Criterion, ...], str]:
    """Acquire regular evidence files once, then delegate to the byte validator."""
    with ExitStack() as resources:
        evidence_root_fd = open_real_directory(evidence_path.parent)
        resources.callback(os.close, evidence_root_fd)
        candidate_root_fd = open_real_directory(candidate_report_path.parent)
        resources.callback(os.close, candidate_root_fd)
        evidence_bytes = release_bundle._safe_bytes_at(
            evidence_root_fd, evidence_path.name, maximum_bytes=MAX_EVIDENCE_BYTES
        )
        candidate_report_bytes = release_bundle._safe_bytes_at(
            candidate_root_fd, candidate_report_path.name, maximum_bytes=MAX_EVIDENCE_BYTES
        )
        artifacts = None
        if candidate_bundle is not None:
            bundle_fd = open_real_directory(candidate_bundle)
            try:
                artifacts = verified_bundle_artifacts(bundle_fd, candidate_report_bytes=candidate_report_bytes)
            finally:
                os.close(bundle_fd)
        acquired = acquire_manual_evidence(
            evidence_root_fd,
            evidence_bytes,
            expected_criterion_ids=REQUIRED_EVIDENCE_IDS,
            support_path_fields=SUPPORT_PATH_FIELDS,
        )
        return validate_bytes(
            repo,
            documentation_contract_sha256=hashlib.sha256(
                read_root_bytes(repo, "docs/documentation-contract.json")
            ).hexdigest(),
            evidence_bytes=evidence_bytes,
            candidate_report_bytes=candidate_report_bytes,
            record_bytes=acquired,
            artifact_bytes=artifacts,
            private_candidate_report_bytes=(
                read_root_bytes(private_candidate_report.parent, private_candidate_report.name)
                if private_candidate_report is not None
                else None
            ),
        )
