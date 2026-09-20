"""Hostile-event and failure contracts for GitHub PII redaction."""

import json
from pathlib import Path
from typing import Any

import check_pii_payload
import pytest

pytestmark = pytest.mark.unit


def _private_email() -> str:
    return "private.person" + "@corp.invalid"


def _event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, body: str, repo: str = "example/fieldkit") -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": repo},
                "pull_request": {"number": 42, "body": body},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")


def _raw_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_name: str,
    payload: dict[str, object],
) -> None:
    event_path = tmp_path / "event.json"
    payload["repository"] = {"full_name": "example/fieldkit"}
    event_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/fieldkit")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")


def test_patch_failure_is_nonzero_and_does_not_post_false_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sensitive = _private_email()
    _event(tmp_path, monkeypatch, body=f"contact {sensitive}")
    posted = False

    def _failed_patch(url: str, payload: dict[str, str]) -> None:
        raise check_pii_payload.GitHubAPIError("PATCH failed with HTTP 403")

    def _post(repo: str, issue_number: int, body: str) -> None:
        nonlocal posted
        posted = True

    monkeypatch.setattr(check_pii_payload, "_patch", _failed_patch)
    monkeypatch.setattr(check_pii_payload, "_post_comment", _post)

    result = check_pii_payload.main()

    output = capsys.readouterr()
    assert result == 1
    assert posted is False
    assert "HTTP 403" in output.err
    assert sensitive not in output.out + output.err


def test_notice_failure_is_observable_after_successful_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _event(tmp_path, monkeypatch, body=f"contact {_private_email()}")
    patched: list[dict[str, str]] = []
    monkeypatch.setattr(check_pii_payload, "_patch", lambda url, payload: patched.append(payload))

    def _failed_post(repo: str, issue_number: int, body: str) -> None:
        raise check_pii_payload.GitHubAPIError("POST failed with HTTP 503")

    monkeypatch.setattr(check_pii_payload, "_post_comment", _failed_post)

    result = check_pii_payload.main()

    assert result == 1
    assert patched == [{"body": "contact <redacted-email>"}]


def test_repository_mismatch_stops_before_api_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _event(tmp_path, monkeypatch, body=f"contact {_private_email()}", repo="example/fieldkit")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/other")
    monkeypatch.setattr(
        check_pii_payload,
        "_patch",
        lambda url, payload: pytest.fail("mismatched repository must not reach PATCH"),
    )

    assert check_pii_payload.main() == 1


def test_oversized_event_stops_before_json_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_bytes(b"{" + b"x" * 32)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/fieldkit")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(check_pii_payload, "_MAX_EVENT_BYTES", 16)

    assert check_pii_payload.main() == 1


def test_http_request_uses_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _urlopen(request: object, *, timeout: float) -> _Response:
        observed["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(check_pii_payload.urllib.request, "urlopen", _urlopen)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")

    check_pii_payload._request("PATCH", "https://api.github.com/repos/example/fieldkit/issues/42", {"body": "safe"})

    assert observed == {"timeout": check_pii_payload._REQUEST_TIMEOUT_SECONDS}


def test_no_match_performs_no_api_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _event(tmp_path, monkeypatch, body="No sensitive values here.")
    monkeypatch.setattr(
        check_pii_payload,
        "_patch",
        lambda url, payload: pytest.fail("clean body must not reach PATCH"),
    )

    assert check_pii_payload.main() == 0


def test_multiple_matches_are_redacted_without_logging_source_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sensitive = _private_email()
    body = f"first {sensitive}\nsecond {sensitive}"
    _event(tmp_path, monkeypatch, body=body)
    patches: list[dict[str, str]] = []
    notices: list[str] = []
    monkeypatch.setattr(check_pii_payload, "_patch", lambda url, payload: patches.append(payload))
    monkeypatch.setattr(
        check_pii_payload,
        "_post_comment",
        lambda repo, number, notice: notices.append(notice),
    )
    result = check_pii_payload.main()

    output = capsys.readouterr()
    assert result == 0
    assert patches == [{"body": "first <redacted-email>\nsecond <redacted-email>"}]
    assert "(x2)" in notices[0]
    assert sensitive not in output.out + output.err + notices[0]


