"""Extended unit tests for fieldkit/commands/issue/cli.py.

Covers: sync-milestone, link, and edge cases for status transitions.
All GHIssueStore calls are mocked — no live GitHub API calls.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.issue.cli import cli
from fieldkit.commands.issue.gh_store import GHIssue

pytestmark = pytest.mark.unit

REPO = "owner/test-repo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(
    issue_id: str = "historic regression",
    title: str = "something broken",
    status: str = "open",
    severity: str = "high",
    module: str = "sf",
    issue_type: str = "bug",
    gh_number: int = 42,
) -> GHIssue:
    return GHIssue(
        id=issue_id,
        type=issue_type,  # type: ignore[arg-type]
        title=title,
        status=status,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        module=module,
        gh_number=gh_number,
        body="Body text.",
        source="test-agent",
        created=datetime(2026, 1, 15, tzinfo=UTC),
    )


def _runner() -> CliRunner:
    return CliRunner()


def _mock_store(
    find_return: GHIssue | None = None,
    list_return: list[GHIssue] | None = None,
    milestone_return: list[GHIssue] | None = None,
) -> MagicMock:
    store = MagicMock()
    store.find.return_value = find_return
    store.list_issues.return_value = list_return or []
    store.list_by_milestone.return_value = milestone_return or []
    store.update_status.return_value = find_return
    store.mark_fixed.return_value = find_return
    store.link_milestone.return_value = find_return
    store.known_modules.return_value = {"sf", "gmail", "other", "watch"}
    return store


# ---------------------------------------------------------------------------
# sync-milestone: queued
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sync_milestone_queued_advances_open_to_planned() -> None:
    runner = _runner()
    issues = [
        _make_issue("historic regression", status="open", gh_number=1),
        _make_issue("historic regression", status="open", gh_number=2),
    ]
    store = _mock_store(milestone_return=issues)
    store.update_status.return_value = issues[0]

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["sync-milestone", "M001", "--state", "queued"])

    assert result.exit_code == 0
    assert store.update_status.call_count == 2
    for call in store.update_status.call_args_list:
        assert call[0][1] == "planned"


@pytest.mark.unit
def test_sync_milestone_queued_skips_non_open() -> None:
    runner = _runner()
    issues = [
        _make_issue("historic regression", status="open", gh_number=1),
        _make_issue("historic regression", status="planned", gh_number=2),  # already planned
    ]
    store = _mock_store(milestone_return=issues)
    store.update_status.return_value = issues[0]

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["sync-milestone", "M001", "--state", "queued"])

    assert result.exit_code == 0
    # Only historic regression should be advanced
    assert store.update_status.call_count == 1
    assert "1 skipped" in result.output


# ---------------------------------------------------------------------------
# sync-milestone: completed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sync_milestone_completed_advances_planned_to_fixed() -> None:
    runner = _runner()
    issues = [
        _make_issue("historic regression", status="planned", gh_number=3),
    ]
    store = _mock_store(milestone_return=issues)
    store.mark_fixed.return_value = issues[0]

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["sync-milestone", "M001", "--state", "completed"])

    assert result.exit_code == 0
    store.mark_fixed.assert_called_once()


@pytest.mark.unit
def test_sync_milestone_prose_exits_1_when_update_fails() -> None:
    runner = _runner()
    issues = [_make_issue("historic regression", status="planned", gh_number=4)]
    store = _mock_store(milestone_return=issues)
    store.mark_fixed.return_value = None

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["sync-milestone", "M001", "--state", "completed"])

    assert result.exit_code == 1
    assert "update FAILED" in result.output


@pytest.mark.unit
def test_sync_milestone_no_issues_prints_message() -> None:
    runner = _runner()
    store = _mock_store(milestone_return=[])

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["sync-milestone", "M999", "--state", "queued"])

    assert result.exit_code == 0
    assert "No issues linked" in result.output


@pytest.mark.unit
def test_sync_milestone_dry_run_does_not_update() -> None:
    runner = _runner()
    issues = [_make_issue("historic regression", status="open", gh_number=1)]
    store = _mock_store(milestone_return=issues)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["sync-milestone", "M001", "--state", "queued", "--dry-run"])

    assert result.exit_code == 0
    store.update_status.assert_not_called()
    assert "would advance" in result.output


# ---------------------------------------------------------------------------
# sync-milestone: completed with commit SHA
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sync_milestone_completed_passes_commit() -> None:
    runner = _runner()
    issues = [_make_issue("historic regression", status="planned", gh_number=10)]
    store = _mock_store(milestone_return=issues)
    store.mark_fixed.return_value = issues[0]

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["sync-milestone", "M002", "--state", "completed", "--commit", "deadbeef"])

    assert result.exit_code == 0, result.output
    store.mark_fixed.assert_called_once()
    call_kwargs = store.mark_fixed.call_args[1]
    assert call_kwargs["commit"] == "deadbeef"


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_link_associates_issue_with_milestone() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", gh_number=42)
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["link", "historic regression", "M025"])

    assert result.exit_code == 0
    store.link_milestone.assert_called_once_with("historic regression", "M025", note=None)
    assert "M025" in result.output


@pytest.mark.unit
def test_link_not_found_exits_3() -> None:
    runner = _runner()
    store = _mock_store(find_return=None)
    store.link_milestone.return_value = None

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["link", "historic regression", "M001"])

    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# close: note propagated
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_close_with_note_passes_note_to_store() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="fixed")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["close", "historic regression", "--note", "Verified OK"])

    assert result.exit_code == 0
    call_kwargs = store.update_status.call_args[1]
    assert call_kwargs["note"] == "Verified OK"


# ---------------------------------------------------------------------------
# reopen: with note
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reopen_with_note() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="wont-fix")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["reopen", "historic regression", "--note", "Reconsidered"])

    assert result.exit_code == 0
    call_kwargs = store.update_status.call_args[1]
    assert call_kwargs["note"] == "Reconsidered"


# ---------------------------------------------------------------------------
# fix: planned issue
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fix_planned_issue_is_allowed() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="planned")
    fixed = _make_issue("historic regression", status="fixed")
    store = _mock_store(find_return=issue)
    store.mark_fixed.return_value = fixed

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["fix", "historic regression"])

    assert result.exit_code == 0
    store.mark_fixed.assert_called_once()


# ---------------------------------------------------------------------------
# list: --type filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_type_bug_passed_to_store() -> None:
    runner = _runner()
    store = _mock_store(list_return=[])

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        runner.invoke(cli, ["list", "--type", "bug"])

    call_kwargs = store.list_issues.call_args[1]
    assert call_kwargs["issue_type"] == "bug"


@pytest.mark.unit
def test_list_module_filter_passed_to_store() -> None:
    runner = _runner()
    store = _mock_store(list_return=[])

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        runner.invoke(cli, ["list", "--module", "sf"])

    call_kwargs = store.list_issues.call_args[1]
    assert call_kwargs["module"] == "sf"


# ---------------------------------------------------------------------------
# board: empty board
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_board_empty_shows_none() -> None:
    runner = _runner()
    store = MagicMock()
    store.list_issues.return_value = []

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["board"])

    assert result.exit_code == 0
    assert "(none)" in result.output
