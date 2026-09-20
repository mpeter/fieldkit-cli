"""Contracts for the public-repository settings verifier."""

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import _repository_settings as verifier
import pytest

pytestmark = pytest.mark.unit


def _manifest() -> dict[str, Any]:
    return json.loads((Path(__file__).parents[1] / ".github" / "repository-settings.json").read_text(encoding="utf-8"))


def _public_observations() -> dict[str, verifier.ApiObservation]:
    manifest = _manifest()
    repository = manifest["repository"]
    actions = manifest["actions"]
    security = manifest["security"]
    return {
        "repository": verifier.ApiObservation(200, repository),
        "rulesets": verifier.ApiObservation(200, manifest["rulesets"]),
        "actions_permissions": verifier.ApiObservation(200, actions["permissions"]),
        "actions_selected": verifier.ApiObservation(200, actions["selected_actions"]),
        "workflow_permissions": verifier.ApiObservation(200, actions["workflow_permissions"]),
        "vulnerability_alerts": verifier.ApiObservation(204, None),
        "automated_security_fixes": verifier.ApiObservation(200, {"enabled": True, "paused": False}),
        "private_vulnerability_reporting": verifier.ApiObservation(200, {"enabled": True}),
        "security_and_analysis": verifier.ApiObservation(200, security["security_and_analysis"]),
    }


def test_post_cutover_matching_snapshot_passes() -> None:
    report = verifier.evaluate(_manifest(), _public_observations(), phase="post-cutover")

    assert report.status == "pass"
    assert report.failures == ()
    assert report.pending == ()


def test_post_cutover_snapshot_requires_the_exact_expected_revision() -> None:
    snapshot = verifier.EvidenceSnapshot(
        "example/fieldkit-cli",
        "2026-09-19T12:00:00Z",
        verifier.API_VERSION,
        "a" * 40,
        _public_observations(),
    )

    report = verifier.evaluate_snapshot(
        _manifest(),
        snapshot,
        phase="post-cutover",
        expected_revision="b" * 40,
        now=datetime(2026, 9, 19, 12, 1, tzinfo=UTC),
    )

    assert report.status == "fail"
    assert any(finding.control == "repository.default_branch" for finding in report.failures)


def test_post_cutover_snapshot_requires_fresh_evidence() -> None:
    now = datetime(2026, 9, 19, 12, 30, tzinfo=UTC)
    snapshot = verifier.EvidenceSnapshot(
        "example/fieldkit-cli",
        (now - timedelta(seconds=verifier.MAX_SNAPSHOT_AGE_SECONDS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        verifier.API_VERSION,
        "a" * 40,
        _public_observations(),
    )

    report = verifier.evaluate_snapshot(
        _manifest(), snapshot, phase="post-cutover", expected_revision="a" * 40, now=now
    )

    assert report.status == "fail"
    assert any(finding.control == "repository.evidence_freshness" for finding in report.failures)


def test_post_cutover_snapshot_rejects_missing_expected_revision() -> None:
    snapshot = verifier.EvidenceSnapshot(
        "example/fieldkit-cli",
        "2026-09-19T12:00:00Z",
        verifier.API_VERSION,
        "a" * 40,
        _public_observations(),
    )

    with pytest.raises(ValueError, match="expected full lowercase Git SHA"):
        verifier.evaluate_snapshot(_manifest(), snapshot, phase="post-cutover")


def test_post_cutover_missing_required_check_fails() -> None:
    observations = _public_observations()
    rulesets = json.loads(json.dumps(observations["rulesets"].data))
    main = next(rule for rule in rulesets if rule["name"] == "protect-main")
    required = next(rule for rule in main["rules"] if rule["type"] == "required_status_checks")
    required["parameters"]["required_status_checks"] = [{"context": "Required checks"}]
    observations["rulesets"] = verifier.ApiObservation(200, rulesets)

    report = verifier.evaluate(_manifest(), observations, phase="post-cutover")

    assert report.status == "fail"
    assert any(finding.control == "rulesets.protect-main" for finding in report.failures)


def test_post_cutover_disabled_private_reporting_fails() -> None:
    observations = _public_observations()
    observations["private_vulnerability_reporting"] = verifier.ApiObservation(404, {"message": "Not Found"})

    report = verifier.evaluate(_manifest(), observations, phase="post-cutover")

    assert report.status == "fail"
    assert any(finding.control == "security.private_vulnerability_reporting" for finding in report.failures)


def test_pre_cutover_expected_plan_limits_remain_pending() -> None:
    observations = _public_observations()
    observations["repository"] = verifier.ApiObservation(200, {**observations["repository"].data, "private": True})
    observations["rulesets"] = verifier.ApiObservation(
        403,
        {"message": "Upgrade to GitHub Pro or make this repository public to enable this feature."},
    )
    observations["private_vulnerability_reporting"] = verifier.ApiObservation(404, {"message": "Not Found"})
    observations["actions_selected"] = verifier.ApiObservation(
        409,
        {"message": "Conflict", "status": 409},
    )

    report = verifier.evaluate(_manifest(), observations, phase="pre-cutover")

    assert report.status == "pending"
    assert report.failures == ()
    assert {finding.control for finding in report.pending} >= {
        "repository.visibility",
        "rulesets",
        "security.private_vulnerability_reporting",
        "actions.selected_actions",
    }


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (403, {"message": "Forbidden"}),
        (404, {"message": "Not Found"}),
        (500, {"message": "Internal Server Error"}),
    ],
)
def test_pre_cutover_unexpected_ruleset_response_fails(status: int, payload: dict[str, str]) -> None:
    observations = _public_observations()
    observations["repository"] = verifier.ApiObservation(200, {**observations["repository"].data, "private": True})
    observations["rulesets"] = verifier.ApiObservation(status, payload)
    observations["private_vulnerability_reporting"] = verifier.ApiObservation(404, {"message": "Not Found"})

    report = verifier.evaluate(_manifest(), observations, phase="pre-cutover")

    assert report.status == "fail"
    assert any(finding.control == "rulesets" for finding in report.failures)


