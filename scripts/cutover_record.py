"""Validate the one evidence record binding an approved export to public cutover."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

if __package__:
    from scripts.json_policy import reject_duplicate_json_keys
else:
    from json_policy import reject_duplicate_json_keys

_SCHEMA_PATH = Path("docs/release-readiness/cutover-record.schema.json")
_PUBLIC_ORIGINS = frozenset(
    {
        "https://github.com/mpeter/fieldkit-cli",
        "https://github.com/mpeter/fieldkit-cli.git",
        "git@github.com:mpeter/fieldkit-cli.git",  # pii-guard: ignore - Git SSH service account
        "ssh://git@github.com/mpeter/fieldkit-cli.git",  # pii-guard: ignore - Git SSH service account
    }
)


def _object(value: object, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{subject} must be an object")
    return value


def _candidate_artifacts(candidate_report: dict[str, Any]) -> set[tuple[str, str, str]]:
    validation = _object(candidate_report.get("artifact_validation"), "candidate artifact validation")
    artifacts = validation.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("candidate artifact validation must list artifacts")
    identities = {
        (item.get("name"), item.get("kind"), item.get("sha256"))
        for item in artifacts
        if isinstance(item, dict) and item.get("status") == "pass"
    }
    if len(identities) != 2 or any(not all(isinstance(value, str) for value in item) for item in identities):
        raise ValueError("candidate must retain exactly one passing wheel and source distribution")
    return {
        (name, kind, digest)
        for name, kind, digest in identities
        if isinstance(name, str) and isinstance(kind, str) and isinstance(digest, str)
    }


def validate(record: object, candidate_report: object, *, repo_root: Path = Path()) -> None:
    """Reject a record that is malformed or not bound to the approved export."""
    schema = json.loads((repo_root / _SCHEMA_PATH).read_text(encoding="utf-8"))
    record_object = _object(record, "cutover record")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(record_object), key=lambda error: list(error.absolute_path)
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"cutover record schema violation at {location}: {errors[0].message}")
    report = _object(candidate_report, "candidate report")
    if report.get("status") != "pass":
        raise ValueError("candidate report must pass before cutover evidence is accepted")
    manifest = _object(report.get("export_manifest"), "candidate export manifest")
    candidate = _object(record_object.get("candidate"), "cutover record candidate")
    expected = {
        "repository": manifest.get("expected_repository", report.get("expected_repository")),
        "source_sha": manifest.get("source_commit"),
        "source_tree": manifest.get("source_tree"),
        "export_policy_sha256": manifest.get("policy_sha256"),
        "exported_tree": manifest.get("exported_tree"),
        "planned_tag": manifest.get("planned_tag", report.get("planned_tag")),
    }
    for field, value in expected.items():
        if candidate.get(field) != value:
            raise ValueError(f"cutover record {field.replace('_', ' ')} does not match the approved candidate")
    recorded_artifacts = {
        (item.get("name"), item.get("kind"), item.get("sha256"))
        for item in candidate["artifacts"]
        if isinstance(item, dict)
    }
    if recorded_artifacts != _candidate_artifacts(report):
        raise ValueError("cutover record artifacts do not match the approved candidate")
    public = _object(record_object.get("public"), "cutover record public identity")
    if public.get("initial_tree") != candidate["exported_tree"]:
        raise ValueError("cutover record initial public tree does not match the approved exported tree")
    initial_commit = public.get("initial_commit")
    if not any(
        isinstance(run, dict)
        and run.get("name") == "Cutover verification"
        and run.get("path") == ".github/workflows/cutover.yml"
        and run.get("event") == "push"
        and run.get("head_sha") == initial_commit
        for run in public["workflow_runs"]
    ):
        raise ValueError("cutover record requires a successful root-commit verification workflow run")


def _command(repository: Path, argv: list[str]) -> str:
    """Run one bounded read-only public identity query."""
    completed = subprocess.run(argv, cwd=repository, capture_output=True, text=True, check=False, timeout=30)
    if completed.returncode != 0:
        raise ValueError(f"public cutover verification failed: {' '.join(argv)}")
    return completed.stdout.strip()


def verify_public_identity(record: object, public_repository: Path) -> None:
    """Resolve every recorded public identity from a fresh checkout and GitHub."""
    record_object = _object(record, "cutover record")
    public = _object(record_object.get("public"), "cutover record public identity")
    if not public_repository.is_dir() or not (public_repository / ".git").exists():
        raise ValueError("public repository must be a fresh Git checkout")
    if _command(public_repository, ["git", "remote", "get-url", "origin"]) not in _PUBLIC_ORIGINS:
        raise ValueError("public repository origin does not identify mpeter/fieldkit-cli")
    commit = public["initial_commit"]
    if _command(public_repository, ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"]) != commit:
        raise ValueError("initial public commit is unavailable from the public checkout")
    if _command(public_repository, ["git", "rev-parse", f"{commit}^{{tree}}"]) != public["initial_tree"]:
        raise ValueError("initial public tree does not match the public checkout")
    if _command(public_repository, ["git", "rev-list", "--parents", "-n", "1", commit]) != commit:
        raise ValueError("initial public commit must be the root of the clean history")
    if _command(public_repository, ["git", "rev-parse", "refs/remotes/origin/main"]) != commit:
        raise ValueError("public main does not point to the recorded initial commit")
    repository_id = _command(public_repository, ["gh", "api", "repos/mpeter/fieldkit-cli", "--jq", ".id"])
    if repository_id != str(public["repository_id"]):
        raise ValueError("public repository ID does not match the cutover record")
    for run in public["workflow_runs"]:
        if not isinstance(run, dict):
            raise ValueError("cutover workflow run must be an object")
        raw = _command(
            public_repository,
            ["gh", "api", f"repos/mpeter/fieldkit-cli/actions/runs/{run['id']}"],
        )
        try:
            observed = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            raise ValueError("public workflow proof returned invalid JSON") from exc
        if not isinstance(observed, dict) or (
            observed.get("id") != run["id"]
            or observed.get("run_attempt") != run["attempt"]
            or observed.get("head_sha") != run["head_sha"]
            or observed.get("status") != "completed"
            or observed.get("conclusion") != run["conclusion"]
            or observed.get("name") != run["name"]
            or observed.get("event") != run["event"]
            or not isinstance(observed.get("path"), str)
            or observed["path"].partition("@")[0] != run["path"]
        ):
            raise ValueError("public workflow run does not match the cutover record")


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_json_keys)


def main(argv: list[str] | None = None) -> int:
    """Validate one candidate-bound cutover record, with optional live verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--candidate-report", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path())
    parser.add_argument("--public-repository", type=Path)
    args = parser.parse_args(argv)
    try:
        record = _load(args.record)
        validate(record, _load(args.candidate_report), repo_root=args.repo_root)
        if args.public_repository is not None:
            verify_public_identity(record, args.public_repository)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(f"Cutover record: ERROR: {error}", file=sys.stderr)
        return 2
    print("Cutover record: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
