"""Schema contracts for public-index consumer evidence."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator


def test_consumer_evidence_schema_accepts_pending_same_candidate_evidence() -> None:
    root = Path(__file__).parents[1]
    schema = json.loads(
        (root / "docs/release-readiness/release-consumer-evidence.schema.json").read_text(encoding="utf-8")
    )
    evidence = {
        "schema_version": 5,
        "status": "pending",
        "repository": "example/fieldkit-cli",
        "source_commit": "a" * 40,
        "planned_tag": "v1.0.0",
        "index_endpoint": "https://test.pypi.org/pypi/fieldkit-cli/1.0.0/json",
        "environment": {"system": "linux", "machine": "x86_64", "python_version": "3.11"},
        "artifacts": [
            {
                "kind": "wheel",
                "name": "fieldkit_cli-1.0.0-py3-none-any.whl",
                "sha256": "b" * 64,
                "observed": False,
                "downloaded": False,
            },
            {
                "kind": "sdist",
                "name": "fieldkit_cli-1.0.0.tar.gz",
                "sha256": "c" * 64,
                "observed": False,
                "downloaded": False,
            },
        ],
        "scenarios": [],
        "findings": ["expected version is not visible"],
    }

    assert list(Draft202012Validator(schema).iter_errors(evidence)) == []


def test_consumer_evidence_schema_rejects_a_download_that_was_not_observed() -> None:
    root = Path(__file__).parents[1]
    schema = json.loads(
        (root / "docs/release-readiness/release-consumer-evidence.schema.json").read_text(encoding="utf-8")
    )
    evidence = {
        "schema_version": 5,
        "status": "failed",
        "repository": "example/fieldkit-cli",
        "source_commit": "a" * 40,
        "planned_tag": "v1.0.0",
        "index_endpoint": "https://test.pypi.org/pypi/fieldkit-cli/1.0.0/json",
        "environment": {"system": "linux", "machine": "x86_64", "python_version": "3.11"},
        "artifacts": [
            {
                "kind": "wheel",
                "name": "fieldkit_cli-1.0.0-py3-none-any.whl",
                "sha256": "b" * 64,
                "observed": False,
                "downloaded": True,
            },
            {
                "kind": "sdist",
                "name": "fieldkit_cli-1.0.0.tar.gz",
                "sha256": "c" * 64,
                "observed": False,
                "downloaded": False,
            },
        ],
        "scenarios": [],
        "findings": ["missing expected wheel artifact"],
    }

    assert list(Draft202012Validator(schema).iter_errors(evidence))
