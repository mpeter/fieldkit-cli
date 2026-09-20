"""Contracts for release-promotion evidence."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import release_promotion_evidence

pytestmark = pytest.mark.unit


def _candidate(revision: str) -> dict[str, object]:
    return {
        "repository": "example/fieldkit-cli",
        "planned_tag": "v1.0.0",
        "source_commit": revision,
        "artifacts": [
            {"name": "fieldkit_cli-1.0.0-py3-none-any.whl", "sha256": "b" * 64, "kind": "wheel"},
            {"name": "fieldkit_cli-1.0.0.tar.gz", "sha256": "c" * 64, "kind": "sdist"},
        ],
    }


def test_success_requires_every_production_boundary() -> None:
    report = release_promotion_evidence.render(
        source_revision="a" * 40,
        source_repository="example/fieldkit-cli",
        source_ref="refs/tags/v1.0.0",
        run_id=1,
        run_attempt=2,
        outcomes=dict.fromkeys(release_promotion_evidence.PRODUCTION_BOUNDARIES, "success"),
        candidate=_candidate("a" * 40),
    )

    assert report["status"] == "success"
    assert report["outcomes"]["publish_pypi"] == "success"
    schema = json.loads(
        (Path("docs/release-readiness/release-promotion-evidence.schema.json")).read_text(encoding="utf-8")
    )
    assert list(Draft202012Validator(schema).iter_errors(report)) == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "missing"])
def test_partial_or_cancelled_boundary_is_non_passing(result: str) -> None:
    outcomes = dict.fromkeys(release_promotion_evidence.PRODUCTION_BOUNDARIES, "success")
    outcomes["github_release"] = result

    report = release_promotion_evidence.render(
        source_revision="b" * 40,
        source_repository="example/fieldkit-cli",
        source_ref="refs/tags/v1.0.0",
        run_id=3,
        run_attempt=1,
        outcomes=outcomes,
        candidate=_candidate("b" * 40),
    )

    assert report["status"] == "failed"
    assert report["outcomes"]["github_release"] == result


def test_unknown_or_missing_boundary_is_rejected() -> None:
    with pytest.raises(ValueError, match="outcomes"):
        release_promotion_evidence.render(
            source_revision="c" * 40,
            source_repository="example/fieldkit-cli",
            source_ref="refs/tags/v1.0.0",
            run_id=1,
            run_attempt=1,
            outcomes={"build": "success"},
            candidate=_candidate("c" * 40),
        )


def test_missing_candidate_is_schema_valid_but_non_passing() -> None:
    report = release_promotion_evidence.render(
        source_revision="d" * 40,
        source_repository="example/fieldkit-cli",
        source_ref="refs/tags/v1.0.0",
        run_id=4,
        run_attempt=1,
        outcomes=dict.fromkeys(release_promotion_evidence.PRODUCTION_BOUNDARIES, "missing"),
        candidate=None,
    )

    assert report["status"] == "failed"
    assert report["candidate"] is None


def test_duplicate_outcomes_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        release_promotion_evidence._validated_outcomes(["build=success", "build=failure"])


def test_candidate_must_match_the_workflow_context() -> None:
    with pytest.raises(ValueError, match="must match"):
        release_promotion_evidence.render(
            source_revision="e" * 40,
            source_repository="example/fieldkit-cli",
            source_ref="refs/tags/v1.0.0",
            run_id=5,
            run_attempt=1,
            outcomes=dict.fromkeys(release_promotion_evidence.PRODUCTION_BOUNDARIES, "success"),
            candidate=_candidate("f" * 40),
        )


def test_success_rejects_an_incomplete_candidate() -> None:
    with pytest.raises(ValueError, match="wheel and sdist"):
        release_promotion_evidence.render(
            source_revision="e" * 40,
            source_repository="example/fieldkit-cli",
            source_ref="refs/tags/v1.0.0",
            run_id=5,
            run_attempt=1,
            outcomes=dict.fromkeys(release_promotion_evidence.PRODUCTION_BOUNDARIES, "success"),
            candidate={**_candidate("e" * 40), "artifacts": []},
        )


def test_schema_rejects_a_success_claim_without_a_complete_candidate() -> None:
    report = release_promotion_evidence.render(
        source_revision="f" * 40,
        source_repository="example/fieldkit-cli",
        source_ref="refs/tags/v1.0.0",
        run_id=6,
        run_attempt=1,
        outcomes=dict.fromkeys(release_promotion_evidence.PRODUCTION_BOUNDARIES, "failure"),
        candidate=None,
    )
    report["status"] = "success"
    schema = json.loads(
        Path("docs/release-readiness/release-promotion-evidence.schema.json").read_text(encoding="utf-8")
    )

    assert list(Draft202012Validator(schema).iter_errors(json.loads(json.dumps(report))))


def test_cli_writes_failed_evidence_when_candidate_is_unavailable(tmp_path: Path) -> None:
    output = tmp_path / "promotion-evidence.json"
    arguments = [
        "--source-revision",
        "e" * 40,
        "--source-repository",
        "example/fieldkit-cli",
        "--source-ref",
        "refs/tags/v1.0.0",
        "--run-id",
        "5",
        "--run-attempt",
        "1",
        "--candidate-unavailable",
        "--output",
        str(output),
    ]
    for boundary in release_promotion_evidence.PRODUCTION_BOUNDARIES:
        arguments.extend(["--outcome", f"{boundary}=missing"])

    result = release_promotion_evidence.main(arguments)
    report = json.loads(output.read_text(encoding="utf-8"))

    assert result == 1
    assert report["candidate"] is None
    assert report["status"] == "failed"
