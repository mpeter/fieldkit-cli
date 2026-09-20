"""Contracts for the exact-candidate public cutover record."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import cutover_record

pytestmark = pytest.mark.unit


def _candidate() -> dict[str, object]:
    return {
        "status": "pass",
        "expected_repository": "mpeter/fieldkit-cli",
        "planned_tag": "v1.0.0",
        "export_manifest": {
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "policy_sha256": "c" * 64,
            "exported_tree": "d" * 40,
        },
        "artifact_validation": {
            "source_revision": "a" * 40,
            "artifacts": [
                {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "kind": "wheel", "sha256": "e" * 64, "status": "pass"},
                {"name": "fieldkit_cli-1.0.0.tar.gz", "kind": "sdist", "sha256": "f" * 64, "status": "pass"},
            ],
        },
    }


def _record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate": {
            "repository": "mpeter/fieldkit-cli",
            "source_sha": "a" * 40,
            "source_tree": "b" * 40,
            "export_policy_sha256": "c" * 64,
            "exported_tree": "d" * 40,
            "planned_tag": "v1.0.0",
            "artifacts": [
                {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "kind": "wheel", "sha256": "e" * 64},
                {"name": "fieldkit_cli-1.0.0.tar.gz", "kind": "sdist", "sha256": "f" * 64},
            ],
        },
        "public": {
            "initial_commit": "a" * 40,
            "initial_tree": "d" * 40,
            "repository_id": 1,
            "workflow_runs": [
                {
                    "name": "Cutover verification",
                    "path": ".github/workflows/cutover.yml",
                    "event": "push",
                    "id": 2,
                    "attempt": 1,
                    "head_sha": "a" * 40,
                    "conclusion": "success",
                }
            ],
        },
    }


def test_validates_a_complete_record_bound_to_the_candidate() -> None:
    cutover_record.validate(_record(), _candidate())


def test_rejects_a_record_for_another_exported_tree() -> None:
    record = deepcopy(_record())
    candidate = record["candidate"]
    assert isinstance(candidate, dict)
    candidate["exported_tree"] = "9" * 40

    with pytest.raises(ValueError, match="exported tree"):
        cutover_record.validate(record, _candidate())


def test_rejects_a_record_without_successful_ci_for_the_initial_commit() -> None:
    record = deepcopy(_record())
    public = record["public"]
    assert isinstance(public, dict)
    runs = public["workflow_runs"]
    assert isinstance(runs, list)
    run = runs[0]
    assert isinstance(run, dict)
    run["head_sha"] = "9" * 40

    with pytest.raises(ValueError, match="root-commit verification workflow run"):
        cutover_record.validate(record, _candidate())


def test_resolves_the_recorded_public_identity_from_the_fresh_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    (tmp_path / ".git").mkdir()

    def command(repository: Path, argv: list[str]) -> str:
        assert repository == tmp_path
        if argv[:3] == ["git", "remote", "get-url"]:
            return "https://github.com/mpeter/fieldkit-cli.git"
        if argv[0:2] == ["git", "rev-parse"] and "^{commit}" in argv[-1]:
            return "a" * 40
        if argv[0:2] == ["git", "rev-parse"]:
            if argv[-1] == "refs/remotes/origin/main":
                return "a" * 40
            return "d" * 40
        if argv[0:2] == ["git", "rev-list"]:
            return "a" * 40
        if argv == ["gh", "api", "repos/mpeter/fieldkit-cli", "--jq", ".id"]:
            return "1"
        assert argv == ["gh", "api", "repos/mpeter/fieldkit-cli/actions/runs/2"]
        return json.dumps(
            {
                "id": 2,
                "run_attempt": 1,
                "head_sha": "a" * 40,
                "status": "completed",
                "conclusion": "success",
                "name": "Cutover verification",
                "event": "push",
                "path": ".github/workflows/cutover.yml@refs/heads/main",
            }
        )

    monkeypatch.setattr(cutover_record, "_command", command)

    cutover_record.verify_public_identity(record, tmp_path)