def test_manifest_defers_code_owner_review_until_second_maintainer() -> None:
    governance = _manifest()["governance"]

    assert governance["code_owner_review"]["status"] == "deferred"
    assert governance["code_owner_review"]["activate_when"] == "second-maintainer-recorded"
    main = next(rule for rule in _manifest()["rulesets"] if rule["name"] == "protect-main")
    pull_requests = next(rule for rule in main["rules"] if rule["type"] == "pull_request")
    assert pull_requests["parameters"]["require_code_owner_review"] is False


def test_selected_action_policy_matches_current_workflows_exactly() -> None:
    root = Path(__file__).parents[1]
    references = {
        match.group(1).partition("@")[0]
        for path in (root / ".github" / "workflows").glob("*.yml")
        for match in re.finditer(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)", path.read_text(encoding="utf-8"), re.MULTILINE)
        if not match.group(1).startswith("./")
    }
    selected = _manifest()["actions"]["selected_actions"]
    patterns = [pattern.partition("@")[0] for pattern in selected["patterns_allowed"]]

    assert selected["github_owned_allowed"] is False
    assert selected["verified_allowed"] is False
    assert all(any(fnmatchcase(reference, pattern) for pattern in patterns) for reference in references)
    assert all(any(fnmatchcase(reference, pattern) for reference in references) for pattern in patterns)


def test_snapshot_loader_rejects_missing_surface(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "example/project",
                "collected_at": "2026-09-11T05:15:09Z",
                "api_version": verifier.API_VERSION,
                "default_branch_sha": "a" * 40,
                "observations": {"repository": {"status": 200, "data": {}}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing observation surfaces"):
        verifier.load_snapshot(path, "example/project")


def test_snapshot_loader_returns_all_observations(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    snapshot = {
        "schema_version": 1,
        "repository": "example/project",
        "collected_at": "2026-09-11T05:15:09Z",
        "api_version": verifier.API_VERSION,
        "default_branch_sha": "a" * 40,
        "observations": {name: {"status": 200, "data": {"surface": name}} for name in verifier._SURFACES},
    }
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    evidence = verifier.load_snapshot(path, "example/project")

    assert evidence == verifier.EvidenceSnapshot(
        repository="example/project",
        collected_at="2026-09-11T05:15:09Z",
        api_version=verifier.API_VERSION,
        default_branch_sha="a" * 40,
        observations={name: verifier.ApiObservation(200, {"surface": name}) for name in verifier._SURFACES},
    )


def test_snapshot_loader_rejects_another_repository(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "example/other",
                "collected_at": "2026-09-11T05:15:09Z",
                "api_version": verifier.API_VERSION,
                "default_branch_sha": "a" * 40,
                "observations": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match"):
        verifier.load_snapshot(path, "example/project")


def test_repository_name_rejects_command_like_input() -> None:
    with pytest.raises(ValueError, match="OWNER/REPO"):
        verifier.validate_repository("example/project;touch-bad")


def test_repository_name_returns_valid_literal() -> None:
    repository = verifier.validate_repository("example/project")

    assert repository == "example/project"


def test_evaluate_rejects_missing_observation_surface() -> None:
    observations = _public_observations()
    del observations["rulesets"]

    with pytest.raises(ValueError, match="missing observation surfaces: rulesets"):
        verifier.evaluate(_manifest(), observations, phase="post-cutover")


def test_api_extracts_conflict_status_from_gh_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(
        args=["gh"],
        returncode=1,
        stdout="",
        stderr=(
            "gh: All actions and workflows are allowed on this repository (Conflict)\n"
            '{"message":"Conflict","status":"409"}\n'
        ),
    )
    monkeypatch.setattr(verifier.subprocess, "run", lambda *args, **kwargs: completed)

    observation = verifier._api("example/project", "actions/permissions/selected-actions")

    assert observation.status == 409
    assert observation.data == {"message": "Conflict", "status": "409"}


def test_api_rejects_non_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="not-json", stderr="")
    monkeypatch.setattr(verifier.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(RuntimeError, match="non-JSON data"):
        verifier._api("example/project", "rulesets")