@pytest.mark.parametrize("head_repo", ["example/fieldkit", "fork/fieldkit"], ids=["same-repo", "fork"])
def test_malicious_pull_request_fields_remain_inert_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head_repo: str
) -> None:
    sensitive = _private_email()
    body = f"$(touch /tmp/never-run) {sensitive}"
    _raw_event(
        tmp_path,
        monkeypatch,
        event_name="pull_request_target",
        payload={
            "action": "opened",
            "pull_request": {
                "number": 12,
                "body": body,
                "head": {"sha": "../../contributor-ref", "repo": {"full_name": head_repo}},
            },
        },
    )
    patches: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(check_pii_payload, "_patch", lambda url, payload: patches.append((url, payload)))
    monkeypatch.setattr(check_pii_payload, "_post_comment", lambda repo, number, notice: None)

    result = check_pii_payload.main()

    assert result == 0
    assert patches == [
        (
            "https://api.github.com/repos/example/fieldkit/pulls/12",
            {"body": "$(touch /tmp/never-run) <redacted-email>"},
        )
    ]


@pytest.mark.parametrize(
    ("event_name", "action", "payload"),
    [
        pytest.param("issues", "opened", {"issue": {"number": 1, "body": "safe"}}, id="issues-opened"),
        pytest.param("issues", "edited", {"issue": {"number": 1, "body": "safe"}}, id="issues-edited"),
        pytest.param(
            "pull_request_target",
            "opened",
            {"pull_request": {"number": 2, "body": "safe"}},
            id="pull-request-opened",
        ),
        pytest.param(
            "pull_request_target",
            "edited",
            {"pull_request": {"number": 2, "body": "safe"}},
            id="pull-request-edited",
        ),
        pytest.param(
            "issue_comment",
            "created",
            {"issue": {"number": 3}, "comment": {"id": 30, "body": "safe"}},
            id="issue-comment-created",
        ),
        pytest.param(
            "issue_comment",
            "edited",
            {"issue": {"number": 3}, "comment": {"id": 30, "body": "safe"}},
            id="issue-comment-edited",
        ),
        pytest.param(
            "pull_request_review_comment",
            "created",
            {"pull_request": {"number": 4}, "comment": {"id": 40, "body": "safe"}},
            id="review-comment-created",
        ),
        pytest.param(
            "pull_request_review_comment",
            "edited",
            {"pull_request": {"number": 4}, "comment": {"id": 40, "body": "safe"}},
            id="review-comment-edited",
        ),
    ],
)
def test_every_supported_event_action_routes_without_a_write_for_clean_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_name: str,
    action: str,
    payload: dict[str, object],
) -> None:
    payload["action"] = action
    _raw_event(tmp_path, monkeypatch, event_name=event_name, payload=payload)
    monkeypatch.setattr(check_pii_payload, "_patch", lambda url, body: pytest.fail("clean body must not be patched"))

    assert check_pii_payload.main() == 0


