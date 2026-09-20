"""Unit tests for fieldkit/commands/issue/cli.py.

All GHIssueStore calls are mocked — no live GitHub API calls.
Tests cover: create, list, show, close, reopen, fix, plan, note, edit, board.
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
    create_return: GHIssue | None = None,
) -> MagicMock:
    store = MagicMock()
    store.find.return_value = find_return
    store.list_issues.return_value = list_return or []
    store.create.return_value = create_return or _make_issue()
    store.update_status.return_value = find_return
    store.mark_fixed.return_value = find_return
    store.add_note.return_value = find_return
    store.edit.return_value = find_return
    store.known_modules.return_value = {"sf", "gmail", "other", "watch", "pursuit", "cli"}
    return store


@pytest.mark.unit
def test_issue_help_documents_the_public_issue_id_format() -> None:
    result = _runner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "fieldkit-<number>" in result.output


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_creates_issue_and_prints_id() -> None:
    runner = _runner()
    new_issue = _make_issue("historic regression", gh_number=99)
    store = _mock_store(create_return=new_issue)

    with (
        patch("fieldkit.commands.issue.cli._store", return_value=store),
        patch("fieldkit.commands.issue.cli.get_github_repo", return_value=REPO),
    ):
        result = runner.invoke(
            cli,
            [
                "create",
                "--type",
                "bug",
                "--title",
                "test bug",
                "--severity",
                "high",
                "--module",
                "sf",
                "--source",
                "agent",
            ],
        )

    assert result.exit_code == 0
    assert "historic regression" in result.output
    assert f"https://github.com/{REPO}/issues/99" in result.output


@pytest.mark.unit
def test_create_unknown_module_exits_1() -> None:
    runner = _runner()
    store = _mock_store()

    with (
        patch("fieldkit.commands.issue.cli._store", return_value=store),
        patch("fieldkit.commands.issue.cli.get_github_repo", return_value=REPO),
    ):
        result = runner.invoke(
            cli,
            [
                "create",
                "--type",
                "bug",
                "--title",
                "test",
                "--module",
                "notamodule",
            ],
        )

    assert result.exit_code == 1


@pytest.mark.parametrize("module", ["companion", "config", "contact", "driver", "health", "meeting", "web"])
def test_create_accepts_current_domain_modules(module: str) -> None:
    runner = _runner()
    store = _mock_store()
    store.known_modules.return_value.add(module)

    with (
        patch("fieldkit.commands.issue.cli._store", return_value=store),
        patch("fieldkit.commands.issue.cli.get_github_repo", return_value=REPO),
    ):
        result = runner.invoke(
            cli,
            ["create", "--type", "bug", "--title", "test", "--module", module],
        )

    assert result.exit_code == 0
    assert store.create.call_args.kwargs["module"] == module


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_shows_open_issues_by_default() -> None:
    runner = _runner()
    issues = [_make_issue("historic regression", gh_number=1), _make_issue("historic regression", gh_number=2)]
    store = _mock_store(list_return=issues)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "historic regression" in result.output
    assert "historic regression" in result.output


@pytest.mark.unit
def test_list_no_issues_prints_message() -> None:
    runner = _runner()
    store = _mock_store(list_return=[])

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "No open issues found" in result.output


@pytest.mark.unit
def test_list_all_flag_passes_all_to_store() -> None:
    runner = _runner()
    store = _mock_store(list_return=[])

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        runner.invoke(cli, ["list", "--all"])

    store.list_issues.assert_called_once()
    call_kwargs = store.list_issues.call_args[1]
    assert call_kwargs["status"] == "all"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_show_displays_issue_details() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", gh_number=42)
    store = _mock_store(find_return=issue)

    with (
        patch("fieldkit.commands.issue.cli._store", return_value=store),
        patch("fieldkit.commands.issue.cli.get_github_repo", return_value=REPO),
    ):
        result = runner.invoke(cli, ["show", "historic regression"])

    assert result.exit_code == 0
    assert "historic regression" in result.output
    assert "something broken" in result.output
    assert "https://github.com" in result.output


@pytest.mark.unit
def test_show_not_found_exits_3() -> None:
    runner = _runner()
    store = _mock_store(find_return=None)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["show", "historic regression"])

    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_close_fixed_issue_succeeds() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="fixed")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["close", "historic regression"])

    assert result.exit_code == 0
    store.update_status.assert_called_once_with("historic regression", "closed", note=None)


@pytest.mark.unit
def test_close_open_without_skip_verify_exits_1() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="open")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["close", "historic regression"])

    assert result.exit_code == 1


@pytest.mark.unit
def test_close_wont_fix_flag() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="open")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["close", "historic regression", "--wont-fix", "--skip-verify"])

    assert result.exit_code == 0
    store.update_status.assert_called_once_with("historic regression", "wont-fix", note=None)


@pytest.mark.unit
def test_close_with_skip_verify_closes_open_issue() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="open")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["close", "historic regression", "--skip-verify"])

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# reopen
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reopen_closed_issue() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="closed")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["reopen", "historic regression"])

    assert result.exit_code == 0
    store.update_status.assert_called_once()


@pytest.mark.unit
def test_reopen_already_open_is_noop() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="open")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["reopen", "historic regression"])

    assert result.exit_code == 0
    store.update_status.assert_not_called()


@pytest.mark.unit
def test_reopen_not_found_exits_3() -> None:
    runner = _runner()
    store = _mock_store(find_return=None)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["reopen", "historic regression"])

    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fix_open_issue_calls_mark_fixed() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="open")
    fixed_issue = _make_issue("historic regression", status="fixed")
    store = _mock_store(find_return=issue)
    store.mark_fixed.return_value = fixed_issue

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["fix", "historic regression", "--commit", "abc1234"])

    assert result.exit_code == 0
    store.mark_fixed.assert_called_once_with("historic regression", commit="abc1234", note=None)
    assert "abc1234" in result.output


@pytest.mark.unit
def test_fix_already_closed_exits_1() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="closed")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["fix", "historic regression"])

    assert result.exit_code == 1
    store.mark_fixed.assert_not_called()


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plan_open_issue() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="open")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["plan", "historic regression"])

    assert result.exit_code == 0
    store.update_status.assert_called_once_with("historic regression", "planned", note=None)


@pytest.mark.unit
def test_plan_already_planned_exits_1() -> None:
    runner = _runner()
    issue = _make_issue("historic regression", status="planned")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["plan", "historic regression"])

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# note
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_note_adds_comment() -> None:
    runner = _runner()
    issue = _make_issue("historic regression")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["note", "historic regression", "Verified on staging"])

    assert result.exit_code == 0
    store.add_note.assert_called_once_with("historic regression", "Verified on staging")


@pytest.mark.unit
def test_note_not_found_exits_3() -> None:
    runner = _runner()
    store = _mock_store(find_return=None)
    store.add_note.return_value = None

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["note", "historic regression", "some text"])

    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_edit_title_calls_store_edit() -> None:
    runner = _runner()
    issue = _make_issue("historic regression")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["edit", "historic regression", "--title", "updated title"])

    assert result.exit_code == 0
    store.edit.assert_called_once()
    call_kwargs = store.edit.call_args[1]
    assert call_kwargs["title"] == "updated title"


@pytest.mark.unit
def test_edit_unknown_module_exits_1() -> None:
    runner = _runner()
    issue = _make_issue("historic regression")
    store = _mock_store(find_return=issue)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["edit", "historic regression", "--module", "badmodule"])

    assert result.exit_code == 1
    store.edit.assert_not_called()


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_board_shows_open_issues() -> None:
    runner = _runner()
    bugs = [_make_issue("historic regression", gh_number=1), _make_issue("historic regression", gh_number=2)]
    enhs = [_make_issue("implementation change", issue_type="enhancement", gh_number=10)]

    store = MagicMock()
    store.list_issues.side_effect = lambda status, **kw: {
        "open": bugs + enhs,
        "planned": [],
        "fixed": [],
        "closed": [],
        "wont-fix": [],
    }.get(status, [])

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = runner.invoke(cli, ["board"])

    assert result.exit_code == 0
    assert "historic regression" in result.output
    assert "historic regression" in result.output
