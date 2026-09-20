"""Render fail-closed evidence for one production release-promotion run."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from scripts import release_consumer

PRODUCTION_BOUNDARIES = (
    "build",
    "validate",
    "attest",
    "publish_pypi",
    "consumer_pypi",
    "github_release",
)
_OUTCOMES = frozenset({"success", "failure", "cancelled", "skipped", "missing"})
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG_REF = re.compile(r"^refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BOUNDARY_ACTIONS = {
    "build": "scripts/check_public_candidate.py",
    "validate": "scripts/release_bundle.py",
    "attest": "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6",
    "publish_pypi": "pypa/gh-action-pypi-publish@ec4db0b4ddc65acdf4bff5fa45ac92d78b56bdf0",
    "consumer_pypi": "scripts/check_release_consumer.py",
    "github_release": "gh release create",
}


def _validated_outcomes(values: list[str]) -> dict[str, str]:
    """Parse one exact, non-duplicated outcome for every release boundary."""
    outcomes: dict[str, str] = {}
    for value in values:
        name, separator, outcome = value.partition("=")
        if not separator or not name or not outcome or name in outcomes:
            raise ValueError("outcomes must be unique name=result values")
        outcomes[name] = outcome
    return outcomes


def _valid_candidate_artifacts(candidate: dict[str, object]) -> bool:
    """Return whether candidate evidence identifies exactly one wheel and sdist."""
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        return False
    by_kind: dict[str, dict[str, object]] = {}
    names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return False
        kind = artifact.get("kind")
        name = artifact.get("name")
        digest = artifact.get("sha256")
        if (
            not isinstance(kind, str)
            or not isinstance(name, str)
            or not isinstance(digest, str)
            or kind in by_kind
            or not name
            or name in names
            or "/" in name
            or "\\" in name
            or _SHA256.fullmatch(digest) is None
        ):
            return False
        by_kind[kind] = artifact
        names.add(name)
    return set(by_kind) == {"wheel", "sdist"}


def render(
    *,
    source_revision: str,
    source_repository: str,
    source_ref: str,
    run_id: int,
    run_attempt: int,
    outcomes: dict[str, str],
    candidate: dict[str, object] | None,
) -> dict[str, object]:
    """Return one closed production-promotion record from GitHub job conclusions."""
    if _REVISION.fullmatch(source_revision) is None:
        raise ValueError("source revision must be a full lowercase commit SHA")
    if _REPOSITORY.fullmatch(source_repository) is None:
        raise ValueError("source repository must be an owner/name identifier")
    if _TAG_REF.fullmatch(source_ref) is None:
        raise ValueError("source ref must be a semantic-version tag ref")
    if run_id < 1 or run_attempt < 1:
        raise ValueError("run identity must be positive")
    if set(outcomes) != set(PRODUCTION_BOUNDARIES) or any(value not in _OUTCOMES for value in outcomes.values()):
        raise ValueError("outcomes must contain every production boundary with a supported result")
    if candidate is not None and (
        candidate.get("repository") != source_repository
        or candidate.get("planned_tag") != source_ref.removeprefix("refs/tags/")
        or candidate.get("source_commit") != source_revision
        or not _valid_candidate_artifacts(candidate)
    ):
        raise ValueError("candidate must match the workflow context and contain one wheel and sdist")
    passed = candidate is not None and all(outcomes[name] == "success" for name in PRODUCTION_BOUNDARIES)
    return {
        "schema_version": 1,
        "kind": "release-promotion",
        "status": "success" if passed else "failed",
        "source_repository": source_repository,
        "source_ref": source_ref,
        "source_revision": source_revision,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "outcomes": {name: outcomes[name] for name in PRODUCTION_BOUNDARIES},
        "boundary_actions": {name: _BOUNDARY_ACTIONS[name] for name in PRODUCTION_BOUNDARIES},
        "candidate": candidate,
    }


def main(argv: list[str] | None = None) -> int:
    """Write one promotion record from fixed workflow-bound arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--outcome", action="append", default=[])
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--candidate-report", type=Path)
    parser.add_argument("--candidate-unavailable", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        supplied_candidate = args.bundle is not None or args.candidate_report is not None
        candidate: dict[str, object] | None = None
        if args.candidate_unavailable:
            if supplied_candidate:
                raise ValueError("supply a bundle and candidate report, or declare the candidate unavailable")
        else:
            if args.bundle is None or args.candidate_report is None:
                raise ValueError("supply a bundle and candidate report, or declare the candidate unavailable")
            expected = release_consumer.expected_release(args.bundle, args.candidate_report)
            if expected.source_commit != args.source_revision:
                raise ValueError("verified candidate source commit does not match the workflow revision")
            candidate = {
                "repository": expected.repository,
                "planned_tag": expected.planned_tag,
                "source_commit": expected.source_commit,
                "artifacts": [
                    {"name": artifact.name, "sha256": artifact.sha256, "kind": artifact.kind}
                    for artifact in expected.artifacts
                ],
            }
        report = render(
            source_revision=args.source_revision,
            source_repository=args.source_repository,
            source_ref=args.source_ref,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            outcomes=_validated_outcomes(args.outcome),
            candidate=candidate,
        )
        if args.output.parent.is_symlink() or not args.output.parent.is_dir():
            raise ValueError("evidence output parent must be a directory")
        with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write((json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
                temporary.replace(args.output)
            except OSError:
                temporary.unlink(missing_ok=True)
                raise
    except (OSError, ValueError) as error:
        print(f"Release promotion evidence: ERROR: {error}", file=sys.stderr)
        return 2
    print(f"Release promotion evidence: {report['status']}")
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
