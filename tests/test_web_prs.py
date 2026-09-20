"""Tests for fieldkit.web.prs — PR queue listing and token-gated write actions."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fieldkit.errors import WebDataError
from fieldkit.web.data import DataSource
from fieldkit.web.prs import comment_pr, list_prs, merge_pr
from fieldkit.web.server import create_app

pytestmark = pytest.mark.unit

_REPO = "owner/repo"

_SAMPLE_PRS = [
    {
        "number": 7,
        "title": "old fix",
        "url": "https://github.com/owner/repo/pull/7",
        "author": {"login": "driver-bot"},
        "createdAt": "2026-07-10T00:00:00Z",
        "isDraft": False,
        "headRefName": "driver/issue-7",
        "reviewDecision": "",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "SUCCESS"}],
    },
    {
        "number": 12,
        "title": "new feature",
        "url": "https://github.com/owner/repo/pull/12",
        "author": {"login": "example-user"},
        "createdAt": "2026-07-11T00:00:00Z",
        "isDraft": True,
        "headRefName": "feat/thing",
        "reviewDecision": "REVIEW_REQUIRED",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"state": "PENDING"}],
    },
]


@pytest.fixture
def source(tmp_path: Path) -> DataSource:
    briefs = tmp_path / "briefs"
    watchers = tmp_path / "watchers"
    briefs.mkdir()
    watchers.mkdir()
    return DataSource(briefs_dir=briefs, watchers_dir=watchers, cli_json=lambda a: {})


def _recording_gh(calls: list[list[str]], stdout: str = "") -> Any:
    def runner(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["pr", "list"]:
            return json.dumps(_SAMPLE_PRS)
        return stdout

    return runner


# ---------------------------------------------------------------------------
# CI rollup summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rollup", "expected_state"),
    [
        ([{"conclusion": "SUCCESS"}], "passing"),
        ([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}], "failing"),
        ([{"conclusion": "SUCCESS"}, {"state": "PENDING"}], "pending"),
        ([], "none"),
        (None, "none"),
        ([{"conclusion": "SKIPPED"}, {"conclusion": "NEUTRAL"}], "passing"),
        ([{"state": "ERROR"}], "failing"),
    ],
)
def test_ci_summary_states(rollup: Any, expected_state: str) -> None:
    # Exercised through the public API: one PR carrying the rollup under test.
    pr = {"number": 1, "title": "t", "url": "u", "author": {}, "statusCheckRollup": rollup}
    result = list_prs(_REPO, gh_runner=lambda args: json.dumps([pr]))
    assert result[0]["ci"]["state"] == expected_state


# ---------------------------------------------------------------------------
# list / merge / comment domain functions
# ---------------------------------------------------------------------------


def test_list_prs_flattens_and_sorts_newest_first() -> None:
    calls: list[list[str]] = []
    result = list_prs(_REPO, gh_runner=_recording_gh(calls))
    assert [p["number"] for p in result] == [12, 7]
    assert result[1]["author"] == "driver-bot"
    assert result[1]["ci"]["state"] == "passing"
    assert result[0]["is_draft"] is True
    assert calls[0][:2] == ["pr", "list"]
    assert "-R" in calls[0]


def test_list_prs_raises_on_non_json() -> None:
    with pytest.raises(WebDataError, match="non-JSON"):
        list_prs(_REPO, gh_runner=lambda args: "not json")


def test_merge_pr_invokes_squash_with_branch_delete() -> None:
    calls: list[list[str]] = []
    result = merge_pr(_REPO, 7, gh_runner=_recording_gh(calls))
    assert "merged" in result
    assert calls[0][:3] == ["pr", "merge", "7"]
    assert "--squash" in calls[0]
    assert "--delete-branch" in calls[0]


def test_comment_pr_rejects_empty_body() -> None:
    with pytest.raises(WebDataError, match="empty comment"):
        comment_pr(_REPO, 7, "   ", gh_runner=_recording_gh([]))


def test_comment_pr_posts_body() -> None:
    calls: list[list[str]] = []
    result = comment_pr(_REPO, 7, "needs a regression test", gh_runner=_recording_gh(calls))
    assert "posted" in result
    assert "needs a regression test" in calls[0]


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


def test_prs_route_lists_and_reports_writes_disabled(source: DataSource) -> None:
    app = create_app(source, github_repo=_REPO, gh_runner=_recording_gh([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        result = client.get("/api/prs")
        assert result.status_code == 200
        assert result.json()["writes_enabled"] is False
        assert [p["number"] for p in result.json()["prs"]] == [12, 7]


def test_merge_route_disabled_without_token_even_on_loopback(source: DataSource) -> None:
    calls: list[list[str]] = []
    app = create_app(source, github_repo=_REPO, gh_runner=_recording_gh(calls))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        result = client.post("/api/prs/7/merge")
        assert result.status_code == 403
        assert "disabled" in result.json()["error"]
        assert calls == []  # gh never invoked


def test_merge_route_requires_valid_token(source: DataSource) -> None:
    calls: list[list[str]] = []
    app = create_app(source, token="s3cret", github_repo=_REPO, gh_runner=_recording_gh(calls))
    with TestClient(app) as client:
        denied = client.post("/api/prs/7/merge", headers={"Authorization": "Bearer wrong"})
        assert denied.status_code == 401
        allowed = client.post("/api/prs/7/merge", headers={"Authorization": "Bearer s3cret"})
        assert allowed.status_code == 200
        assert "merged" in allowed.json()["result"]
        assert any(c[:2] == ["pr", "merge"] for c in calls)


def test_merge_route_surfaces_gh_failure_as_502(source: DataSource) -> None:
    def failing_gh(args: list[str]) -> str:
        raise WebDataError("gh pr merge exited 1: base branch protection")

    app = create_app(source, token="s3cret", github_repo=_REPO, gh_runner=failing_gh)
    with TestClient(app) as client:
        result = client.post("/api/prs/7/merge", headers={"Authorization": "Bearer s3cret"})
        assert result.status_code == 502
        assert "branch protection" in result.json()["error"]


def test_comment_route_requires_token_and_posts(source: DataSource) -> None:
    calls: list[list[str]] = []
    app = create_app(source, token="s3cret", github_repo=_REPO, gh_runner=_recording_gh(calls))
    with TestClient(app) as client:
        denied = client.post("/api/prs/7/comment", json={"body": "bounce"})
        assert denied.status_code == 401
        allowed = client.post(
            "/api/prs/7/comment",
            json={"body": "bounce: fix the flaky test"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert allowed.status_code == 200
        assert "posted" in allowed.json()["result"]


def test_comment_route_maps_empty_body_to_400(source: DataSource) -> None:
    app = create_app(source, token="s3cret", github_repo=_REPO, gh_runner=_recording_gh([]))
    with TestClient(app) as client:
        result = client.post("/api/prs/7/comment", json={"body": "  "}, headers={"Authorization": "Bearer s3cret"})
        assert result.status_code == 400
