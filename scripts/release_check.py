"""Build one current release candidate and render fail-closed readiness evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

if __package__:
    from scripts import check_public_candidate
    from scripts._release_governance import load_policy
    from scripts._release_governance import validate as validate_governance
    from scripts.json_policy import reject_duplicate_json_keys
else:
    import check_public_candidate
    from _release_governance import load_policy
    from _release_governance import validate as validate_governance
    from json_policy import reject_duplicate_json_keys

_EXTERNAL_CONTROLS = (
    "github-release-environment",
    "pypi-trusted-publisher",
)
_MANUAL_GATES = (
    "package-name-reservation",
    "repository-controls",
    "testpypi-rehearsal",
    "public-contributor-journeys",
    "public-user-journeys",
    "documentation-rehearsals",
    "frontier-final-review",
    "cutover-approval",
)
_REQUIRED_EVIDENCE_IDS = (*_EXTERNAL_CONTROLS, *_MANUAL_GATES)
_MANUAL_EVIDENCE_SCHEMA = Path("docs/release-readiness/release-manual-evidence.schema.json")
_MANUAL_RECORD_SCHEMA = Path("docs/release-readiness/release-evidence-record.schema.json")
_RECORD_SOURCE_PREFIXES = {
    "github-release-environment": ("https://api.github.com/repos/",),
    "pypi-trusted-publisher": ("https://github.com/",),
    "package-name-reservation": ("https://pypi.org/",),
    "repository-controls": ("https://api.github.com/repos/",),
    "testpypi-rehearsal": ("https://test.pypi.org/",),
    "public-contributor-journeys": ("https://github.com/",),
    "public-user-journeys": ("https://github.com/",),
    "documentation-rehearsals": ("https://github.com/",),
    "frontier-final-review": ("https://github.com/",),
    "cutover-approval": ("https://github.com/",),
}
_RECORD_PAYLOAD_FIELDS = {
    "github-release-environment": frozenset({"repository_id", "environment", "required_reviewers", "workflow_path"}),
    "pypi-trusted-publisher": frozenset({"project", "repository", "workflow_path", "environment"}),
    "package-name-reservation": frozenset({"project", "index_endpoint", "availability"}),
    "repository-controls": frozenset({"repository_id", "default_branch", "required_checks", "fork_ci"}),
    "testpypi-rehearsal": frozenset({"index_endpoint", "consumer_evidence_sha256", "artifacts"}),
    "public-contributor-journeys": frozenset({"workflow_run_id", "journeys", "artifacts"}),
    "public-user-journeys": frozenset({"workflow_run_id", "journeys", "artifacts"}),
    "documentation-rehearsals": frozenset({"rehearsal_evidence_sha256", "verified_blocks", "scenarios"}),
    "frontier-final-review": frozenset(
        {"reviewed_revision", "reviewer", "model", "review_mode", "scope", "unresolved_findings"}
    ),
    "cutover-approval": frozenset({"cutover_record_sha256", "initial_commit", "initial_tree", "workflow_runs"}),
}


@dataclass(frozen=True)
class Criterion:
    """One release criterion with its authoritative evidence source."""

    id: str
    status: str
    evidence: str
    record_sha256: str | None = None


@dataclass(frozen=True)
class Report:
    """Versioned readiness result for one immutable source revision."""

    schema_version: int
    status: str
    revision: str
    criteria: tuple[Criterion, ...]
    evidence_ledger_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "revision": self.revision,
            "criteria": [
                {key: value for key, value in asdict(item).items() if value is not None} for item in self.criteria
            ],
            **(
                {"evidence_ledger_sha256": self.evidence_ledger_sha256}
                if self.evidence_ledger_sha256 is not None
                else {}
            ),
        }


def _head_revision(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=10
    )
    if result.returncode != 0:
        raise ValueError("repository HEAD is unavailable")
    revision = result.stdout.strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("repository HEAD is not a full lowercase commit SHA")
    return revision


def _require_clean_worktree(repo: Path) -> None:
    """Reject uncommitted policy or source changes from release evidence."""
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=False, timeout=10
    )
    if result.returncode != 0:
        raise ValueError("repository worktree status is unavailable")
    if result.stdout:
        raise ValueError("release check requires a clean worktree")


def _governance(repo: Path, candidate_report: Path) -> tuple[str, tuple[str, ...]]:
    report = validate_governance(_policy_path(repo), candidate_report)
    return report.status, report.pending_controls


def _policy_path(repo: Path) -> Path:
    return repo / "docs/release-readiness/release-governance-policy.json"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_json_keys)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a JSON object")
    return value


def _manual_record_bytes(path: Path, expected_sha256: object) -> dict[str, Any]:
    """Load one digest-bound evidence record without accepting an assertion alone."""
    record_bytes = path.read_bytes()
    if not isinstance(expected_sha256, str) or hashlib.sha256(record_bytes).hexdigest() != expected_sha256:
        raise ValueError("manual release evidence record digest does not match")
    value = json.loads(record_bytes, object_pairs_hook=reject_duplicate_json_keys)
    if not isinstance(value, dict):
        raise ValueError("manual release evidence record must be a JSON object")
    return value


def _canonical_json_sha256(value: object) -> str:
    """Return the digest of one retained JSON value in canonical form."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _valid_payload(gate: str, payload: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Require each external criterion to retain meaningful typed observations."""

    def strings(*names: str) -> bool:
        return all(isinstance(payload[name], str) and payload[name] for name in names)

    def digest(value: object) -> bool:
        return (
            isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
        )

    def artifacts(value: object) -> bool:
        return isinstance(value, list) and len(value) == 2 and all(isinstance(item, dict) for item in value)

    if gate == "github-release-environment":
        return (
            type(payload["repository_id"]) is int
            and payload["repository_id"] > 0
            and strings("environment", "workflow_path")
            and isinstance(payload["required_reviewers"], list)
        )
    if gate == "pypi-trusted-publisher":
        return strings("project", "repository", "workflow_path", "environment")
    if gate == "package-name-reservation":
        return strings("project", "index_endpoint", "availability")
    if gate == "repository-controls":
        return (
            type(payload["repository_id"]) is int
            and payload["repository_id"] > 0
            and strings("default_branch")
            and isinstance(payload["required_checks"], list)
            and type(payload["fork_ci"]) is bool
        )
    if gate == "frontier-final-review":
        return (
            strings("reviewed_revision", "reviewer", "model", "review_mode", "scope")
            and payload["reviewed_revision"] == candidate["source_sha"]
            and payload["reviewer"] != "release-maintainer"
            and "frontier" in payload["model"].lower()
            and payload["review_mode"] == "fresh-eyes"
            and payload["unresolved_findings"] == []
        )
    if gate == "testpypi-rehearsal":
        return (
            strings("index_endpoint")
            and digest(payload["consumer_evidence_sha256"])
            and artifacts(payload["artifacts"])
        )
    if gate in {"public-contributor-journeys", "public-user-journeys"}:
        return (
            type(payload["workflow_run_id"]) is int
            and payload["workflow_run_id"] > 0
            and isinstance(payload["journeys"], list)
            and bool(payload["journeys"])
            and artifacts(payload["artifacts"])
        )
    if gate == "documentation-rehearsals":
        return (
            digest(payload["rehearsal_evidence_sha256"])
            and isinstance(payload["verified_blocks"], list)
            and bool(payload["verified_blocks"])
            and isinstance(payload["scenarios"], list)
            and bool(payload["scenarios"])
        )
    return (
        digest(payload["cutover_record_sha256"])
        and strings("initial_commit", "initial_tree")
        and isinstance(payload["workflow_runs"], list)
        and bool(payload["workflow_runs"])
    )


