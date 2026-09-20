"""Tests for the fieldkit driver loop.

Covers:
- Work order resolution from issue body
- Branch name generation
- GitHub label transitions (mocked)
- run_driver happy path (dry-run)
- run_driver failure path (no issues)
- Spend summary query
- comment_on_issue / list_ready_issues
- _isolate_data_dir / _write_run_status
"""

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.driver.opencode import OpencodeOutcome

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# fieldkit.driver.github
# ---------------------------------------------------------------------------


def _make_issue(labels: list[str]) -> "AgentIssue":  # type: ignore[name-defined]  # noqa: F821
    from fieldkit.driver.github import AgentIssue

    return AgentIssue(number=1, title="Test", body="", labels=labels)


def test_attempt_count_zero_when_no_label() -> None:
    issue = _make_issue([])
    assert issue.attempt_count == 0


def test_attempt_count_reads_label() -> None:
    issue = _make_issue(["attempt:2", "agent-ready"])
    assert issue.attempt_count == 2


def test_attempt_count_ignores_malformed() -> None:
    issue = _make_issue(["attempt:xyz"])
    assert issue.attempt_count == 0


def test_get_pr_identity_requires_one_well_formed_open_pr() -> None:
    from fieldkit.driver.github import PullRequestIdentity, get_pr_identity

    pr_payload = json.dumps(
        [
            {
                "number": 9,
                "baseRefName": "main",
                "headRefName": "driver/issue-1-test",
                "headRefOid": "a" * 40,
                "headRepository": {"name": "repo"},
                "headRepositoryOwner": {"login": "example"},
            }
        ]
    )
    responses = [
        subprocess.CompletedProcess([], 0, pr_payload, ""),
        subprocess.CompletedProcess([], 0, "42\n", ""),
    ]
    with patch("fieldkit.driver.github.subprocess.run", side_effect=responses):
        result = get_pr_identity("example/repo", "driver/issue-1-test")

    assert result == PullRequestIdentity(9, "example/repo", 42, "main", "driver/issue-1-test", "a" * 40)


def test_get_pr_identity_propagates_authentication_failure() -> None:
    from fieldkit.driver.github import get_pr_identity
    from fieldkit.errors import AuthError

    failure = subprocess.CompletedProcess([], 1, "", "authentication required")
    with (
        patch("fieldkit.driver.github.subprocess.run", return_value=failure),
        pytest.raises(AuthError, match="authentication"),
    ):
        get_pr_identity("example/repo", "driver/issue-1-test")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [{"number": 1}, {"number": 2}],
        [
            {
                "number": 9,
                "baseRefName": "main",
                "headRefName": "driver/issue-1-test",
                "headRefOid": "a" * 40,
                "headRepository": {"name": "different"},
                "headRepositoryOwner": {"login": "example"},
            }
        ],
    ],
    ids=["missing", "ambiguous", "repository-mismatch"],
)
def test_get_pr_identity_rejects_missing_ambiguous_or_mismatched_results(payload: list[dict[str, object]]) -> None:
    from fieldkit.driver.github import GitHubLookupError, get_pr_identity

    response = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    with (
        patch("fieldkit.driver.github.subprocess.run", return_value=response),
        pytest.raises(GitHubLookupError),
    ):
        get_pr_identity("example/repo", "driver/issue-1-test")


def test_removes_ready_adds_failed() -> None:
    from fieldkit.driver.github import AgentIssue, transition_to_failed

    issue = AgentIssue(number=42, title="T", body="", labels=["agent-ready"])

    with (
        patch("fieldkit.driver.github.add_label") as mock_add,
        patch("fieldkit.driver.github.remove_label"),
    ):
        transition_to_failed("owner/repo", issue, attempt=1)

    mock_add.assert_any_call("owner/repo", 42, "agent-failed")
    mock_add.assert_any_call("owner/repo", 42, "attempt:1")


def test_escalates_to_blocked_at_max() -> None:
    from fieldkit.driver.github import MAX_ATTEMPTS, AgentIssue, transition_to_failed

    # Simulate issue that has already failed MAX_ATTEMPTS - 1 times
    labels = ["agent-ready", f"attempt:{MAX_ATTEMPTS - 1}"]
    issue = AgentIssue(number=7, title="T", body="", labels=labels)

    with (
        patch("fieldkit.driver.github.add_label") as mock_add,
        patch("fieldkit.driver.github.remove_label"),
    ):
        transition_to_failed("owner/repo", issue, attempt=MAX_ATTEMPTS)

    added = [c.args[2] for c in mock_add.call_args_list]
    assert "agent-blocked" in added
    assert "agent-failed" not in added


def test_transition_to_failed_keeps_local_authority_when_projection_fails() -> None:
    """A failed GitHub projection does not change the lifecycle decision."""
    from fieldkit.driver.github import AgentIssue, transition_to_failed

    issue = AgentIssue(number=9, title="T", body="", labels=["agent-ready"])

    def fake_add(repo: str, number: int, label: str) -> bool:
        return not label.startswith("attempt:")  # attempt write fails; others succeed

    with (
        patch("fieldkit.driver.github.add_label", side_effect=fake_add) as mock_add,
        patch("fieldkit.driver.github.remove_label"),
        patch("fieldkit.driver.github.comment_on_issue") as mock_comment,
    ):
        transition_to_failed("owner/repo", issue, attempt=1, error="boom")

    added = [c.args[2] for c in mock_add.call_args_list]
    assert "agent-blocked" not in added
    assert "agent-failed" in added
    assert "Driver failed" in mock_comment.call_args[0][2]


# ---------------------------------------------------------------------------
# fieldkit.driver.runner.resolve_prompt_source — work order resolution
# ---------------------------------------------------------------------------


