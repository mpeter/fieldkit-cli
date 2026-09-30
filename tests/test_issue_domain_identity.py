"""Canonical GitHub-number issue identity and irreversible-create safety."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from fieldkit.__main__ import main
from fieldkit.errors import AuthError, GitHubCreationUncertainError, GitHubDataError, GitHubRequestError
from fieldkit.issue import (
    GHIssue,
    GHIssueStore,
    advance_milestone_candidates,
    format_issue_id,
    milestone_exit_code,
    parse_issue_id,
    plan_milestone_sync,
)
from fieldkit.issue.github import _issue_from_gh
from fieldkit.issue.model import status_from_github

pytestmark = pytest.mark.unit

REPO = "owner/repo"


def _provider_issue(*, number: int = 42, title: str = "bug: public report", labels: list[str]) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": "OPEN",
        "stateReason": None,
        "labels": [{"name": label} for label in labels],
        "body": "public body",
        "createdAt": "2026-09-27T12:00:00+00:00",
    }


@pytest.mark.parametrize(
    ("number", "expected"),
    [(1, "fieldkit-001"), (42, "fieldkit-042"), (1000, "fieldkit-1000")],
)
def test_public_id_is_derived_only_from_github_number(number: int, expected: str) -> None:
    assert format_issue_id(number) == expected
    assert parse_issue_id(expected) == number


@pytest.mark.parametrize("value", ["fieldkit-000", "fieldkit--1", "fieldkit-abc", "other-042"])
def test_invalid_public_ids_are_rejected(value: str) -> None:
    with pytest.raises(GitHubDataError, match="issue identifier"):
        parse_issue_id(value)


def test_public_dispatcher_normalizes_invalid_module_to_data_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = MagicMock()
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: False)
    monkeypatch.setattr("fieldkit.issue.github.subprocess.run", run)

    result = main(["issue", "create", "--type", "bug", "--title", "test", "--module", "unknown"])

    assert result == 3
    assert capsys.readouterr().out == ""
    run.assert_not_called()


def test_legacy_title_number_cannot_override_canonical_identity() -> None:
    result = _issue_from_gh(
        _provider_issue(
            number=42, title="fieldkit-007: old private title", labels=["bug", "severity:high", "module:sf"]
        )
    )

    assert result is not None
    assert result.id == "fieldkit-042"
    assert result.gh_number == 42
    assert result.title == "old private title"


@pytest.mark.parametrize("labels", [["documentation"], ["question"], []])
def test_unmanaged_community_issues_are_filtered_without_error(labels: list[str]) -> None:
    assert _issue_from_gh(_provider_issue(labels=labels)) is None


@pytest.mark.parametrize(("type_label", "expected_type"), [("bug", "bug"), ("enhancement", "enhancement")])
def test_public_issue_forms_receive_safe_managed_defaults(type_label: str, expected_type: str) -> None:
    result = _issue_from_gh(_provider_issue(labels=[type_label]))

    assert result is not None
    assert result.type == expected_type
    assert result.severity == "medium"
    assert result.module == "other"


@pytest.mark.parametrize(
    "labels",
    [
        ["bug", "enhancement"],
        ["bug", "severity:future"],
        ["bug", "module:future"],
        ["bug", "severity:low", "severity:high"],
    ],
)
def test_malformed_managed_labels_fail_closed(labels: list[str]) -> None:
    with pytest.raises(GitHubDataError, match="labels"):
        _issue_from_gh(_provider_issue(labels=labels))


@pytest.mark.parametrize(
    ("state", "reason", "labels"),
    [
        ("OPEN", "COMPLETED", []),
        ("OPEN", None, ["status:fixed"]),
        ("CLOSED", None, []),
        ("CLOSED", "COMPLETED", ["status:planned"]),
        ("CLOSED", "NOT_PLANNED", ["status:fixed"]),
    ],
)
def test_inconsistent_provider_status_fails_closed(state: str, reason: str | None, labels: list[str]) -> None:
    with pytest.raises(GitHubDataError, match="inconsistent"):
        status_from_github(state, reason, labels)


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr=stderr)


def test_create_uses_plain_title_and_atomic_github_number(monkeypatch: pytest.MonkeyPatch) -> None:
    call = MagicMock(return_value=_completed(json.dumps({"number": 101})))
    monkeypatch.setattr("fieldkit.issue.github.subprocess.run", call)

    result = GHIssueStore(REPO).create(issue_type="bug", title="plain public title", module="issue")

    assert result.id == "fieldkit-101"
    argv = call.call_args.args[0]
    assert "title=plain public title" in argv
    assert not any("fieldkit-" in token for token in argv if token.startswith("title="))


@pytest.mark.parametrize(
    "stdout",
    ["", "101", "{}", '{"number": true}', '{"number": "101"}', '{"number": 101, "extra": true}'],
)
def test_successful_post_with_unprovable_identity_is_nonretryable(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr("fieldkit.issue.github.subprocess.run", MagicMock(return_value=_completed(stdout)))

    with pytest.raises(GitHubCreationUncertainError, match="may have been created"):
        GHIssueStore(REPO).create(issue_type="bug", title="plain public title")


def test_timed_out_post_is_nonretryable_uncertainty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fieldkit.issue.github.subprocess.run",
        MagicMock(side_effect=subprocess.TimeoutExpired(["gh"], 5)),
    )

    with pytest.raises(GitHubCreationUncertainError, match="may have been created"):
        GHIssueStore(REPO).create(issue_type="bug", title="plain public title")


def test_duplicate_create_identity_key_is_nonretryable_uncertainty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fieldkit.issue.github.subprocess.run",
        MagicMock(return_value=_completed('{"number": 101, "number": 202}')),
    )

    with pytest.raises(GitHubCreationUncertainError, match="may have been created"):
        GHIssueStore(REPO).create(issue_type="bug", title="plain public title")


def test_explicit_rate_limit_rejection_remains_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fieldkit.issue.github.subprocess.run",
        MagicMock(return_value=_completed("", returncode=1, stderr="rate limit exceeded (HTTP 403)")),
    )

    with pytest.raises(GitHubRequestError, match="rate limit"):
        GHIssueStore(REPO).create(issue_type="bug", title="plain public title")


def test_explicit_invalid_post_is_data_error_not_uncertainty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fieldkit.issue.github.subprocess.run",
        MagicMock(return_value=_completed("", returncode=1, stderr="validation failed (HTTP 422)")),
    )

    with pytest.raises(GitHubDataError, match="rejected"):
        GHIssueStore(REPO).create(issue_type="bug", title="plain public title")


def _issue(status: str = "open") -> GHIssue:
    return GHIssue(
        id="fieldkit-042",
        type="bug",
        title="bug",
        status=status,  # type: ignore[arg-type]
        severity="high",
        module="issue",
        gh_number=42,
        created=datetime(2026, 9, 27, tzinfo=UTC),
    )


def test_mark_fixed_labels_before_close(monkeypatch: pytest.MonkeyPatch) -> None:
    store = GHIssueStore(REPO)
    monkeypatch.setattr(store, "find", lambda _issue_id: _issue())
    gh = MagicMock(return_value="")
    monkeypatch.setattr("fieldkit.issue.github._gh", gh)

    result = store.mark_fixed("fieldkit-042")

    assert result is not None
    calls = [call.args for call in gh.call_args_list]
    assert "edit" in calls[0] and "--add-label" in calls[0]
    assert "close" in calls[1]


def test_closed_without_fixed_label_is_repairable(monkeypatch: pytest.MonkeyPatch) -> None:
    store = GHIssueStore(REPO)
    monkeypatch.setattr(store, "find", lambda _issue_id: _issue("closed"))
    gh = MagicMock(return_value="")
    monkeypatch.setattr("fieldkit.issue.github._gh", gh)

    result = store.mark_fixed("fieldkit-042")

    assert result is not None
    assert result.status == "fixed"
    calls = [call.args for call in gh.call_args_list]
    assert len(calls) == 1
    assert "--add-label" in calls[0]


@pytest.mark.parametrize("target", ["open", "planned"])
def test_fixed_issue_removes_fixed_label_before_reopening(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    store = GHIssueStore(REPO)
    monkeypatch.setattr(store, "find", lambda _issue_id: _issue("fixed"))
    gh = MagicMock(return_value="")
    monkeypatch.setattr("fieldkit.issue.github._gh", gh)

    result = store.update_status("fieldkit-042", target)  # type: ignore[arg-type]

    assert result is not None
    calls = [call.args for call in gh.call_args_list]
    remove_index = next(i for i, call in enumerate(calls) if "--remove-label" in call and "status:fixed" in call)
    reopen_index = next(i for i, call in enumerate(calls) if "reopen" in call)
    assert remove_index < reopen_index


@pytest.mark.parametrize("operation", ["update", "fix", "note", "link"])
def test_irreversible_comment_failure_is_nonretryable_uncertainty(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store = GHIssueStore(REPO)
    issue = _issue("fixed" if operation == "fix" else "open")
    monkeypatch.setattr(store, "find", lambda _issue_id: issue)
    monkeypatch.setattr("fieldkit.issue.github._read_milestones", lambda *_args: [(1, "M1")])

    def fake_gh(*args: str, irreversible: bool = False) -> str:
        if "comment" in args:
            assert irreversible is True
            raise GitHubCreationUncertainError("may have been created")
        return ""

    monkeypatch.setattr("fieldkit.issue.github._gh", fake_gh)

    with pytest.raises(GitHubCreationUncertainError, match="may have been created"):
        if operation == "update":
            store.update_status("fieldkit-042", "open", note="note")
        elif operation == "fix":
            store.mark_fixed("fieldkit-042", note="note")
        elif operation == "note":
            store.add_note("fieldkit-042", "note")
        else:
            store.link_milestone("fieldkit-042", "M1", note="note")


@pytest.mark.parametrize(
    ("failure", "category", "exit_code"),
    [
        (GitHubRequestError("private provider payload"), "retryable", 1),
        (AuthError("private provider payload"), "authentication", 2),
        (GitHubDataError("private provider payload"), "data", 3),
    ],
)
def test_milestone_batch_retains_safe_per_issue_failure_category(
    failure: Exception,
    category: str,
    exit_code: int,
) -> None:
    issue = _issue()
    store = MagicMock()
    store.update_status.side_effect = failure
    plan = plan_milestone_sync([issue], "queued")

    outcomes = advance_milestone_candidates(store, plan, commit=None)

    assert outcomes[0].issue_id == issue.id
    assert outcomes[0].updated is None
    assert outcomes[0].failure == category
    assert "private provider payload" not in repr(outcomes[0])
    assert milestone_exit_code(outcomes) == exit_code


def test_milestone_batch_continues_after_one_candidate_fails() -> None:
    first = _issue()
    second = replace(first, id="fieldkit-043", gh_number=43)
    store = MagicMock()
    store.update_status.side_effect = [GitHubRequestError("failure"), second]
    plan = plan_milestone_sync([first, second], "queued")

    outcomes = advance_milestone_candidates(store, plan, commit=None)

    assert [outcome.failure for outcome in outcomes] == ["retryable", None]
    assert store.update_status.call_count == 2
