"""Tests for run_driver covers-as-locks batch scheduling.

Covers:
- Concurrent execution of pairwise-disjoint batches
- Batch-overlap and busy-covers skips (issue keeps agent-ready)
- Fail-closed behavior when the busy-set lookup errors
- max_concurrent=1 parity with the old one-issue-per-tick behavior
- depends_on blocking while a listed issue is open
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.driver import runner
from fieldkit.driver.github import AgentIssue
from fieldkit.driver.opencode import OpencodeOutcome
from fieldkit.driver.prompt_source import FrozenPrompt, PromptSource, freeze_prompt

pytestmark = pytest.mark.unit


def _isolated_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    issues = [_covered_issue_and_wo(tmp_path, number, [f"src/{number}.py"]) for number in (1, 2)]
    monkeypatch.setattr(runner, "get_fieldkit_data", lambda: tmp_path)
    monkeypatch.setattr(runner, "get_github_repo", lambda: "owner/repo")
    monkeypatch.setattr(runner, "list_ready_issues", lambda _repo: issues)
    monkeypatch.setattr(runner, "busy_files", lambda _repo: {})
    monkeypatch.setattr(runner, "get_driver_max_concurrent", lambda: 2)
    monkeypatch.setattr("fieldkit.commands.driver.cli._repo_root", lambda: tmp_path)

    def execute(_repo: str, issue: AgentIssue, *_args: object, **_kwargs: object) -> runner.RunResult:
        return runner.RunResult(issue.number, issue.title, "ok", "branch", 0.0, "", "")

    monkeypatch.setattr(runner, "_execute_one", execute)


@pytest.mark.parametrize("failed_number", [1, 2])
@pytest.mark.parametrize("as_json", [False, True], ids=["human", "json"])
def test_mixed_execution_failure_never_becomes_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_number: int, as_json: bool
) -> None:
    from fieldkit.commands.driver.cli import cli

    _isolated_tick(tmp_path, monkeypatch)

    def execute(_repo: str, issue: AgentIssue, *_args: object, **_kwargs: object) -> runner.RunResult:
        failed = issue.number == failed_number
        return runner.RunResult(
            issue.number,
            issue.title,
            "failed" if failed else "ok",
            "branch",
            0.0,
            "",
            "execution failed" if failed else "",
        )

    monkeypatch.setattr(runner, "_execute_one", execute)
    result = CliRunner().invoke(cli, ["run", *(["--json"] if as_json else [])])

    assert result.exit_code == 1, result.output
    assert f"#{failed_number}" in result.output
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["issue_number"] is None
        assert [item["issue_number"] for item in payload["candidates"]] == [1, 2]
    records = json.loads((tmp_path / "logs/driver/driver-run-status.json").read_text(encoding="utf-8"))["runs"]
    assert sorted(item["issue_number"] for item in records) == [1, 2]


@pytest.mark.parametrize("failure_origin", ["direct", "retry-finalization"])
@pytest.mark.parametrize("as_json", [False, True], ids=["human", "json"])
def test_single_failure_is_sanitized_before_output_and_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_origin: str, as_json: bool
) -> None:
    from fieldkit.commands.driver.cli import cli
    from fieldkit.driver import retry_state

    _isolated_tick(tmp_path, monkeypatch)
    issue = _covered_issue_and_wo(tmp_path, 1, ["src/1.py"])
    monkeypatch.setattr(runner, "list_ready_issues", lambda _repo: [issue])

    def failed_update(_path: Path) -> None:
        raise OSError("fictional-secret /private/location")

    monkeypatch.setattr(retry_state, "locked_json_update", failed_update)

    def execute(_repo: str, issue: AgentIssue, *_args: object, **_kwargs: object) -> runner.RunResult:
        if failure_origin == "direct":
            return runner.RunResult(
                issue.number, issue.title, "failed", "branch", 0.0, "", "fictional-secret /private/location"
            )
        return runner._finalize_run_outcome(
            "owner/repo",
            issue,
            attempt=1,
            branch="branch",
            oc_outcome=OpencodeOutcome(status="ok"),
            started_at=datetime.now(UTC),
            spend_note="",
            dry_run=False,
        )

    monkeypatch.setattr(runner, "_execute_one", execute)
    result = CliRunner().invoke(cli, ["run", *(["--json"] if as_json else [])])

    assert result.exit_code == 1
    assert "fictional-secret" not in result.output
    assert "/private/location" not in result.output
    raw_status = (tmp_path / "logs/driver/driver-run-status.json").read_text(encoding="utf-8")
    assert "fictional-secret" not in raw_status
    assert "/private/location" not in raw_status
    assert json.loads(raw_status)["runs"][0]["outcome"] == "failed"


@pytest.mark.parametrize("blocked", ["busy", "retry", "invalid"])
def test_blocked_candidate_and_success_are_partial_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    from fieldkit.driver.prompt_source import PromptSourceError
    from fieldkit.driver.retry_state import RetryDecision

    _isolated_tick(tmp_path, monkeypatch)
    if blocked == "busy":
        monkeypatch.setattr(runner, "busy_files", lambda _repo: {"src/1.py": 17})
    elif blocked == "retry":
        monkeypatch.setattr(
            runner,
            "check_eligibility",
            lambda _repo, number, _labels, **_kwargs: RetryDecision(
                number != 1, f"owner/repo#{number}", "retryable", 1, None, "retry cooldown"
            ),
        )
    else:

        def freeze(repo_root: Path, source: PromptSource, revision: str | None) -> FrozenPrompt:
            if source.path.name == "wo-1.md":
                raise PromptSourceError("invalid structured authority")
            return freeze_prompt(repo_root, source, revision)

        monkeypatch.setattr(runner, "freeze_prompt", freeze)
    result = runner.run_driver(repo_root=tmp_path)

    assert result.outcome == "failed"
    assert "#1" in result.error
    records = json.loads((tmp_path / "logs/driver/driver-run-status.json").read_text(encoding="utf-8"))["runs"]
    assert sorted((item["issue_number"], item["outcome"]) for item in records) == [(1, "skipped"), (2, "ok")]


@pytest.mark.parametrize("failure_site", ["writer", "atomic_update"])
def test_status_persistence_failure_is_partial_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    _isolated_tick(tmp_path, monkeypatch)

    def unavailable(_result: object) -> None:
        raise OSError("fictional-secret /private/location")

    monkeypatch.setattr(runner, "_write_run_status" if failure_site == "writer" else "locked_json_update", unavailable)
    result = runner.run_driver(repo_root=tmp_path)

    assert result.outcome == "failed"
    assert "status" in result.error.lower()
    assert "fictional-secret" not in result.error
    assert "/private/location" not in result.error
    assert all(not candidate.status_persisted for candidate in result.candidates)


def test_retry_denied_queue_retains_each_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.driver.retry_state import RetryDecision

    _isolated_tick(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runner,
        "check_eligibility",
        lambda _repo, number, _labels, **_kwargs: RetryDecision(
            False, f"owner/repo#{number}", "retryable", 1, None, "retry cooldown"
        ),
    )
    result = runner.run_driver(repo_root=tmp_path)

    assert result.outcome == "skipped"
    assert "#1" in result.error and "#2" in result.error
    records = json.loads((tmp_path / "logs/driver/driver-run-status.json").read_text(encoding="utf-8"))["runs"]
    assert {item["issue_number"] for item in records if item["issue_number"] is not None} == {1, 2}


def test_capacity_deferral_remains_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_tick(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "get_driver_max_concurrent", lambda: 1)
    result = runner.run_driver(repo_root=tmp_path)

    assert result.outcome == "ok"
    assert result.error == ""
    records = json.loads((tmp_path / "logs/driver/driver-run-status.json").read_text(encoding="utf-8"))["runs"]
    assert [item["issue_number"] for item in records] == [1]


def test_empty_queue_is_success_without_candidate_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.commands.driver.cli import cli

    _isolated_tick(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "list_ready_issues", lambda _repo: [])
    result = CliRunner().invoke(cli, ["run", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["candidates"] == []
    assert not (tmp_path / "logs/driver/driver-run-status.json").exists()


@pytest.mark.parametrize("as_json", [False, True], ids=["human", "json"])
def test_authentication_during_batch_retains_completed_status_and_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    from fieldkit.commands.driver.cli import cli
    from fieldkit.errors import AuthError

    _isolated_tick(tmp_path, monkeypatch)

    def execute(_repo: str, issue: AgentIssue, *_args: object, **_kwargs: object) -> runner.RunResult:
        if issue.number == 1:
            raise AuthError("fictional-secret /private/location")
        return runner.RunResult(issue.number, issue.title, "ok", "branch", 0.0, "", "")

    monkeypatch.setattr(runner, "_execute_one", execute)
    result = CliRunner().invoke(cli, ["run", *(["--json"] if as_json else [])])

    assert result.exit_code == 2
    assert "fictional-secret" not in result.output
    records = json.loads((tmp_path / "logs/driver/driver-run-status.json").read_text(encoding="utf-8"))["runs"]
    assert sorted((item["issue_number"], item["outcome"]) for item in records) == [(1, "failed"), (2, "ok")]


@pytest.mark.parametrize("as_json", [False, True], ids=["human", "json"])
def test_authentication_failure_stays_two_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    from fieldkit.commands.driver.cli import cli
    from fieldkit.errors import AuthError

    _isolated_tick(tmp_path, monkeypatch)

    def unavailable(_repo: str) -> None:
        raise AuthError("fictional-secret /private/location")

    monkeypatch.setattr(runner, "busy_files", unavailable)
    result = CliRunner().invoke(cli, ["run", *(["--json"] if as_json else [])])

    assert result.exit_code == 2
    assert "fictional-secret" not in result.output
    assert "/private/location" not in result.output


def _covered_issue_and_wo(tmp_path: Path, number: int, covers: list[str]) -> AgentIssue:
    wo_dir = tmp_path / "docs" / "work-orders"
    wo_dir.mkdir(parents=True, exist_ok=True)
    covers_block = "\n".join(f"  - {path}" for path in [*covers, f"driver-target-{number}.txt"])
    target = tmp_path / f"driver-target-{number}.txt"
    target.write_text(f"unique anchor {number}\n", encoding="utf-8")
    (wo_dir / f"wo-{number}.md").write_text(
        f"---\ncovers:\n{covers_block}\n"
        "edit_sites:\n  version: 1\n  sites:\n"
        f"    - path: driver-target-{number}.txt\n      anchor: unique anchor {number}\n"
        "done_checks:\n  version: 1\n  checks:\n    - id: tests\n      argv: [pytest, -q]\n"
        "---\n\n# Work Order\n",
        encoding="utf-8",
    )
    return AgentIssue(
        number=number,
        title=f"Issue {number}",
        body=f"WorkOrder: docs/work-orders/wo-{number}.md",
        labels=["agent-ready"],
    )


def _fake_worktree(_branch: str, repo_root: Path, issue_number: int, **_kwargs: object) -> Path:
    worktree = repo_root / f"wt-{issue_number}"
    destination = worktree / "docs" / "work-orders" / f"wo-{issue_number}.md"
    destination.parent.mkdir(parents=True)
    destination.write_bytes((repo_root / "docs" / "work-orders" / f"wo-{issue_number}.md").read_bytes())
    return worktree


def test_batch_of_two_disjoint_issues_both_execute(tmp_path: Path) -> None:
    from fieldkit.driver.runner import run_driver

    issue_a = _covered_issue_and_wo(tmp_path, 1, ["src/a.py"])
    issue_b = _covered_issue_and_wo(tmp_path, 2, ["src/b.py"])

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue_a, issue_b]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.get_driver_max_concurrent", return_value=2),
        patch("fieldkit.driver.runner.create_worktree", side_effect=_fake_worktree),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=object()),
        patch(
            "fieldkit.driver.runner.verify_submitted_head", return_value=SimpleNamespace(passed=True, reason="")
        ) as mock_verify,
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch("fieldkit.driver.runner.remove_worktree") as mock_rm_wt,
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")) as mock_oc,
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.transition_to_succeeded") as mock_ok,
        patch("fieldkit.driver.runner.comment_on_issue"),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "ok"
    assert mock_oc.call_count == 2
    assert mock_ok.call_count == 2
    assert mock_rm_wt.call_count == 2
    verification_starts = {call.kwargs["run_started_monotonic"] for call in mock_verify.call_args_list}
    assert len(verification_starts) == 1
    # Each run gets its own status entry
    status = json.loads((tmp_path / "logs" / "driver" / "driver-run-status.json").read_text())
    executed_numbers = sorted(run["issue_number"] for run in status["runs"])
    assert executed_numbers == [1, 2]


def test_batch_overlapping_second_issue_skipped_with_reason(tmp_path: Path) -> None:
    from fieldkit.driver.runner import run_driver

    issue_a = _covered_issue_and_wo(tmp_path, 1, ["src/a.py"])
    issue_b = _covered_issue_and_wo(tmp_path, 2, ["src/a.py", "src/b.py"])

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue_a, issue_b]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.get_driver_max_concurrent", return_value=2),
        patch("fieldkit.driver.runner.create_worktree", side_effect=_fake_worktree),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=object()),
        patch("fieldkit.driver.runner.verify_submitted_head", return_value=SimpleNamespace(passed=True, reason="")),
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")) as mock_oc,
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.transition_to_succeeded"),
        patch("fieldkit.driver.runner.comment_on_issue"),
        patch("fieldkit.driver.runner.remove_label") as mock_remove,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.issue_number is None
    assert result.outcome == "failed"
    assert mock_oc.call_count == 1  # only the first, disjointness violated by #2
    mock_remove.assert_not_called()  # skipped issue keeps agent-ready


def test_busy_covers_skip_keeps_agent_ready_and_records_skipped(tmp_path: Path) -> None:
    # Spec: candidate blocked by an open PR touching a covered file stays
    # agent-ready; when nothing is runnable the run records outcome=skipped
    # with the blocking file and PR number.
    from fieldkit.driver.runner import run_driver

    issue_a = _covered_issue_and_wo(tmp_path, 1, ["src/a.py"])

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue_a]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={"src/a.py": 1350}),
        patch("fieldkit.driver.runner.run_opencode") as mock_oc,
        patch("fieldkit.driver.runner.remove_label") as mock_remove,
        patch("fieldkit.driver.runner.transition_to_failed") as mock_fail,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    assert "src/a.py" in result.error
    assert "#1350" in result.error
    mock_oc.assert_not_called()
    mock_remove.assert_not_called()
    mock_fail.assert_not_called()


def test_busy_set_lookup_failure_fails_closed(tmp_path: Path) -> None:
    from fieldkit.driver.runner import run_driver
    from fieldkit.driver.scheduler import SchedulerError

    issue_a = _covered_issue_and_wo(tmp_path, 1, ["src/a.py"])

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue_a]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", side_effect=SchedulerError("gh pr failed: boom")),
        patch("fieldkit.driver.runner.run_opencode") as mock_oc,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    assert "fail closed" in result.error
    mock_oc.assert_not_called()


def test_max_concurrent_one_executes_exactly_one(tmp_path: Path) -> None:
    # Characterization: default max_concurrent=1 preserves the old
    # one-issue-per-tick behavior even with several eligible candidates.
    from fieldkit.driver.runner import run_driver

    issue_a = _covered_issue_and_wo(tmp_path, 1, ["src/a.py"])
    issue_b = _covered_issue_and_wo(tmp_path, 2, ["src/b.py"])

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue_a, issue_b]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.get_driver_max_concurrent", return_value=1),
        patch("fieldkit.driver.runner.create_worktree", side_effect=_fake_worktree),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=object()),
        patch("fieldkit.driver.runner.verify_submitted_head", return_value=SimpleNamespace(passed=True, reason="")),
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="ok")) as mock_oc,
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.transition_to_succeeded"),
        patch("fieldkit.driver.runner.comment_on_issue"),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.issue_number == 1  # oldest eligible first
    assert mock_oc.call_count == 1


def test_depends_on_open_issue_blocks_execution(tmp_path: Path) -> None:
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import run_driver

    wo_dir = tmp_path / "docs" / "work-orders"
    wo_dir.mkdir(parents=True)
    target = tmp_path / "driver-target-6.txt"
    target.write_text("unique anchor 6\n", encoding="utf-8")
    (wo_dir / "wo-6.md").write_text(
        "---\ncovers:\n  - src/a.py\n  - driver-target-6.txt\ndepends_on: [1293]\n"
        "edit_sites:\n  version: 1\n  sites:\n"
        "    - path: driver-target-6.txt\n      anchor: unique anchor 6\n"
        "done_checks:\n  version: 1\n  checks:\n    - id: tests\n      argv: [pytest, -q]\n"
        "---\n\n# Work Order\n",
        encoding="utf-8",
    )
    issue = AgentIssue(
        number=6,
        title="Depends on 1293",
        body="WorkOrder: docs/work-orders/wo-6.md",
        labels=["agent-ready"],
    )

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.open_issue_numbers", return_value=frozenset({1293})) as mock_open,
        patch("fieldkit.driver.runner.run_opencode") as mock_oc,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    assert "#1293" in result.error
    mock_open.assert_called_once_with("owner/repo", {1293})
    mock_oc.assert_not_called()


def test_run_driver_retains_agent_ready_when_work_order_unresolvable(tmp_path: Path) -> None:
    """Bug 2 (anti-strand): a body that *references* a work order not resolvable
    this tick keeps ``agent-ready`` (self-heals once the file lands), posts an
    idempotent ``comment_once`` note, and records a skipped run — NOT delabeled.
    """
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import _WO_UNRESOLVED_MARKER, run_driver

    (tmp_path / "docs" / "work-orders").mkdir(parents=True)
    issue = AgentIssue(
        number=1,
        title="unlanded WO",
        body="WorkOrder: docs/work-orders/not-yet-merged.md",
        labels=["agent-ready"],
    )

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.prompt_source._exists_on_main_or_disk", return_value=False),
        patch("fieldkit.driver.runner.comment_once") as mock_comment_once,
        patch("fieldkit.driver.runner.comment_on_issue") as mock_comment,
        patch("fieldkit.driver.runner.remove_label") as mock_remove,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    mock_remove.assert_not_called()  # agent-ready RETAINED — the whole point
    mock_comment_once.assert_called_once()
    assert mock_comment_once.call_args[0][:3] == ("owner/repo", 1, _WO_UNRESOLVED_MARKER)
    mock_comment.assert_not_called()  # policy A uses comment_once, not comment_on_issue


def test_run_driver_removes_agent_ready_when_no_prompt_reference(tmp_path: Path) -> None:
    """Bug 2 (contrast): a body with NO prompt reference loses ``agent-ready`` and
    gets a plain ``comment_on_issue`` — the malformed-issue path, distinct from the
    unresolvable-WO retain path above.
    """
    from fieldkit.driver.github import AgentIssue
    from fieldkit.driver.runner import run_driver

    issue = AgentIssue(
        number=2,
        title="no reference",
        body="This issue is agent-ready but names no work order.",
        labels=["agent-ready"],
    )

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.comment_once") as mock_comment_once,
        patch("fieldkit.driver.runner.comment_on_issue") as mock_comment,
        patch("fieldkit.driver.runner.remove_label") as mock_remove,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    mock_remove.assert_called_once_with("owner/repo", 2, "agent-ready")
    mock_comment.assert_called_once()
    mock_comment_once.assert_not_called()


def test_run_driver_persists_bounded_failure_reason(tmp_path: Path) -> None:
    """Raw child diagnostics stay in local logs, not status or public comments."""
    from fieldkit.driver.runner import run_driver

    issue = _covered_issue_and_wo(tmp_path, 1, ["src/a.py"])
    reason = (
        "OpenCode timed out after 3600s (killed by the 1h ceiling) for issue #1 — the session made no forward progress"
    )

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", side_effect=_fake_worktree),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=object()),
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch("fieldkit.driver.runner.run_opencode", return_value=OpencodeOutcome(status="failed", reason=reason)),
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.transition_to_failed") as mock_fail,
        patch("fieldkit.driver.runner.comment_on_issue"),
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "failed"
    assert result.error == "OpenCode execution failed; inspect the bounded local driver logs."
    assert "timed out" not in result.error
    mock_fail.assert_called_once()
    assert mock_fail.call_args.kwargs["error"] == result.error


def test_run_driver_skips_rate_limited_issue_without_penalty(tmp_path: Path) -> None:
    """A ``status="rate_limited"`` OpencodeOutcome (historic regression) is infrastructure
    starvation, not a work-order defect — the driver must not call
    ``transition_to_failed`` (which would burn an attempt and eventually
    escalate to agent-blocked), and the RunResult outcome is ``skipped``.
    """
    from fieldkit.driver.runner import run_driver

    issue = _covered_issue_and_wo(tmp_path, 1, ["src/a.py"])
    reason = "OpenCode hit sustained Claude Max rate limiting for issue #1 (3+ 'Claude Max rate limit reached' errors)"

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", side_effect=_fake_worktree),
        patch("fieldkit.driver.runner.create_trusted_snapshot", return_value=object()),
        patch("fieldkit.driver.runner.remove_snapshot"),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch(
            "fieldkit.driver.runner.run_opencode",
            return_value=OpencodeOutcome(status="rate_limited", reason=reason),
        ),
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.transition_to_failed") as mock_fail,
        patch("fieldkit.driver.runner.comment_on_issue"),
        patch("fieldkit.driver.runner.comment_once") as mock_comment_once,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=False)

    assert result.outcome == "skipped"
    assert "rate limit" in result.error.lower()
    mock_fail.assert_not_called()
    mock_comment_once.assert_called_once()
    assert mock_comment_once.call_args.args[0] == "owner/repo"
    assert mock_comment_once.call_args.args[1] == 1
    assert "Driver rate-limited" in mock_comment_once.call_args.args[2]


def test_run_driver_dry_run_rate_limited_posts_no_comment(tmp_path: Path) -> None:
    """``--dry-run`` must stay side-effect free on the rate-limited skip path.

    The branch is still *reached* under dry-run (``run_opencode`` is invoked
    regardless), so only the ``if not dry_run:`` guard stops the driver from
    writing a real comment to a real GitHub issue during a rehearsal. Without
    this test that guard is unpinned: removing it leaves every other driver
    test green.
    """
    from fieldkit.driver.runner import run_driver

    issue = _covered_issue_and_wo(tmp_path, 1, ["src/a.py"])
    reason = "OpenCode hit sustained Claude Max rate limiting for issue #1 (3+ 'Claude Max rate limit reached' errors)"

    with (
        patch("fieldkit.driver.runner.list_ready_issues", return_value=[issue]),
        patch("fieldkit.driver.runner.get_github_repo", return_value="owner/repo"),
        patch("fieldkit.driver.runner.get_fieldkit_data", return_value=tmp_path),
        patch("fieldkit.driver.runner.busy_files", return_value={}),
        patch("fieldkit.driver.runner.create_worktree", side_effect=_fake_worktree),
        patch("fieldkit.driver.runner.remove_worktree"),
        patch(
            "fieldkit.driver.runner.run_opencode",
            return_value=OpencodeOutcome(status="rate_limited", reason=reason),
        ),
        patch("fieldkit.driver.runner.get_spend_summary", return_value=""),
        patch("fieldkit.driver.runner.transition_to_failed") as mock_fail,
        patch("fieldkit.driver.runner.comment_on_issue") as mock_comment,
        patch("fieldkit.driver.runner.comment_once") as mock_comment_once,
    ):
        result = run_driver(repo_root=tmp_path, dry_run=True)

    assert result.outcome == "skipped"
    assert "rate limit" in result.error.lower()
    mock_comment_once.assert_not_called()
    mock_comment.assert_not_called()
    mock_fail.assert_not_called()