def test_resolves_valid_work_order(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    wo_dir = tmp_path / "docs" / "work-orders"
    wo_dir.mkdir(parents=True)
    wo_file = wo_dir / "test-wo.md"
    wo_file.write_text("# Work Order")

    issue = AgentIssue(
        number=1,
        title="T",
        body="WorkOrder: docs/work-orders/test-wo.md",
        labels=[],
    )
    result = resolve_prompt_source(issue, tmp_path)
    assert result == wo_file


def test_resolves_backward_compat_brief_format(tmp_path: Path) -> None:
    """Backward compat: 'Brief: docs/briefs/...' still resolves."""
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    briefs_dir = tmp_path / "docs" / "briefs"
    briefs_dir.mkdir(parents=True)
    wo_file = briefs_dir / "old-format.md"
    wo_file.write_text("# Old format work order\n")
    issue = AgentIssue(
        number=99,
        title="backward compat",
        body="Brief: docs/briefs/old-format.md",
        labels=["agent-ready"],
    )
    result = resolve_prompt_source(issue, tmp_path)
    assert result == wo_file.resolve()


def test_returns_none_when_body_has_no_reference(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    issue = AgentIssue(number=1, title="T", body="No reference here.", labels=[])
    assert resolve_prompt_source(issue, tmp_path) is None


def test_bare_work_order_path_in_prose_does_not_match(tmp_path: Path) -> None:
    """A bare docs/work-orders/ path mentioned in prose must not fire without prefix."""
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    wo_dir = tmp_path / "docs" / "work-orders"
    wo_dir.mkdir(parents=True)
    (wo_dir / "old-wo.md").write_text("# Old")

    # Body mentions work order in prose, not as a reference line
    issue = AgentIssue(
        number=1,
        title="T",
        body="See docs/work-orders/old-wo.md for context.\nOpenSpec: openspec/changes/my-fix/",
        labels=[],
    )
    # No openspec dir — should return None, not accidentally resolve the work order
    (tmp_path / "openspec" / "changes").mkdir(parents=True)
    assert resolve_prompt_source(issue, tmp_path) is None


def test_returns_none_when_work_order_file_missing(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    issue = AgentIssue(
        number=1,
        title="T",
        body="WorkOrder: docs/work-orders/nonexistent.md",
        labels=[],
    )
    (tmp_path / "docs" / "work-orders").mkdir(parents=True)
    assert resolve_prompt_source(issue, tmp_path) is None


def test_rejects_path_traversal(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    # Craft a body where the regex could match a traversal path.
    # The regex only matches docs/work-orders/... so this must pass the regex,
    # then be rejected by the relative_to check.
    # We simulate by patching the match to return a traversal path.
    issue = AgentIssue(number=1, title="T", body="", labels=[])

    with patch("fieldkit.driver.runner._WORK_ORDER_RE") as mock_re:
        mock_match = MagicMock()
        mock_match.group.return_value = "docs/work-orders/../../etc/passwd"
        mock_re.search.return_value = mock_match
        (tmp_path / "docs" / "work-orders").mkdir(parents=True)
        result = resolve_prompt_source(issue, tmp_path)

    assert result is None


# ---------------------------------------------------------------------------
# fieldkit.driver.runner.resolve_prompt_source — OpenSpec and Speckit branches
# ---------------------------------------------------------------------------


def test_resolves_openspec_tasks_md(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    change_dir = tmp_path / "openspec" / "changes" / "my-change"
    change_dir.mkdir(parents=True)
    tasks = change_dir / "tasks.md"
    tasks.write_text("# Tasks")

    issue = AgentIssue(
        number=2,
        title="T",
        body="OpenSpec: openspec/changes/my-change/",
        labels=[],
    )
    assert resolve_prompt_source(issue, tmp_path) == tasks


def test_openspec_falls_back_to_proposal_when_no_tasks_md(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    change_dir = tmp_path / "openspec" / "changes" / "my-change"
    change_dir.mkdir(parents=True)
    proposal = change_dir / "proposal.md"
    proposal.write_text("# Proposal")

    issue = AgentIssue(
        number=2,
        title="T",
        body="OpenSpec: openspec/changes/my-change/",
        labels=[],
    )
    assert resolve_prompt_source(issue, tmp_path) == proposal


def test_openspec_resolves_via_origin_main_when_tasks_md_absent_on_disk(tmp_path: Path) -> None:
    """historic regression: OpenSpec sources self-heal when the shared checkout is stale."""
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    change_dir = tmp_path / "openspec" / "changes" / "my-change"
    change_dir.mkdir(parents=True)
    expected = change_dir / "tasks.md"
    issue = AgentIssue(
        number=7,
        title="T",
        body="OpenSpec: openspec/changes/my-change/",
        labels=[],
    )

    with patch("fieldkit.driver.runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="blob")
        result = resolve_prompt_source(issue, tmp_path)

    assert result == expected
    assert mock_run.call_args.args[0] == [
        "git",
        "cat-file",
        "-t",
        "origin/main:openspec/changes/my-change/tasks.md",
    ]


def test_openspec_uses_remote_fallback_when_primary_is_unavailable(tmp_path: Path) -> None:
    """historic regression: directory sources retain their fallback precedence remotely."""
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    change_dir = tmp_path / "openspec" / "changes" / "my-change"
    change_dir.mkdir(parents=True)
    expected = change_dir / "proposal.md"
    issue = AgentIssue(
        number=9,
        title="T",
        body="OpenSpec: openspec/changes/my-change/",
        labels=[],
    )

    with patch("fieldkit.driver.runner.subprocess.run") as mock_run:
        mock_run.side_effect = [subprocess.CalledProcessError(1, "git"), MagicMock(stdout="blob")]
        result = resolve_prompt_source(issue, tmp_path)

    assert result == expected
    assert [call.args[0][-1] for call in mock_run.call_args_list] == [
        "origin/main:openspec/changes/my-change/tasks.md",
        "origin/main:openspec/changes/my-change/proposal.md",
    ]


def test_openspec_returns_none_when_dir_missing(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    (tmp_path / "openspec" / "changes").mkdir(parents=True)
    issue = AgentIssue(
        number=2,
        title="T",
        body="OpenSpec: openspec/changes/nonexistent/",
        labels=[],
    )
    assert resolve_prompt_source(issue, tmp_path) is None


def test_resolves_speckit_tasks_md(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    spec_dir = tmp_path / "specs" / "039-test-infra"
    spec_dir.mkdir(parents=True)
    tasks = spec_dir / "tasks.md"
    tasks.write_text("# Tasks")

    issue = AgentIssue(
        number=3,
        title="T",
        body="Speckit: specs/039-test-infra/",
        labels=[],
    )
    assert resolve_prompt_source(issue, tmp_path) == tasks


def test_speckit_falls_back_to_spec_md_when_no_tasks_md(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    spec_dir = tmp_path / "specs" / "039-test-infra"
    spec_dir.mkdir(parents=True)
    spec_md = spec_dir / "spec.md"
    spec_md.write_text("# Spec")

    issue = AgentIssue(
        number=3,
        title="T",
        body="Speckit: specs/039-test-infra/",
        labels=[],
    )
    assert resolve_prompt_source(issue, tmp_path) == spec_md


def test_speckit_resolves_via_origin_main_when_tasks_md_absent_on_disk(tmp_path: Path) -> None:
    """historic regression: Speckit sources self-heal when the shared checkout is stale."""
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    spec_dir = tmp_path / "specs" / "039-test-infra"
    spec_dir.mkdir(parents=True)
    expected = spec_dir / "tasks.md"
    issue = AgentIssue(
        number=8,
        title="T",
        body="Speckit: specs/039-test-infra/",
        labels=[],
    )

    with patch("fieldkit.driver.runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="blob")
        result = resolve_prompt_source(issue, tmp_path)

    assert result == expected
    assert mock_run.call_args.args[0] == [
        "git",
        "cat-file",
        "-t",
        "origin/main:specs/039-test-infra/tasks.md",
    ]


def test_speckit_returns_none_when_dir_missing(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    (tmp_path / "specs").mkdir(parents=True)
    issue = AgentIssue(
        number=3,
        title="T",
        body="Speckit: specs/nonexistent/",
        labels=[],
    )
    assert resolve_prompt_source(issue, tmp_path) is None


def test_work_order_takes_priority_over_openspec(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    wo_dir = tmp_path / "docs" / "work-orders"
    wo_dir.mkdir(parents=True)
    wo_file = wo_dir / "foo.md"
    wo_file.write_text("# Work Order")

    change_dir = tmp_path / "openspec" / "changes" / "bar"
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text("# Tasks")

    issue = AgentIssue(
        number=4,
        title="T",
        body="WorkOrder: docs/work-orders/foo.md\nOpenSpec: openspec/changes/bar/",
        labels=[],
    )
    assert resolve_prompt_source(issue, tmp_path) == wo_file


def test_rejects_openspec_path_traversal(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    (tmp_path / "openspec" / "changes").mkdir(parents=True)
    issue = AgentIssue(number=5, title="T", body="", labels=[])

    with patch("fieldkit.driver.runner._OPENSPEC_RE") as mock_re:
        mock_match = MagicMock()
        mock_match.group.return_value = "openspec/changes/../../etc/"
        mock_re.search.return_value = mock_match
        # Work order RE must NOT match so we fall through to openspec branch
        with patch("fieldkit.driver.runner._WORK_ORDER_RE") as mock_wo_re:
            mock_wo_re.search.return_value = None
            result = resolve_prompt_source(issue, tmp_path)

    assert result is None


def test_rejects_speckit_path_traversal(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import resolve_prompt_source

    (tmp_path / "specs").mkdir(parents=True)
    issue = AgentIssue(number=6, title="T", body="", labels=[])

    with patch("fieldkit.driver.runner._SPECKIT_RE") as mock_re:
        mock_match = MagicMock()
        mock_match.group.return_value = "specs/../../etc/"
        mock_re.search.return_value = mock_match
        with patch("fieldkit.driver.runner._WORK_ORDER_RE") as mock_wo_re:
            mock_wo_re.search.return_value = None
            with patch("fieldkit.driver.runner._OPENSPEC_RE") as mock_openspec_re:
                mock_openspec_re.search.return_value = None
                result = resolve_prompt_source(issue, tmp_path)

    assert result is None


# ---------------------------------------------------------------------------
# fieldkit.driver.runner.make_branch_name
# ---------------------------------------------------------------------------


def test_format() -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import make_branch_name

    issue = AgentIssue(number=42, title="Fix exit code leaves", body="", labels=[])
    branch = make_branch_name(issue)
    assert branch.startswith("driver/issue-42-")
    assert "fix-exit-code-leaves" in branch


def test_truncates_long_title() -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import make_branch_name

    title = "A" * 200
    issue = AgentIssue(number=1, title=title, body="", labels=[])
    branch = make_branch_name(issue)
    # slug is capped at 50 chars
    assert len(branch) <= len("driver/issue-1-") + 50


# ---------------------------------------------------------------------------
# fieldkit.driver.runner.run_driver (dry-run)
# ---------------------------------------------------------------------------


def test_skipped_when_no_issues(tmp_path: Path) -> None:
    from fieldkit.driver.runner import run_driver

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=True)

    assert result.outcome == "skipped"


def test_dry_run_skips_labels_and_git(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import run_driver

    wo_dir = tmp_path / "docs" / "work-orders"
    wo_dir.mkdir(parents=True)
    (wo_dir / "test.md").write_text("# Work Order")

    issue = AgentIssue(
        number=99,
        title="Test issue",
        body="WorkOrder: docs/work-orders/test.md",
        labels=["agent-ready"],
    )

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree") as mock_worktree,
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")) as mock_oc,
        patch("fieldkit.driver.runner.transition_to_failed") as mock_fail,
        patch("fieldkit.driver.runner.transition_to_succeeded") as mock_ok,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=True)

    assert result.outcome == "dry-run"
    assert result.issue_number == 99
    mock_worktree.assert_not_called()
    mock_fail.assert_not_called()
    mock_ok.assert_not_called()
    mock_oc.assert_called_once()


def test_skipped_when_work_order_missing(tmp_path: Path) -> None:
    # An issue with no work order reference is skipped (agent-ready removed),
    # not failed. The attempt counter is NOT incremented.
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import run_driver

    issue = AgentIssue(
        number=5,
        title="Bad issue",
        body="No work order reference.",
        labels=["agent-ready"],
    )
    (tmp_path / "docs" / "work-orders").mkdir(parents=True)

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.transition_to_failed") as mock_fail,
        patch("fieldkit.driver.runner.remove_label") as mock_remove,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=True)

    assert result.outcome == "skipped"
    mock_fail.assert_not_called()  # must not burn an attempt
    mock_remove.assert_not_called()  # dry_run=True skips label ops


def test_skips_unlinked_then_executes_next(tmp_path: Path) -> None:
    # First issue has no work order — driver strips agent-ready and moves on.
    # Second issue has a valid work order — driver executes it and returns ok.
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import run_driver

    wo_dir = tmp_path / "docs" / "work-orders"
    wo_dir.mkdir(parents=True)
    (wo_dir / "good.md").write_text("# Work Order")

    bad_issue = AgentIssue(
        number=10,
        title="No work order",
        body="No work order reference.",
        labels=["agent-ready"],
    )
    good_issue = AgentIssue(
        number=11,
        title="Has work order",
        body="WorkOrder: docs/work-orders/good.md",
        labels=["agent-ready"],
    )

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[bad_issue, good_issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.remove_label") as mock_remove,
        patch("fieldkit.driver.runner.transition_to_failed") as mock_fail,
        patch("fieldkit.driver.runner.transition_to_succeeded") as mock_ok,
        patch("fieldkit.driver.runner.comment_on_issue"),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "wt"),
        patch("fieldkit.driver.runner.remove_worktree") as mock_rm_wt,
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=MagicMock()),
        patch("fieldkit.driver.runner.verify_submitted_head", return_value=MagicMock(passed=True)),
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    # Worktree is always torn down after the run
    mock_rm_wt.assert_called_once()

    # Bad issue: agent-ready stripped, no attempt burned
    mock_remove.assert_called_once_with("owner/repo", 10, "agent-ready")
    mock_fail.assert_not_called()
    # Good issue: executed successfully
    assert result.outcome == "ok"
    assert result.issue_number == 11
    mock_ok.assert_called_once()


# ---------------------------------------------------------------------------
# fieldkit.driver.runner.run_driver — flock concurrency guard (implementation change)
# ---------------------------------------------------------------------------


def test_skipped_when_lock_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent run_driver() holding the lock causes this run to skip, not race."""
    import fcntl

    from fieldkit.driver.runner import run_driver

    monkeypatch.setattr("fieldkit.driver.runner.get_fieldkit_data", lambda: tmp_path)
    lock_path = tmp_path / "driver" / "driver.lock"
    lock_path.parent.mkdir(parents=True)
    held = lock_path.open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_driver(repo_root=tmp_path, dry_run=True)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert result.outcome == "skipped"
    assert "lock" in result.error.lower()


def test_acquires_and_releases_lock_across_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sequential run_driver() calls each succeed — the lock does not leak."""
    from fieldkit.driver.runner import run_driver

    monkeypatch.setattr("fieldkit.driver.runner.get_fieldkit_data", lambda: tmp_path)

    with (
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[]),
    ):
        first = run_driver(repo_root=tmp_path, dry_run=True)
        second = run_driver(repo_root=tmp_path, dry_run=True)

    assert first.outcome == "skipped"
    assert second.outcome == "skipped"
    assert "lock" not in first.error.lower()
    assert "lock" not in second.error.lower()


# ---------------------------------------------------------------------------
# fieldkit.driver.spend
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, rows: list[tuple]) -> Path:
    db = tmp_path / "llm-calls.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("""
            CREATE TABLE llm_calls (
                id TEXT PRIMARY KEY, ts TEXT, skill TEXT, account TEXT,
                model TEXT, input_tokens INTEGER, output_tokens INTEGER,
                latency_ms INTEGER, cost_usd REAL, error TEXT, prompt_hash TEXT
            )
        """)
        conn.executemany("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return db


def test_returns_empty_when_db_missing(tmp_path: Path) -> None:
    from fieldkit.driver.spend import get_spend_summary

    with patch("fieldkit.driver.spend.get_read_db_path", return_value=tmp_path / "missing.db"):
        result = get_spend_summary(42)
    assert result == ""


def test_returns_empty_when_no_matching_rows(tmp_path: Path) -> None:
    from fieldkit.driver.spend import get_spend_summary

    db = _make_db(tmp_path, [])
    with patch("fieldkit.driver.spend.get_read_db_path", return_value=db):
        result = get_spend_summary(42)
    assert result == ""


def test_returns_spend_note_for_matching_rows(tmp_path: Path) -> None:
    from fieldkit.driver.spend import get_spend_summary

    db = _make_db(
        tmp_path,
        [
            ("id1", "ts", "driver-loop", "driver-issue-7", "m", 100, 50, 500, 0.05, None, None),
            ("id2", "ts", "driver-loop", "driver-issue-7", "m", 200, 80, 600, 0.07, None, None),
        ],
    )
    with patch("fieldkit.driver.spend.get_read_db_path", return_value=db):
        result = get_spend_summary(7)

    assert "$0.12" in result
    assert "300" in result  # input tokens
    assert "130" in result  # output tokens
    assert "2 calls" in result


def test_ignores_rows_for_other_issues(tmp_path: Path) -> None:
    from fieldkit.driver.spend import get_spend_summary

    db = _make_db(
        tmp_path,
        [
            ("id1", "ts", "x", "driver-issue-99", "m", 100, 50, 500, 0.05, None, None),
        ],
    )
    with patch("fieldkit.driver.spend.get_read_db_path", return_value=db):
        result = get_spend_summary(7)  # different issue
    assert result == ""


def test_get_daily_spend_total_sums_todays_driver_issue_rows(tmp_path: Path) -> None:
    from fieldkit.driver.spend import get_daily_spend_total

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    db = _make_db(
        tmp_path,
        [
            ("id1", f"{today}T10:00:00", "driver-loop", "driver-issue-7", "m", 100, 50, 500, 0.05, None, None),
            ("id2", f"{today}T11:00:00", "driver-loop", "driver-issue-9", "m", 200, 80, 600, 0.07, None, None),
            ("id3", f"{today}T12:00:00", "driver-loop", "not-driver-issue", "m", 100, 50, 500, 1.00, None, None),
            ("id4", "2020-01-01T00:00:00", "driver-loop", "driver-issue-7", "m", 100, 50, 500, 3.00, None, None),
        ],
    )
    with patch("fieldkit.driver.spend.get_db_path", return_value=db):
        total = get_daily_spend_total()

    assert total == pytest.approx(0.12)


def test_get_daily_spend_total_returns_zero_when_db_missing(tmp_path: Path) -> None:
    from fieldkit.driver.spend import get_daily_spend_total

    with patch("fieldkit.driver.spend.get_db_path", return_value=tmp_path / "missing.db"):
        total = get_daily_spend_total()

    assert total == 0.0


def test_spend_resolves_same_db_path_as_llm_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Driver spend and LLM logging resolve one shared database path (implementation change)."""
    import fieldkit.driver.spend as spend_module
    from fieldkit.llm.log import get_db_path as log_get_db_path

    custom_path = tmp_path / "custom-llm-calls.db"
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(custom_path))

    resolved_path = spend_module.get_db_path()

    assert resolved_path == custom_path
    assert resolved_path == log_get_db_path()
    assert spend_module.get_db_path is log_get_db_path


def test_run_driver_skips_when_daily_spend_cap_reached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.driver.runner import run_driver

    monkeypatch.setenv("FIELDKIT_DRIVER_SPEND_CAP", "10.0")

    with (
        patch("fieldkit.driver.spend.get_daily_spend_total", return_value=12.0),
        patch("fieldkit.driver.runner.list_ready_issues") as mock_list,
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    mock_list.assert_not_called()


def test_run_driver_proceeds_when_cap_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.driver.runner import run_driver

    monkeypatch.delenv("FIELDKIT_DRIVER_SPEND_CAP", raising=False)

    with (
        patch("fieldkit.driver.spend.get_daily_spend_total", return_value=999.0),
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[]) as mock_list,
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    # Falls through to the existing "no ready issues" skip, not the cap check.
    assert result.outcome == "skipped"
    mock_list.assert_called_once()


def test_run_driver_dry_run_ignores_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.driver.runner import run_driver

    monkeypatch.setenv("FIELDKIT_DRIVER_SPEND_CAP", "0.01")

    with (
        patch("fieldkit.driver.spend.get_daily_spend_total", return_value=999.0),
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[]) as mock_list,
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=True)

    # dry_run bypasses the cap check entirely; falls through to the
    # "no ready issues" skip.
    assert result.outcome == "skipped"
    mock_list.assert_called_once()


# ---------------------------------------------------------------------------
# fieldkit.driver.runner.create_worktree / remove_worktree (isolation)
# ---------------------------------------------------------------------------


def test_create_worktree_fetches_then_adds_off_origin_main(tmp_path: Path) -> None:
    from fieldkit.driver.runner import create_worktree

    with (
        patch("fieldkit.driver.runner._worktrees_root", return_value=tmp_path / "wt"),
        patch("fieldkit.driver.runner.subprocess") as mock_sub,
    ):
        mock_sub.run.return_value = MagicMock(returncode=0, stderr="")
        result = create_worktree("driver/issue-99-test", tmp_path, 99)

    assert result is not None
    assert result.parent == tmp_path / "wt"
    assert result.name.startswith("issue-99-")
    # First call fetches origin/main; second adds the worktree with -B off origin/main.
    assert mock_sub.run.call_count == 2
    fetch_args = mock_sub.run.call_args_list[0][0][0]
    assert fetch_args[:2] == ["git", "fetch"] and "main" in fetch_args
    add_args = mock_sub.run.call_args_list[1][0][0]
    assert add_args[:3] == ["git", "worktree", "add"]
    assert "-B" in add_args and "driver/issue-99-test" in add_args
    assert add_args[-1] == "origin/main"


def test_create_worktree_returns_none_when_fetch_fails(tmp_path: Path) -> None:
    from fieldkit.driver.runner import create_worktree

    with (
        patch("fieldkit.driver.runner._worktrees_root", return_value=tmp_path / "wt"),
        patch("fieldkit.driver.runner.subprocess") as mock_sub,
    ):
        mock_sub.CalledProcessError = subprocess.CalledProcessError
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        mock_sub.run.side_effect = subprocess.CalledProcessError(1, "git fetch", stderr="boom")
        result = create_worktree("driver/issue-99-test", tmp_path, 99)

    assert result is None
    # Never attempted the worktree add after the fetch failed.
    assert mock_sub.run.call_count == 1


def test_create_worktree_cleans_up_when_add_fails(tmp_path: Path) -> None:
    from fieldkit.driver.runner import create_worktree

    with (
        patch("fieldkit.driver.runner._worktrees_root", return_value=tmp_path / "wt"),
        patch("fieldkit.driver.runner.remove_worktree") as mock_rm,
        patch("fieldkit.driver.runner.subprocess") as mock_sub,
    ):
        mock_sub.CalledProcessError = subprocess.CalledProcessError
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        # fetch OK, then worktree add fails.
        mock_sub.run.side_effect = [
            MagicMock(returncode=0, stderr=""),
            subprocess.CalledProcessError(1, "git worktree add", stderr="exists"),
        ]
        result = create_worktree("driver/issue-99-test", tmp_path, 99)

    assert result is None
    mock_rm.assert_called_once()


def test_remove_worktree_removes_and_prunes(tmp_path: Path) -> None:
    from fieldkit.driver.runner import remove_worktree

    with patch("fieldkit.driver.runner.subprocess") as mock_sub:
        mock_sub.run.return_value = MagicMock(returncode=0, stderr="")
        remove_worktree(tmp_path / "wt" / "issue-99", tmp_path)

    assert mock_sub.run.call_count == 2
    remove_args = mock_sub.run.call_args_list[0][0][0]
    assert remove_args[:3] == ["git", "worktree", "remove"] and "--force" in remove_args
    prune_args = mock_sub.run.call_args_list[1][0][0]
    assert prune_args == ["git", "worktree", "prune"]


# ---------------------------------------------------------------------------
# fieldkit.driver.github — comment_on_issue / list_ready_issues
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_comment_on_issue_calls_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """comment_on_issue posts a comment via gh CLI."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from fieldkit.driver.github import comment_on_issue

    result = comment_on_issue("owner/repo", 42, "test body")
    assert result is True
    assert len(calls) == 1
    assert "comment" in calls[0]
    assert "42" in calls[0]


@pytest.mark.unit
def test_comment_on_issue_returns_false_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """comment_on_issue returns False when gh CLI fails."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd, "", "gh: error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from fieldkit.driver.github import comment_on_issue

    result = comment_on_issue("owner/repo", 42, "test body")
    assert result is False


def test_last_failure_comment_returns_latest_driver_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry prompt receives the most recent prior driver failure comment."""

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["gh", "issue", "view"]
        assert cmd[3] == "42"
        return subprocess.CompletedProcess(cmd, 0, "⚠️ **Driver failed**\n\nprevious failure\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from fieldkit.driver.github import last_failure_comment

    result = last_failure_comment("owner/repo", 42)

    assert result == "⚠️ **Driver failed**\n\nprevious failure"


@pytest.mark.unit
def test_list_ready_issues_unions_fresh_and_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ready list passes failed issues to the local retry authority.

    Locks the retry re-queue fix (Reading A): a cleanly-failed issue — one whose
    ``agent-ready`` was stripped and ``agent-failed`` set — must be re-selected
    even when GitHub's label says it is at the cap. A ``agent-blocked`` issue
    stays excluded because it has been explicitly disabled on GitHub.
    """
    ready = [
        {"number": 1, "title": "clean", "body": "", "labels": [{"name": "agent-ready"}]},
        {
            "number": 2,
            "title": "partial",
            "body": "",
            "labels": [{"name": "agent-ready"}, {"name": "agent-failed"}, {"name": "attempt:1"}],
        },
    ]
    failed = [
        # #2 is the partial-transition case (both labels) — now runnable via retry.
        {
            "number": 2,
            "title": "partial",
            "body": "",
            "labels": [{"name": "agent-ready"}, {"name": "agent-failed"}, {"name": "attempt:1"}],
        },
        # #3 cleanly failed, under the cap → retried.
        {
            "number": 3,
            "title": "cleanly-failed",
            "body": "",
            "labels": [{"name": "agent-failed"}, {"name": "attempt:1"}],
        },
        # #4 blocked → never retried.
        {
            "number": 4,
            "title": "blocked",
            "body": "",
            "labels": [{"name": "agent-failed"}, {"name": "agent-blocked"}, {"name": "attempt:3"}],
        },
        # #5 reaches local eligibility for a durable ledger decision.
        {"number": 5, "title": "at-cap", "body": "", "labels": [{"name": "agent-failed"}, {"name": "attempt:3"}]},
    ]

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        label = cmd[cmd.index("--label") + 1]
        payload = ready if label == "agent-ready" else failed
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from fieldkit.driver.github import list_ready_issues

    issues = list_ready_issues("owner/repo")
    assert [i.number for i in issues] == [1, 2, 3, 5]


# ---------------------------------------------------------------------------
# fieldkit.driver.runner — _isolate_data_dir / _write_run_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_isolate_data_dir_creates_directory(tmp_path: Path) -> None:
    """_isolate_data_dir creates .fieldkit-data inside the worktree."""
    from fieldkit.driver.runner import _isolate_data_dir

    result = _isolate_data_dir(tmp_path)
    assert result == tmp_path / ".fieldkit-data"
    assert result.is_dir()


@pytest.mark.unit
def test_write_run_status_preserves_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_write_run_status accumulates entries across writes (PM-01 fix)."""
    from fieldkit.driver.runner import RunResult, _write_run_status

    monkeypatch.setattr("fieldkit.driver.runner.get_fieldkit_data", lambda: tmp_path)

    result1 = RunResult(
        issue_number=1,
        issue_title="first",
        outcome="ok",
        branch="b1",
        elapsed_seconds=1.0,
        spend_note="",
        error="",
    )
    result2 = RunResult(
        issue_number=2,
        issue_title="second",
        outcome="failed",
        branch="b2",
        elapsed_seconds=2.0,
        spend_note="",
        error="oops",
    )
    _write_run_status(result1)
    _write_run_status(result2)

    status_file = tmp_path / "logs" / "driver" / "driver-run-status.json"
    data = json.loads(status_file.read_text())
    assert len(data["runs"]) == 2
    assert data["runs"][0]["issue_number"] == 1
    assert data["runs"][1]["issue_number"] == 2


# ---------------------------------------------------------------------------
# Regression locks — driver retry re-queue + WO-not-found policy + failure honesty
#
# Bug 1: retry re-queue — an agent-failed issue under the attempt cap is
#        re-selected by list_ready_issues and executed by run_driver.
# Bug 2: WO-not-found policy A — a referenced-but-unresolvable work order keeps
#        agent-ready; a reference-less issue loses it.
# Bug 3: OpencodeOutcome failure-mode distinction — timeout vs non-zero exit vs
#        not-pushed are separable in .reason, and the reason is threaded through.
# ---------------------------------------------------------------------------


def test_run_driver_reexecutes_retryable_failed_issue(tmp_path: Path) -> None:
    """Bug 1 (end-to-end): an ``agent-failed`` issue with ``attempt:1`` and a
    resolvable work order is re-selected through the *real* ``list_ready_issues``
    union and executed — ``run_opencode`` IS called for it on the next tick.

    Drives the real ``list_ready_issues`` via a mocked ``gh`` rather than stubbing
    it, so the test genuinely exercises the retry re-queue source the fix added:
    remove that source and ``run_opencode`` goes uncalled.
    """
    from fieldkit.driver.runner import run_driver

    wo_dir = tmp_path / "docs" / "work-orders"
    wo_dir.mkdir(parents=True)
    (wo_dir / "wo-7.md").write_text("# Work Order\n")

    failed_payload = [
        {
            "number": 7,
            "title": "retry me",
            "body": "WorkOrder: docs/work-orders/wo-7.md",
            "labels": [{"name": "agent-failed"}, {"name": "attempt:1"}],
        }
    ]

    def fake_gh(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        label = cmd[cmd.index("--label") + 1]
        payload = [] if label == "agent-ready" else failed_payload
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    with (
        patch("fieldkit.driver.github.subprocess.run", side_effect=fake_gh),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner._fetch_main"),
        patch("fieldkit.driver.runner.last_failure_comment", return_value=""),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", side_effect=lambda b, r, n: tmp_path / f"wt-{n}"),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=MagicMock()),
        patch("fieldkit.driver.runner.verify_submitted_head", return_value=MagicMock(passed=True)),
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")) as mock_oc,
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.transition_to_succeeded"),
        patch("fieldkit.driver.runner.comment_on_issue"),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "ok"
    assert result.issue_number == 7
    mock_oc.assert_called_once()
    assert mock_oc.call_args[0][2] == 7  # issue.number threaded into run_opencode


def test_run_driver_caps_retries_when_github_mutations_fail(tmp_path: Path) -> None:
    """historic regression: failed label/comment writes cannot cause a fourth OpenCode run."""
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import run_driver

    work_order = tmp_path / "docs" / "work-orders" / "retry.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text("# Work Order\n", encoding="utf-8")
    issue = AgentIssue(42, "retry", "WorkOrder: docs/work-orders/retry.md", ["agent-failed"])

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner._fetch_main"),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "worktree"),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=MagicMock()),
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch(
            "fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="failed", reason="boom")
        ) as mock_oc,
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.github.add_label", return_value=False),
        patch("fieldkit.driver.github.remove_label", return_value=False),
        patch("fieldkit.driver.github.comment_on_issue", return_value=False),
    ):
        results = [run_driver(repo_root=tmp_path) for _ in range(4)]

    assert [result.outcome for result in results] == ["failed", "failed", "failed", "skipped"]
    assert mock_oc.call_count == 3


def test_run_driver_rate_limit_exhaustion_blocks_fourth_run_and_explains_reset(tmp_path: Path) -> None:
    """historic regression: rate limiting consumes the retry budget and explains recovery."""
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import run_driver

    work_order = tmp_path / "docs" / "work-orders" / "retry.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text("# Work Order\n", encoding="utf-8")
    issue = AgentIssue(42, "retry", "WorkOrder: docs/work-orders/retry.md", ["agent-failed"])

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner._fetch_main"),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "worktree"),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=MagicMock()),
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch(
            "fieldkit.driver.runner.run_opencode",
            return_value=OpencodeOutcome(status="rate_limited", reason="provider quota exhausted"),
        ) as mock_oc,
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.comment_once") as mock_comment,
    ):
        results = [run_driver(repo_root=tmp_path) for _ in range(4)]

    assert [result.outcome for result in results] == ["skipped", "skipped", "skipped", "skipped"]
    assert mock_oc.call_count == 3
    message = mock_comment.call_args.args[3]
    assert "provider quota exhausted" in message
    assert "local reserved attempt is charged" in message
    assert "fieldkit driver retry status" in message
    assert "fieldkit driver retry reset" in message


def test_run_driver_skips_before_worktree_when_reservation_fails(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.retry_state import RetryDecision
    from fieldkit.driver.runner import run_driver

    work_order = tmp_path / "docs" / "work-orders" / "retry.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text("# Work Order\n", encoding="utf-8")
    issue = AgentIssue(42, "retry", "WorkOrder: docs/work-orders/retry.md", ["agent-ready"])
    denied = RetryDecision(False, "owner/repo#42", None, 0, None, "could not persist retry reservation")

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner._fetch_main"),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.reserve_attempt", return_value=denied),
        patch("fieldkit.driver.runner.create_worktree") as mock_worktree,
        patch("fieldkit.driver.runner.run_opencode") as mock_oc,
    ):
        result = run_driver(repo_root=tmp_path)

    assert result.outcome == "skipped"
    assert "could not persist" in result.error
    mock_worktree.assert_not_called()
    mock_oc.assert_not_called()


@pytest.mark.parametrize("finalization_allowed", [True, False])
def test_run_driver_finalizes_worktree_creation_failure(tmp_path: Path, finalization_allowed: bool) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.retry_state import RetryDecision
    from fieldkit.driver.runner import run_driver

    work_order = tmp_path / "docs" / "work-orders" / "retry.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text("# Work Order\n", encoding="utf-8")
    issue = AgentIssue(42, "retry", "WorkOrder: docs/work-orders/retry.md", ["agent-ready"])
    finalized = RetryDecision(finalization_allowed, "owner/repo#42", "retryable", 1, 1, "finalization result")

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner._fetch_main"),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", return_value=None),
        patch("fieldkit.driver.runner.run_opencode") as mock_oc,
        patch("fieldkit.driver.runner.complete_attempt", return_value=finalized) as mock_finalize,
        patch("fieldkit.driver.runner.transition_to_failed") as mock_transition,
    ):
        result = run_driver(repo_root=tmp_path)

    assert result.outcome == "failed"
    assert result.error == f"Could not create worktree for {result.branch}"
    mock_finalize.assert_called_once_with("owner/repo", 42, succeeded=False, outcome=result.error, data_root=tmp_path)
    mock_oc.assert_not_called()
    if finalization_allowed:
        mock_transition.assert_called_once_with("owner/repo", issue, attempt=1, error=result.error)
    else:
        mock_transition.assert_not_called()


@pytest.mark.parametrize("oc_status", ["ok", "failed"])
def test_run_driver_does_not_project_outcome_when_finalization_fails(
    tmp_path: Path, oc_status: Literal["ok", "failed"]
) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.retry_state import RetryDecision
    from fieldkit.driver.runner import run_driver

    work_order = tmp_path / "docs" / "work-orders" / "success.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text("# Work Order\n", encoding="utf-8")
    issue = AgentIssue(42, "success", "WorkOrder: docs/work-orders/success.md", ["agent-ready"])
    failed_finalization = RetryDecision(False, "owner/repo#42", None, 0, None, "could not finalize retry state")

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner._fetch_main"),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "worktree"),
        patch("fieldkit.driver.runner.remove_worktree") as mock_remove,
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=MagicMock()),
        patch("fieldkit.driver.runner.verify_submitted_head", return_value=MagicMock(passed=True)),
        patch("fieldkit.driver.runner.remove_snapshot", return_value=True),
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status=oc_status)),
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.complete_attempt", return_value=failed_finalization),
        patch("fieldkit.driver.runner.transition_to_succeeded") as mock_transition,
        patch("fieldkit.driver.runner.transition_to_failed") as mock_failed,
        patch("fieldkit.driver.runner.comment_on_issue") as mock_comment,
    ):
        result = run_driver(repo_root=tmp_path)

    assert result.outcome == "failed"
    assert "could not finalize" in result.error
    mock_transition.assert_not_called()
    mock_failed.assert_not_called()
    mock_comment.assert_not_called()
    mock_remove.assert_called_once_with(tmp_path / "worktree", tmp_path)


def test_run_driver_does_not_comment_on_rate_limit_when_finalization_fails(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.retry_state import RetryDecision
    from fieldkit.driver.runner import run_driver

    work_order = tmp_path / "docs" / "work-orders" / "rate-limit.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text("# Work Order\n", encoding="utf-8")
    issue = AgentIssue(42, "rate limit", "WorkOrder: docs/work-orders/rate-limit.md", ["agent-ready"])
    failed_finalization = RetryDecision(False, "owner/repo#42", None, 0, None, "could not finalize retry state")

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner._fetch_main"),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "worktree"),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=MagicMock()),
        patch("fieldkit.driver.runner.remove_snapshot", return_value=True),
        patch(
            "fieldkit.driver.runner.run_opencode",
            return_value=OpencodeOutcome(status="rate_limited", reason="provider quota exhausted"),
        ),
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.complete_attempt", return_value=failed_finalization),
        patch("fieldkit.driver.runner.comment_once") as mock_comment,
    ):
        result = run_driver(repo_root=tmp_path)

    assert result.outcome == "failed"
    assert "could not finalize" in result.error
    mock_comment.assert_not_called()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("WorkOrder: docs/work-orders/foo.md", True),
        ("Brief: docs/briefs/foo.md", True),
        ("OpenSpec: openspec/changes/my-change/", True),
        ("Speckit: specs/039-thing/", True),
        ("Just prose describing a bug, with no reference.", False),
        ("See docs/work-orders/foo.md for context.", False),  # bare path, no keyword prefix
    ],
)
def test_has_prompt_reference_detects_references(body: str, expected: bool) -> None:
    """Bug 2: True for a WorkOrder/OpenSpec/Speckit body, False for prose."""
    from fieldkit.driver.runner import _has_prompt_reference

    assert _has_prompt_reference(body) is expected


def test_exists_on_main_or_disk_true_when_on_disk(tmp_path: Path) -> None:
    """Bug 2: a work order present in the working tree resolves True."""
    from fieldkit.driver.runner import _exists_on_main_or_disk

    wo = tmp_path / "docs" / "work-orders" / "wo.md"
    wo.parent.mkdir(parents=True)
    wo.write_text("# Work Order\n")

    assert _exists_on_main_or_disk(tmp_path, wo) is True


def test_exists_on_main_or_disk_true_when_blob_on_origin_main(tmp_path: Path) -> None:
    """An origin/main blob resolves before it reaches the shared checkout."""
    from fieldkit.driver.runner import _exists_on_main_or_disk

    work_order = tmp_path / "docs" / "work-orders" / "wo.md"

    with patch("fieldkit.driver.runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="blob")
        result = _exists_on_main_or_disk(tmp_path, work_order)

    assert result is True
    assert mock_run.call_args.args[0][:3] == ["git", "cat-file", "-t"]


def test_exists_on_main_or_disk_rejects_tree_on_origin_main(tmp_path: Path) -> None:
    """A remote directory is not a usable prompt file."""
    from fieldkit.driver.runner import _exists_on_main_or_disk

    candidate = tmp_path / "openspec" / "changes" / "change" / "tasks.md"

    with patch("fieldkit.driver.runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="tree")
        result = _exists_on_main_or_disk(tmp_path, candidate)

    assert result is False


def test_exists_on_main_or_disk_false_when_absent_everywhere(tmp_path: Path) -> None:
    """Bug 2: absent on disk and ``git cat-file`` failing (CalledProcessError) → False."""
    from fieldkit.driver.runner import _exists_on_main_or_disk

    wo = tmp_path / "docs" / "work-orders" / "wo.md"

    with patch(
        "fieldkit.driver.runner.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["git", "cat-file", "-e"]),
    ):
        result = _exists_on_main_or_disk(tmp_path, wo)

    assert result is False


# ── implementation note: the spend cap fails closed ──────────────────────────────────────


def test_get_daily_spend_total_returns_none_when_query_fails(tmp_path: Path) -> None:
    """A cost guard must not read an unreadable database as $0.

    0.0 means "nothing spent"; None means "cannot see what was spent". Conflating them
    disables the cap in exactly the circumstances it exists for — a locked or corrupted
    DB during a run that is burning money.
    """
    from fieldkit.driver.spend import get_daily_spend_total

    db = tmp_path / "llm-calls.db"
    db.write_text("this is not a sqlite database", encoding="utf-8")

    with patch("fieldkit.driver.spend.get_db_path", return_value=db):
        total = get_daily_spend_total()

    assert total is None, "an unreadable DB must be reported as unknown, not as zero spend"


def test_get_daily_developer_spend_total_reads_openchamber_sessions(tmp_path: Path) -> None:
    """Admission cost control measures the OpenChamber sessions it actually gates."""
    from fieldkit.driver.spend import get_daily_developer_spend_total

    db = tmp_path / "opencode.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE project (id TEXT, worktree TEXT)")
        conn.execute("CREATE TABLE session (project_id TEXT, time_created INTEGER, cost REAL, model TEXT)")
        conn.execute("INSERT INTO project VALUES ('fieldkit', '/repo/fieldkit-cli')")
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        conn.executemany(
            "INSERT INTO session VALUES (?, ?, ?, ?)",
            [
                ("fieldkit", now_ms, 1.25, '{"providerID":"openai","id":"gpt-5.6-terra"}'),
                ("fieldkit", now_ms, 2.50, '{"providerID":"openai","id":"gpt-5.6-terra"}'),
                ("other", now_ms, 99.0, '{"providerID":"openai","id":"gpt-5.6-terra"}'),
                ("fieldkit", now_ms, 99.0, '{"providerID":"anthropic","id":"claude-opus-5"}'),
                ("fieldkit", now_ms, 99.0, '{"providerID":"openai","id":"gpt-5.6-sol"}'),
            ],
        )

    with (
        patch("fieldkit.driver.spend._get_opencode_db_path", return_value=db),
        patch("fieldkit.driver.spend.Path.cwd", return_value=Path("/repo/fieldkit-cli")),
    ):
        total = get_daily_developer_spend_total()

    assert total == 3.75


def test_get_daily_developer_spend_total_returns_zero_without_a_current_project_session(tmp_path: Path) -> None:
    from fieldkit.driver.spend import get_daily_developer_spend_total

    db = tmp_path / "opencode.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE project (id TEXT, worktree TEXT)")
        conn.execute("CREATE TABLE session (project_id TEXT, time_created INTEGER, cost REAL, model TEXT)")
        conn.execute("INSERT INTO project VALUES ('fieldkit', '/repo/fieldkit-cli')")

    with (
        patch("fieldkit.driver.spend._get_opencode_db_path", return_value=db),
        patch("fieldkit.driver.spend.Path.cwd", return_value=Path("/repo/fieldkit-cli")),
    ):
        total = get_daily_developer_spend_total()

    assert total == 0.0


def test_run_driver_fails_closed_when_spend_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap set + spend unknowable => run nothing, mirroring the busy-set posture."""
    from fieldkit.driver.runner import run_driver

    monkeypatch.setenv("FIELDKIT_DRIVER_SPEND_CAP", "10.0")

    with (
        patch("fieldkit.driver.spend.get_daily_spend_total", return_value=None),
        patch("fieldkit.driver.runner.list_ready_issues") as mock_list,
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    assert "fail closed" in result.error.lower()
    mock_list.assert_not_called()


def test_run_driver_fails_closed_on_unparseable_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd cap is a misconfiguration, not permission to spend without one.

    Ignoring it would mean `FIELDKIT_DRIVER_SPEND_CAP=5O` (letter O) silently runs
    uncapped — the operator believes they are protected and is not.
    """
    from fieldkit.driver.runner import run_driver

    monkeypatch.setenv("FIELDKIT_DRIVER_SPEND_CAP", "5O")

    with (
        patch("fieldkit.driver.runner.list_ready_issues") as mock_list,
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    assert "misconfigured" in result.error.lower()
    mock_list.assert_not_called()


def test_execute_one_orders_snapshot_agent_verification_and_success(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import _execute_one

    events: list[str] = []
    issue = AgentIssue(1242, "verify", "", ["agent-ready"])
    reservation = MagicMock(allowed=True, attempt=1)
    snapshot = MagicMock()
    durable_db = tmp_path / "driver" / "llm-runs" / "run.db"

    def record(name: str, value: Any) -> Any:
        events.append(name)
        return value

    with (
        patch("fieldkit.driver.runner.reserve_attempt", return_value=reservation),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "agent"),
        patch("fieldkit.driver.runner._isolate_data_dir", return_value=tmp_path / "data"),
        patch("fieldkit.driver.runner.reserve_run_db_path", return_value=durable_db),
        patch(
            "fieldkit.driver.runner.create_trusted_snapshot", side_effect=lambda *a, **k: record("snapshot", snapshot)
        ),
        patch(
            "fieldkit.driver.runner.run_opencode",
            side_effect=lambda *a, **k: record("agent", OpencodeOutcome(status="ok")),
        ) as agent,
        patch(
            "fieldkit.driver.runner.verify_submitted_head",
            side_effect=lambda *a, **k: record("verify", MagicMock(passed=True)),
        ),
        patch(
            "fieldkit.driver.runner.complete_attempt",
            side_effect=lambda *a, **k: record("complete", MagicMock(allowed=True)),
        ),
        patch(
            "fieldkit.driver.runner.transition_to_succeeded",
            side_effect=lambda *a, **k: record("github", None),
        ),
        patch("fieldkit.driver.runner.comment_on_issue"),
        patch("fieldkit.driver.runner.get_spend_summary", return_value="") as spend,
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.get_harness_scratch_root", return_value=tmp_path),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.remove_snapshot"),
    ):
        result = _execute_one(
            "example/repo",
            issue,
            tmp_path / "docs/work.md",
            tmp_path,
            datetime.now(tz=UTC),
            0.0,
            dry_run=False,
        )

    assert result.outcome == "ok"
    assert events == ["snapshot", "agent", "verify", "complete", "github"]
    assert agent.call_args.kwargs["llm_log_path"] == durable_db
    spend.assert_called_once_with(1242, durable_db)


def test_execute_one_verification_failure_uses_retry_path(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import _execute_one

    issue = AgentIssue(1242, "verify", "", ["agent-ready"])
    with (
        patch("fieldkit.driver.runner.reserve_attempt", return_value=MagicMock(allowed=True, attempt=1)),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "agent"),
        patch("fieldkit.driver.runner._isolate_data_dir", return_value=tmp_path / "data"),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=MagicMock()),
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")),
        patch(
            "fieldkit.driver.runner.verify_submitted_head",
            return_value=MagicMock(passed=False, reason="check failed"),
        ),
        patch("fieldkit.driver.runner.complete_attempt", return_value=MagicMock(allowed=True)) as complete,
        patch("fieldkit.driver.runner.transition_to_failed") as failed,
        patch("fieldkit.driver.runner.transition_to_succeeded") as succeeded,
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.get_harness_scratch_root", return_value=tmp_path),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.remove_snapshot"),
    ):
        result = _execute_one(
            "example/repo",
            issue,
            tmp_path / "docs/work.md",
            tmp_path,
            datetime.now(tz=UTC),
            0.0,
            dry_run=False,
        )

    assert result.outcome == "failed"
    assert "Independent verification failed" in result.error
    assert complete.call_args.kwargs["succeeded"] is False
    failed.assert_called_once()
    succeeded.assert_not_called()


def test_execute_one_snapshot_storage_failure_closes_attempt(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import _execute_one

    issue = AgentIssue(1242, "verify", "", ["agent-ready"])
    with (
        patch("fieldkit.driver.runner.reserve_attempt", return_value=MagicMock(allowed=True, attempt=1)),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "agent"),
        patch("fieldkit.driver.runner._isolate_data_dir", return_value=tmp_path / "data"),
        patch("fieldkit.driver.runner.create_trusted_snapshot", side_effect=OSError("no space")),
        patch("fieldkit.driver.runner.run_opencode") as agent,
        patch("fieldkit.driver.runner.complete_attempt", return_value=MagicMock(allowed=True)) as complete,
        patch("fieldkit.driver.runner.transition_to_failed") as failed,
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.get_harness_scratch_root", return_value=tmp_path),
        patch("fieldkit.driver.runner.remove_worktree"),
    ):
        result = _execute_one(
            "example/repo",
            issue,
            tmp_path / "docs/work.md",
            tmp_path,
            datetime.now(tz=UTC),
            0.0,
            dry_run=False,
        )

    assert result.outcome == "failed"
    assert "no space" in result.error
    assert complete.call_args.kwargs["succeeded"] is False
    failed.assert_called_once()
    agent.assert_not_called()


def test_execute_one_auth_failure_closes_attempt_without_github_projection(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import _execute_one
    from fieldkit.errors import AuthError

    issue = AgentIssue(1242, "verify", "", ["agent-ready"])
    with (
        patch("fieldkit.driver.runner.reserve_attempt", return_value=MagicMock(allowed=True, attempt=1)),
        patch("fieldkit.driver.runner.create_worktree", return_value=tmp_path / "agent"),
        patch("fieldkit.driver.runner._isolate_data_dir", return_value=tmp_path / "data"),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=MagicMock()),
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")),
        patch("fieldkit.driver.runner.verify_submitted_head", side_effect=AuthError("login required")),
        patch("fieldkit.driver.runner.complete_attempt", return_value=MagicMock(allowed=True)) as complete,
        patch("fieldkit.driver.runner.transition_to_failed") as failed,
        patch("fieldkit.driver.runner.transition_to_succeeded") as succeeded,
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.get_harness_scratch_root", return_value=tmp_path),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.remove_snapshot"),
        pytest.raises(AuthError, match="login required"),
    ):
        _execute_one(
            "example/repo",
            issue,
            tmp_path / "docs/work.md",
            tmp_path,
            datetime.now(tz=UTC),
            0.0,
            dry_run=False,
        )

    assert complete.call_args.kwargs["succeeded"] is False
    failed.assert_not_called()
    succeeded.assert_not_called()


def test_execute_one_dry_run_skips_snapshot_and_verification(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import _execute_one

    issue = AgentIssue(1242, "verify", "", ["agent-ready"])
    with (
        patch("fieldkit.driver.runner.check_eligibility", return_value=MagicMock(allowed=True, attempt=1)),
        patch("fieldkit.driver.runner.create_trusted_snapshot") as snapshot,
        patch("fieldkit.driver.runner.verify_submitted_head") as verify,
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")) as agent,
    ):
        result = _execute_one(
            "example/repo",
            issue,
            tmp_path / "docs/work.md",
            tmp_path,
            datetime.now(tz=UTC),
            0.0,
            dry_run=True,
        )

    assert result.outcome == "dry-run"
    snapshot.assert_not_called()
    verify.assert_not_called()
    assert agent.call_args.kwargs["dry_run"] is True


def test_reserve_run_db_path_returns_distinct_durable_paths(tmp_path: Path) -> None:
    from fieldkit.driver.spend import reserve_run_db_path

    with patch("fieldkit.driver.spend.get_fieldkit_data", return_value=tmp_path):
        first = reserve_run_db_path(1266)
        second = reserve_run_db_path(1266)

    assert first != second
    assert first.parent == tmp_path / "driver" / "llm-runs"
    assert second.parent == first.parent
    assert first.parent.is_dir()


def test_build_opencode_env_pins_durable_llm_log(tmp_path: Path) -> None:
    from fieldkit.driver.opencode import _build_opencode_env

    data_dir = tmp_path / "worktree" / ".fieldkit-data"
    durable_db = tmp_path / "data" / "driver" / "llm-runs" / "run.db"

    result = _build_opencode_env(1266, data_dir, durable_db)

    assert result["FIELDKIT_DATA_DIR"] == str(data_dir)
    assert result["FIELDKIT_LLM_LOG"] == str(durable_db)
    assert result["FIELDKIT_LLM_ACCOUNT"] == "driver-issue-1266"


def test_get_daily_spend_total_aggregates_distinct_run_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver.spend import get_daily_spend_total

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    default_dir = tmp_path / "ordinary"
    run_dir = tmp_path / "driver" / "llm-runs"
    default_dir.mkdir()
    run_dir.mkdir(parents=True)
    default_db = _make_db(
        default_dir, [("a", f"{today}T01:00:00", "x", "driver-issue-1", "m", 1, 1, 1, 0.2, None, None)]
    )
    run_db = _make_db(run_dir, [("b", f"{today}T02:00:00", "x", "driver-issue-2", "m", 1, 1, 1, 0.3, None, None)])
    run_db.rename(run_dir / "run.db")

    monkeypatch.delenv("FIELDKIT_LLM_LOG", raising=False)
    with (
        patch("fieldkit.driver.spend.get_db_path", return_value=default_db),
        patch("fieldkit.driver.spend.get_legacy_db_path", return_value=default_db),
        patch("fieldkit.driver.spend.get_fieldkit_data", return_value=tmp_path),
    ):
        total = get_daily_spend_total()

    assert total == pytest.approx(0.5)


def test_get_daily_spend_total_fails_closed_for_malformed_run_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver.spend import get_daily_spend_total

    run_dir = tmp_path / "driver" / "llm-runs"
    run_dir.mkdir(parents=True)
    (run_dir / "broken.db").write_text("not sqlite")
    missing = tmp_path / "missing.db"

    monkeypatch.delenv("FIELDKIT_LLM_LOG", raising=False)
    with (
        patch("fieldkit.driver.spend.get_db_path", return_value=missing),
        patch("fieldkit.driver.spend.get_legacy_db_path", return_value=missing),
        patch("fieldkit.driver.spend.get_fieldkit_data", return_value=tmp_path),
    ):
        total = get_daily_spend_total()

    assert total is None


def test_get_daily_spend_total_includes_run_databases_with_parent_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver.spend import get_daily_spend_total

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    override_dir = tmp_path / "override"
    run_dir = tmp_path / "driver" / "llm-runs"
    override_dir.mkdir()
    run_dir.mkdir(parents=True)
    override_db = _make_db(
        override_dir, [("a", f"{today}T01:00:00", "x", "driver-issue-1", "m", 1, 1, 1, 0.2, None, None)]
    )
    run_db = _make_db(run_dir, [("b", f"{today}T02:00:00", "x", "driver-issue-2", "m", 1, 1, 1, 0.3, None, None)])
    run_db.rename(run_dir / "run.db")
    monkeypatch.setenv("FIELDKIT_LLM_LOG", str(override_db))

    with (
        patch("fieldkit.driver.spend.get_db_path", return_value=override_db),
        patch("fieldkit.driver.spend.get_fieldkit_data", return_value=tmp_path),
    ):
        total = get_daily_spend_total()

    assert total == pytest.approx(0.5)


def test_get_daily_spend_total_returns_none_when_run_directory_scan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver.spend import get_daily_spend_total

    run_dir = tmp_path / "driver" / "llm-runs"
    run_dir.mkdir(parents=True)
    monkeypatch.delenv("FIELDKIT_LLM_LOG", raising=False)
    with (
        patch("fieldkit.driver.spend.get_db_path", return_value=tmp_path / "missing.db"),
        patch("fieldkit.driver.spend.get_legacy_db_path", return_value=tmp_path / "legacy.db"),
        patch("fieldkit.driver.spend.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.spend.os.scandir", side_effect=PermissionError("denied")),
    ):
        total = get_daily_spend_total()

    assert total is None


def test_run_driver_limits_capped_execution_to_one_admitted_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import run_driver

    work_order = tmp_path / "docs" / "work-orders" / "work.md"
    work_order.parent.mkdir(parents=True)
    work_order.write_text("---\ncovers:\n  - src/fieldkit/llm/log.py\n---\n")
    issues = [AgentIssue(1, "one", f"WorkOrder: {work_order.relative_to(tmp_path)}", ["agent-ready"])]
    monkeypatch.setenv("FIELDKIT_DRIVER_SPEND_CAP", "10")
    with (
        patch("fieldkit.driver.runner.evaluate_daily_spend_cap", return_value=MagicMock(allowed=True)),
        patch("fieldkit.driver.runner.get_driver_max_concurrent", return_value=4),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.get_github_repo", return_value="example/repo"),
        patch("fieldkit.driver.runner._fetch_main"),
        patch("fieldkit.driver.runner.list_ready_issues", return_value=issues),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.select_runnable", return_value=([], [])) as select,
    ):
        run_driver(repo_root=tmp_path, dry_run=False)

    assert select.call_args.args[3] == 1