@pytest.mark.parametrize(
    ("event_name", "payload", "expected_patch_suffix", "expected_notice_number"),
    [
        pytest.param(
            "issues",
            {"action": "opened", "issue": {"number": 7, "body": "contact " + _private_email()}},
            "/issues/7",
            7,
            id="issue-body",
        ),
        pytest.param(
            "pull_request_target",
            {"action": "edited", "pull_request": {"number": 8, "body": "contact " + _private_email()}},
            "/pulls/8",
            8,
            id="pull-request-body",
        ),
        pytest.param(
            "issue_comment",
            {
                "action": "created",
                "issue": {"number": 9},
                "comment": {"id": 90, "body": "contact " + _private_email()},
            },
            "/issues/comments/90",
            9,
            id="issue-comment",
        ),
        pytest.param(
            "pull_request_review_comment",
            {
                "action": "edited",
                "pull_request": {"number": 10},
                "comment": {"id": 100, "body": "contact " + _private_email()},
            },
            "/pulls/comments/100",
            10,
            id="review-comment",
        ),
    ],
)
def test_supported_event_routes_patch_and_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_name: str,
    payload: dict[str, object],
    expected_patch_suffix: str,
    expected_notice_number: int,
) -> None:
    _raw_event(tmp_path, monkeypatch, event_name=event_name, payload=payload)
    patches: list[tuple[str, dict[str, str]]] = []
    notices: list[tuple[str, int, str]] = []
    monkeypatch.setattr(check_pii_payload, "_patch", lambda url, body: patches.append((url, body)))
    monkeypatch.setattr(
        check_pii_payload,
        "_post_comment",
        lambda repo, number, body: notices.append((repo, number, body)),
    )

    result = check_pii_payload.main()

    assert result == 0
    assert patches[0][0].endswith(expected_patch_suffix)
    assert patches[0][1] == {"body": "contact <redacted-email>"}
    assert notices[0][0:2] == ("example/fieldkit", expected_notice_number)
    assert _private_email() not in notices[0][2]


def test_unsupported_action_stops_before_api_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_event(
        tmp_path,
        monkeypatch,
        event_name="issues",
        payload={"action": "deleted", "issue": {"number": 7, "body": "contact " + _private_email()}},
    )
    monkeypatch.setattr(
        check_pii_payload,
        "_patch",
        lambda url, body: pytest.fail("unsupported action must not reach PATCH"),
    )

    assert check_pii_payload.main() == 1


def test_oversized_body_stops_before_api_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_event(
        tmp_path,
        monkeypatch,
        event_name="issues",
        payload={"action": "opened", "issue": {"number": 7, "body": "x" * 33}},
    )
    monkeypatch.setattr(check_pii_payload, "_MAX_BODY_CHARS", 32)
    monkeypatch.setattr(
        check_pii_payload,
        "_patch",
        lambda url, body: pytest.fail("oversized body must not reach PATCH"),
    )

    assert check_pii_payload.main() == 1


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"action": "opened"}, id="missing-issue"),
        pytest.param({"action": "opened", "issue": {"number": "../../7", "body": "safe"}}, id="string-id"),
        pytest.param({"action": "opened", "issue": {"number": -1, "body": "safe"}}, id="negative-id"),
        pytest.param({"action": "opened", "issue": {"number": True, "body": "safe"}}, id="boolean-id"),
        pytest.param({"action": "opened", "issue": {"number": 7, "body": ["not", "text"]}}, id="non-text-body"),
    ],
)
def test_malformed_routing_fields_stop_before_api_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    _raw_event(tmp_path, monkeypatch, event_name="issues", payload=payload)
    monkeypatch.setattr(
        check_pii_payload,
        "_patch",
        lambda url, body: pytest.fail("invalid route must not reach PATCH"),
    )

    assert check_pii_payload.main() == 1


def test_forged_repository_name_stops_before_api_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _raw_event(
        tmp_path,
        monkeypatch,
        event_name="issues",
        payload={"action": "opened", "issue": {"number": 7, "body": "safe"}},
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/fieldkit/../../other")
    monkeypatch.setattr(
        check_pii_payload,
        "_patch",
        lambda url, body: pytest.fail("forged repository must not reach PATCH"),
    )

    assert check_pii_payload.main() == 1


@pytest.mark.parametrize("repo", ["../fieldkit", "example/..", "-owner/fieldkit", "owner-/fieldkit"])
def test_invalid_repository_segment_stops_before_api_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: str
) -> None:
    _event(tmp_path, monkeypatch, body=f"contact {_private_email()}", repo=repo)
    monkeypatch.setattr(
        check_pii_payload,
        "_patch",
        lambda url, body: pytest.fail("invalid repository must not reach PATCH"),
    )

    assert check_pii_payload.main() == 1


def test_invalid_json_stops_before_api_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_bytes(b"\xff")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/fieldkit")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(
        check_pii_payload,
        "_patch",
        lambda url, body: pytest.fail("invalid JSON must not reach PATCH"),
    )

    assert check_pii_payload.main() == 1
