"""--json coverage tests for fieldkit/commands/issue/cli.py.

Every `issue` subcommand accepts --json and emits a single machine-readable
document on stdout. All GHIssueStore calls are mocked — no live GitHub API calls.

Two properties are worth more than the individual payload shapes, so they are
asserted across all twelve commands rather than command by command:

  1. --json produces parseable JSON on *stdout* (never stderr), exit code 0.
  2. --json changes format only. Exit codes are unchanged, and the error paths
     still put prose on stderr and leave stdout empty — a consumer parses stdout
     iff the exit code is 0.

One deliberate exception to (2): `sync-milestone` exits 1 on *partial success* —
some candidate issues advanced, some did not — and still emits its full document
on stdout, because the `advanced`/`failed` breakdown is exactly what a caller
needs in that case. Partial success is not an error path. Consumers of that one
command parse stdout on exit 0 and exit 1 alike; `_ERROR_CASES` below covers the
genuine error paths, and sync-milestone is deliberately not among them.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

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


def _mock_store(issue: GHIssue | None) -> MagicMock:
    """A store where every lookup resolves to *issue* (or misses when None)."""
    store = MagicMock()
    store.find.return_value = issue
    store.create.return_value = issue
    store.update_status.return_value = issue
    store.mark_fixed.return_value = issue
    store.add_note.return_value = issue
    store.edit.return_value = issue
    store.link_milestone.return_value = issue
    store.list_issues.side_effect = lambda status, **kw: [issue] if issue is not None and status == "open" else []
    store.list_by_milestone.return_value = [] if issue is None else [issue]
    store.known_modules.return_value = {"sf", "gmail", "other", "watch", "pursuit", "cli"}
    return store


def _parses_as_json(text: str) -> bool:
    """Prose output opens with `[issue] ...`, so a `[`-prefix check would call it
    JSON. Only an actual parse distinguishes the two renderings."""
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _invoke(argv: list[str], issue: GHIssue | None) -> tuple[Result, MagicMock]:
    # TC-013: `_store` is the private seam every subcommand calls to reach GitHub;
    # it is the only boundary that stops these tests hitting the live API, and the
    # sibling issue-CLI test modules patch the same one.
    store = _mock_store(issue)
    with (
        patch("fieldkit.commands.issue.cli._store", return_value=store),
        patch("fieldkit.commands.issue.cli.get_github_repo", return_value=REPO),
    ):
        result = CliRunner().invoke(cli, argv)
    return result, store


# Every subcommand, with the issue status each needs to reach its success path
# and the top-level keys its document must carry.
_SUCCESS_CASES = [
    pytest.param(["create", "--type", "bug", "--title", "t", "--module", "sf"], "open", {"id", "url"}, id="create"),
    pytest.param(["list"], "open", {"items", "count", "filters"}, id="list"),
    pytest.param(["show", "historic regression"], "open", {"id", "url"}, id="show"),
    pytest.param(["close", "historic regression", "--skip-verify"], "open", {"id", "status"}, id="close"),
    pytest.param(["reopen", "historic regression"], "closed", {"id", "status"}, id="reopen"),
    pytest.param(["fix", "historic regression"], "open", {"id", "commit"}, id="fix"),
    pytest.param(["plan", "historic regression"], "open", {"id", "status"}, id="plan"),
    pytest.param(["edit", "historic regression", "--title", "new"], "open", {"id", "title"}, id="edit"),
    pytest.param(["note", "historic regression", "a note"], "open", {"id"}, id="note"),
    pytest.param(["link", "historic regression", "M001"], "open", {"id", "milestone"}, id="link"),
    pytest.param(
        ["sync-milestone", "M001", "--state", "queued"],
        "open",
        {"milestone", "state", "advanced", "candidates"},
        id="sync-milestone",
    ),
    pytest.param(["board"], "open", {"counts", "bugs", "enhancements"}, id="board"),
]


# ---------------------------------------------------------------------------
# The --json contract, across every subcommand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("argv", "status", "expected_keys"), _SUCCESS_CASES)
def test_json_flag_emits_parseable_document_on_stdout(argv: list[str], status: str, expected_keys: set[str]) -> None:
    result, _store = _invoke([*argv, "--json"], _make_issue(status=status))

    assert result.exit_code == 0, result.output
    assert result.stderr == ""

    payload = json.loads(result.stdout)
    assert expected_keys <= set(payload)


@pytest.mark.parametrize(("argv", "status", "expected_keys"), _SUCCESS_CASES)
def test_without_json_flag_output_stays_prose(argv: list[str], status: str, expected_keys: set[str]) -> None:
    """The default rendering is untouched — --json is opt-in, never the default."""
    result, _store = _invoke(argv, _make_issue(status=status))

    assert result.exit_code == 0, result.output
    assert not _parses_as_json(result.stdout)


# Error paths: --json must not change the exit code, and must leave stdout empty
# so that "exit 0 ⇒ parseable stdout" holds for a machine consumer.
_ERROR_CASES = [
    pytest.param(["show", "historic regression"], None, 3, id="show-missing"),
    pytest.param(["reopen", "historic regression"], None, 3, id="reopen-missing"),
    pytest.param(["note", "historic regression", "text"], None, 3, id="note-missing"),
    pytest.param(["link", "historic regression", "M001"], None, 3, id="link-missing"),
    pytest.param(["edit", "historic regression", "--title", "x"], None, 3, id="edit-missing"),
    pytest.param(["close", "historic regression"], "open", 1, id="close-unverified"),
    pytest.param(["fix", "historic regression"], "closed", 1, id="fix-already-closed"),
    pytest.param(["plan", "historic regression"], "planned", 1, id="plan-already-planned"),
    pytest.param(
        ["create", "--type", "bug", "--title", "t", "--module", "bogus"], "open", 1, id="create-unknown-module"
    ),
    pytest.param(["edit", "historic regression", "--module", "bogus"], "open", 1, id="edit-unknown-module"),
]


@pytest.mark.parametrize(("argv", "status", "expected_exit"), _ERROR_CASES)
def test_json_flag_preserves_error_exit_codes(argv: list[str], status: str | None, expected_exit: int) -> None:
    issue = None if status is None else _make_issue(status=status)

    result, _store = _invoke([*argv, "--json"], issue)

    assert result.exit_code == expected_exit, result.output
    assert result.stdout == ""
    assert result.stderr != ""


@pytest.mark.parametrize(("argv", "status", "expected_exit"), _ERROR_CASES)
def test_error_exit_codes_match_without_json(argv: list[str], status: str | None, expected_exit: int) -> None:
    """The same failures exit the same way without --json (historic regression class guard)."""
    issue = None if status is None else _make_issue(status=status)

    result, _store = _invoke(argv, issue)

    assert result.exit_code == expected_exit, result.output


# ---------------------------------------------------------------------------
# Payload contents worth asserting beyond shape
# ---------------------------------------------------------------------------


def test_list_json_carries_every_issue_and_the_filters() -> None:
    issues = [
        _make_issue("historic regression", severity="low", gh_number=1),
        _make_issue("historic regression", severity="critical"),
    ]
    store = _mock_store(None)
    store.list_issues.side_effect = None
    store.list_issues.return_value = issues

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = CliRunner().invoke(cli, ["list", "--module", "sf", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["filters"] == {"status": "open", "type": "all", "module": "sf"}
    # Sorted by severity, as the table is: critical before low.
    assert [i["id"] for i in payload["items"]] == ["historic regression", "historic regression"]


def test_show_json_includes_the_github_url() -> None:
    result, _store = _invoke(["show", "historic regression", "--json"], _make_issue(gh_number=42))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url"] == f"https://github.com/{REPO}/issues/42"
    assert payload["id"] == "historic regression"


def test_fix_json_records_the_commit_sha() -> None:
    result, store = _invoke(["fix", "historic regression", "--commit", "abc1234", "--json"], _make_issue(status="open"))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["commit"] == "abc1234"
    store.mark_fixed.assert_called_once_with("historic regression", commit="abc1234", note=None)


def test_reopen_already_open_still_emits_a_document() -> None:
    """Exit 0 with empty stdout would break any consumer that parses on success."""
    result, store = _invoke(["reopen", "historic regression", "--json"], _make_issue(status="open"))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "open"
    store.update_status.assert_not_called()


def test_board_json_reports_counts_and_splits_by_type() -> None:
    bug = _make_issue("historic regression", gh_number=1)
    enh = _make_issue("implementation change", issue_type="enhancement", gh_number=10)
    store = _mock_store(None)
    store.list_issues.side_effect = lambda status, **kw: [bug, enh] if status == "open" else []

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = CliRunner().invoke(cli, ["board", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["counts"]["open"] == 2
    assert [i["id"] for i in payload["bugs"]] == ["historic regression"]
    assert [i["id"] for i in payload["enhancements"]] == ["implementation change"]


def test_sync_milestone_json_reports_what_advanced() -> None:
    result, store = _invoke(["sync-milestone", "M001", "--state", "queued", "--json"], _make_issue(status="open"))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["advanced"] == 1
    assert payload["failed"] == []
    assert payload["target_status"] == "planned"
    assert payload["milestone"] == "M001"
    store.update_status.assert_called_once()


def test_sync_milestone_dry_run_json_advances_nothing() -> None:
    result, store = _invoke(
        ["sync-milestone", "M001", "--state", "queued", "--dry-run", "--json"], _make_issue(status="open")
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["advanced"] == 0
    assert len(payload["candidates"]) == 1
    store.update_status.assert_not_called()


def test_sync_milestone_json_reports_failed_updates() -> None:
    store = _mock_store(_make_issue(status="open"))
    store.update_status.return_value = None

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = CliRunner().invoke(cli, ["sync-milestone", "M001", "--state", "queued", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["failed"] == ["historic regression"]
    assert payload["advanced"] == 0


def test_sync_milestone_json_partial_success_still_emits_the_document_on_exit_1() -> None:
    """Partial success exits 1 *and* leaves the full document on stdout.

    This is the documented exception to the --json error contract described in
    this module's docstring: `sync-milestone` is the one command whose stdout a
    consumer parses on exit 1 as well as exit 0, because the advanced/failed
    breakdown is exactly what it needs to recover.

    Pinned because the obvious refactor — raising as soon as a failure is seen,
    or returning early — produces the right exit code with an empty stdout, and
    every other assertion in this file would still pass.
    """
    advanced_issue = _make_issue(issue_id="historic regression", status="open")
    doomed_issue = _make_issue(issue_id="historic regression", status="open", gh_number=43)

    store = _mock_store(advanced_issue)
    store.list_by_milestone.return_value = [advanced_issue, doomed_issue]
    # First candidate advances, second fails to write.
    store.update_status.side_effect = [advanced_issue, None]

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = CliRunner().invoke(cli, ["sync-milestone", "M001", "--state", "queued", "--json"])

    assert result.exit_code == 1, result.output
    assert result.stdout.strip(), "partial success must still emit the document on stdout"
    payload = json.loads(result.stdout)
    assert payload["advanced"] == 1
    assert payload["failed"] == ["historic regression"]


def test_sync_milestone_json_when_linked_issues_are_all_past_the_target() -> None:
    # Linked, but already fixed — nothing for a `queued` sync to advance.
    store = _mock_store(_make_issue(status="fixed"))

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = CliRunner().invoke(cli, ["sync-milestone", "M001", "--state", "queued", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["linked"] == 1
    assert payload["skipped"] == 1
    assert payload["candidates"] == []
    store.update_status.assert_not_called()


def test_sync_milestone_prose_when_linked_issues_are_all_past_the_target() -> None:
    store = _mock_store(_make_issue(status="fixed"))

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = CliRunner().invoke(cli, ["sync-milestone", "M001", "--state", "queued"])

    assert result.exit_code == 0, result.output
    assert "Nothing to do." in result.stdout
    assert "1 skipped (already at target or beyond)" in result.stdout


def test_sync_milestone_json_when_no_issues_are_linked() -> None:
    store = _mock_store(None)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = CliRunner().invoke(cli, ["sync-milestone", "M999", "--state", "queued", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["linked"] == 0
    assert payload["candidates"] == []


def test_list_json_when_no_issues_match() -> None:
    store = _mock_store(None)

    with patch("fieldkit.commands.issue.cli._store", return_value=store):
        result = CliRunner().invoke(cli, ["list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["items"] == []