def _valid_captured_at(value: object) -> bool:
    """Accept only an offset-aware ISO-8601 observation timestamp."""
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _validate_manual_record(gate: str, record_data: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Validate one criterion-specific, same-candidate evidence record."""
    if record_data.get("schema_version") != 1 or record_data.get("kind") != gate or record_data.get("status") != "pass":
        raise ValueError(f"manual release evidence {gate} record has an invalid identity or status")
    if record_data.get("candidate") != candidate:
        raise ValueError(f"manual release evidence {gate} record does not bind the retained public candidate")
    proof = record_data.get("proof")
    if not isinstance(proof, dict):
        raise ValueError(f"manual release evidence {gate} record has no typed proof")
    source_uri = proof.get("source_uri")
    if not isinstance(source_uri, str) or not source_uri.startswith(_RECORD_SOURCE_PREFIXES[gate]):
        raise ValueError(f"manual release evidence {gate} record has an untrusted proof source")
    if not _valid_captured_at(proof.get("captured_at")):
        raise ValueError(f"manual release evidence {gate} record has an invalid capture time")
    payload = proof.get("payload")
    if not isinstance(payload, dict) or set(payload) != _RECORD_PAYLOAD_FIELDS[gate]:
        raise ValueError(f"manual release evidence {gate} record has an incomplete typed proof payload")
    if not _valid_payload(gate, payload, candidate):
        raise ValueError(f"manual release evidence {gate} record has invalid typed proof values")
    if proof.get("payload_sha256") != _canonical_json_sha256(payload):
        raise ValueError(f"manual release evidence {gate} record proof payload digest does not match")


def _manual_criteria(repo: Path, evidence_path: Path, candidate_report_path: Path) -> tuple[tuple[Criterion, ...], str]:
    """Return passing external criteria for a schema-valid ledger bound to this candidate."""
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise ValueError("manual release evidence ledger must be a regular file")
    evidence = _load_json(evidence_path)
    schema = _load_json(repo / _MANUAL_EVIDENCE_SCHEMA)
    errors = sorted(Draft202012Validator(schema).iter_errors(evidence), key=lambda error: list(error.absolute_path))
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"manual release evidence schema violation at {location}: {errors[0].message}")
    record_schema = _load_json(repo / _MANUAL_RECORD_SCHEMA)
    report = _load_json(candidate_report_path)
    manifest = report.get("export_manifest")
    validation = report.get("artifact_validation")
    if report.get("status") != "pass" or not isinstance(manifest, dict) or not isinstance(validation, dict):
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
    if set(by_id) != set(_REQUIRED_EVIDENCE_IDS) or len(by_id) != len(criteria):
        raise ValueError("manual release evidence must cover every required criterion exactly once")
    evidence_root = evidence_path.parent.resolve()
    for gate in _REQUIRED_EVIDENCE_IDS:
        record = by_id[gate]["record"]
        if not isinstance(record, dict):
            raise ValueError(f"manual release evidence {gate} has an invalid record")
        relative_path = record.get("path")
        if not isinstance(relative_path, str):
            raise ValueError(f"manual release evidence {gate} record path is invalid")
        record_path = (evidence_root / relative_path).resolve()
        if evidence_root not in record_path.parents or not record_path.is_file() or record_path.is_symlink():
            raise ValueError(f"manual release evidence {gate} record must be a regular file beside the ledger")
        record_data = _manual_record_bytes(record_path, record.get("sha256"))
        record_errors = sorted(
            Draft202012Validator(record_schema).iter_errors(record_data), key=lambda error: list(error.absolute_path)
        )
        if record_errors:
            location = ".".join(str(part) for part in record_errors[0].absolute_path) or "<root>"
            raise ValueError(f"manual release evidence {gate} record schema violation at {location}")
        if record.get("kind") != gate:
            raise ValueError(f"manual release evidence {gate} record kind does not match its criterion")
        _validate_manual_record(gate, record_data, candidate)
        if record.get("uri") != record_data["proof"]["source_uri"]:
            raise ValueError(f"manual release evidence {gate} ledger URI does not match its retained proof")
    ledger_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    return (
        tuple(
            Criterion(gate, "pass", str(by_id[gate]["record"]["uri"]), str(by_id[gate]["record"]["sha256"]))
            for gate in _REQUIRED_EVIDENCE_IDS
        ),
        ledger_sha256,
    )


def _write(path: Path, report: Report) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path())
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manual-evidence", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo = args.repo.resolve()
        _require_clean_worktree(repo)
        head = _head_revision(repo)
        if args.revision != head:
            raise ValueError("requested revision is not the current checkout HEAD")
        candidate = load_policy(_policy_path(repo)).candidate
        check_public_candidate.check_candidate(
            repo,
            head,
            expected_repository=candidate.repository,
            planned_tag=candidate.planned_tag,
            output_dir=args.output_dir.resolve(),
        )
        _governance_status, pending = _governance(repo, args.output_dir / "report.json")
        evidence_ledger_sha256: str | None = None
        if args.manual_evidence is not None:
            external_criteria, evidence_ledger_sha256 = _manual_criteria(
                repo, args.manual_evidence, args.output_dir / "report.json"
            )
        else:
            external_criteria = (
                *(
                    Criterion(control, "pending", "operator-recorded same-candidate external evidence")
                    for control in pending
                ),
                *(
                    Criterion(gate, "pending", "operator-recorded same-candidate release evidence")
                    for gate in _MANUAL_GATES
                ),
            )
        criteria = (
            Criterion("source-revision", "pass", "current checkout HEAD"),
            Criterion("public-candidate", "pass", "verified export and retained candidate"),
            *external_criteria,
        )
        report = Report(
            1,
            "pass" if all(criterion.status == "pass" for criterion in criteria) else "pending",
            head,
            criteria,
            evidence_ledger_sha256,
        )
        status = 0 if report.status == "pass" else 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report = Report(1, "failed", args.revision, (Criterion("source-revision", "failed", str(error)),))
        status = 2
    _write(args.output, report)
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Release check: {report.status.upper()} ({args.output})")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
