"""Unit tests for fieldkit/commands/issue/gh_store.py.

All gh CLI calls are mocked via subprocess.run — no live GitHub API calls.
Tests cover: find, list_issues, create, update_status, mark_fixed, add_note, edit.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.issue.gh_store import (
    GHIssueStore,
    _issue_from_gh,
    _parse_fieldkit_id,
    _status_from_github,
)

pytestmark = pytest.mark.unit

REPO = "owner/test-repo"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OPEN_ISSUE = {
    "number": 42,
    "title": "fieldkit-007: something is broken",
    "state": "open",
    "stateReason": None,
    "labels": [
        {"name": "bug"},
        {"name": "severity:high"},
        {"name": "module:sf"},
    ],
    "body": "> **Source:** test-agent | **Created:** 2026-01-15\n\nThis is broken.",
    "createdAt": "2026-01-15T10:00:00Z",
}

_CLOSED_COMPLETED = {
    "number": 10,
    "title": "fieldkit-003: add a feature",
    "state": "closed",
    "stateReason": "completed",
    "labels": [
        {"name": "enhancement"},
        {"name": "severity:low"},
        {"name": "module:gmail"},
    ],
    "body": "> **Source:** contributor | **Created:** 2026-02-01\n\nFeature request.",
    "createdAt": "2026-02-01T09:00:00Z",
}

_CLOSED_NOT_PLANNED = {
    "number": 20,
    "title": "fieldkit-012: wont fix this",
    "state": "closed",
    "stateReason": "not_planned",
    "labels": [
        {"name": "bug"},
        {"name": "severity:low"},
        {"name": "module:other"},
    ],
    "body": "> **Source:** unknown | **Created:** 2026-03-01\n\nNot fixing this.",
    "createdAt": "2026-03-01T08:00:00Z",
}

_FIXED_ISSUE = {
    "number": 33,
    "title": "fieldkit-099: fixed but not verified",
    "state": "closed",
    "stateReason": "completed",
    "labels": [
        {"name": "bug"},
        {"name": "severity:medium"},
        {"name": "module:watch"},
        {"name": "status:fixed"},
    ],
    "body": "> **Source:** agent | **Created:** 2026-04-01\n\nFixed in main.",
    "createdAt": "2026-04-01T07:00:00Z",
}

_PLANNED_ISSUE = {
    "number": 55,
    "title": "fieldkit-011: planned enhancement",
    "state": "open",
    "stateReason": None,
    "labels": [
        {"name": "enhancement"},
        {"name": "severity:medium"},
        {"name": "module:pipeline"},
        {"name": "status:planned"},
    ],
    "body": "> **Source:** pm | **Created:** 2026-05-01\n\nPlanned.",
    "createdAt": "2026-05-01T06:00:00Z",
}


def _make_store() -> GHIssueStore:
    return GHIssueStore(REPO)


def _mock_gh(stdout: str = "[]", returncode: int = 0, stderr: str = "") -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


# ---------------------------------------------------------------------------
# _parse_fieldkit_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_fieldkit_id_is_lowercase_public_key() -> None:
    assert _parse_fieldkit_id("fieldkit-042: some title") == "fieldkit-042"


@pytest.mark.unit
def test_parse_fieldkit_id_does_not_encode_issue_type() -> None:
    assert _parse_fieldkit_id("fieldkit-017: a feature") == "fieldkit-017"


@pytest.mark.unit
def test_parse_fieldkit_id_no_match() -> None:
    assert _parse_fieldkit_id("Some random title") is None


@pytest.mark.unit
def test_parse_fieldkit_id_preserves_zero_padding() -> None:
    assert _parse_fieldkit_id("fieldkit-001: first issue") == "fieldkit-001"


# ---------------------------------------------------------------------------
# _status_from_github
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_open_uppercase() -> None:
    """GitHub returns 'OPEN' (uppercase) — must map to open status."""
    assert _status_from_github("OPEN", None, []) == "open"


@pytest.mark.unit
def test_status_open_lowercase() -> None:
    """Lowercase 'open' is accepted for backward compatibility."""
    assert _status_from_github("open", None, []) == "open"


@pytest.mark.unit
def test_status_planned_uppercase() -> None:
    """GitHub 'OPEN' plus the planned label maps to planned."""
    assert _status_from_github("OPEN", None, ["status:planned"]) == "planned"


@pytest.mark.unit
def test_status_planned() -> None:
    assert _status_from_github("open", None, ["status:planned"]) == "planned"


@pytest.mark.unit
def test_status_wont_fix() -> None:
    assert _status_from_github("closed", "not_planned", []) == "wont-fix"


@pytest.mark.unit
def test_status_wont_fix_uppercase() -> None:
    """GitHub 'CLOSED' (uppercase) with not_planned maps to wont-fix."""
    assert _status_from_github("CLOSED", "not_planned", []) == "wont-fix"


@pytest.mark.unit
def test_status_fixed() -> None:
    assert _status_from_github("closed", "completed", ["status:fixed"]) == "fixed"


@pytest.mark.unit
def test_status_closed() -> None:
    assert _status_from_github("closed", "completed", []) == "closed"


# ---------------------------------------------------------------------------
# _issue_from_gh
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_issue_from_gh_open() -> None:
    issue = _issue_from_gh(_OPEN_ISSUE)
    assert issue is not None
    assert issue.id == "fieldkit-007"
    assert issue.type == "bug"
    assert issue.title == "something is broken"
    assert issue.status == "open"
    assert issue.severity == "high"
    assert issue.module == "sf"
    assert issue.gh_number == 42
    assert issue.source == "test-agent"


@pytest.mark.unit
def test_issue_from_gh_ignores_public_key_without_type_label() -> None:
    issue = _issue_from_gh({**_OPEN_ISSUE, "labels": [{"name": "severity:high"}]})

    assert issue is None


@pytest.mark.unit
def test_issue_from_gh_closed_completed() -> None:
    issue = _issue_from_gh(_CLOSED_COMPLETED)
    assert issue is not None
    assert issue.status == "closed"
    assert issue.type == "enhancement"


@pytest.mark.unit
def test_issue_from_gh_wont_fix() -> None:
    issue = _issue_from_gh(_CLOSED_NOT_PLANNED)
    assert issue is not None
    assert issue.status == "wont-fix"


@pytest.mark.unit
def test_issue_from_gh_fixed() -> None:
    issue = _issue_from_gh(_FIXED_ISSUE)
    assert issue is not None
    assert issue.status == "fixed"


@pytest.mark.unit
def test_issue_from_gh_planned() -> None:
    issue = _issue_from_gh(_PLANNED_ISSUE)
    assert issue is not None
    assert issue.status == "planned"


@pytest.mark.unit
def test_issue_from_gh_non_fieldkit_returns_none() -> None:
    data = {**_OPEN_ISSUE, "title": "Some unrelated issue"}
    assert _issue_from_gh(data) is None


# ---------------------------------------------------------------------------
# GHIssueStore.find
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_returns_matching_issue() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh(json.dumps([_OPEN_ISSUE]))
        result = store.find("fieldkit-007")
    assert result is not None
    assert result.id == "fieldkit-007"
    assert result.gh_number == 42


@pytest.mark.unit
def test_find_returns_none_when_not_found() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("[]")
        result = store.find("fieldkit-999")
    assert result is None


# ---------------------------------------------------------------------------
# GHIssueStore.list_issues
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_open_filters_by_status() -> None:
    store = _make_store()
    # Return one open and one closed-completed; list_issues(status="open") should filter
    items = [_OPEN_ISSUE, _CLOSED_COMPLETED]
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh(json.dumps(items))
        result = store.list_issues(status="open")
    assert all(i.status == "open" for i in result)


@pytest.mark.unit
def test_list_all_returns_all() -> None:
    store = _make_store()
    items = [_OPEN_ISSUE, _CLOSED_COMPLETED, _CLOSED_NOT_PLANNED]
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh(json.dumps(items))
        result = store.list_issues(status="all")
    assert len(result) == 3


@pytest.mark.unit
def test_list_gh_error_returns_empty() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("", returncode=1)
        result = store.list_issues(status="open")
    assert result == []


# ---------------------------------------------------------------------------
# GHIssueStore._get_watermark / _set_watermark / next_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_watermark_returns_none_when_label_missing() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("", returncode=1, stderr="gh: Not Found (HTTP 404)")
        result = store._get_watermark()
    assert result is None


@pytest.mark.unit
def test_get_watermark_parses_valid_int() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("42")
        result = store._get_watermark()
    assert result == 42


@pytest.mark.unit
def test_get_watermark_returns_none_for_non_numeric_description() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("unset")
        result = store._get_watermark()
    assert result is None


@pytest.mark.unit
def test_get_watermark_reraises_non_404_errors() -> None:
    store = _make_store()
    with (
        patch("subprocess.run") as mock_run,
        pytest.raises(RuntimeError, match="failed"),
    ):
        mock_run.return_value = _mock_gh("", returncode=1, stderr="Internal Server Error")
        store._get_watermark()


@pytest.mark.unit
def test_set_watermark_patches_existing_label() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("")
        store._set_watermark(5)
    argvs = _argvs(mock_run)
    assert len(argvs) == 1
    assert "PATCH" in argvs[0]
    assert "description=5" in argvs[0]


@pytest.mark.unit
def test_set_watermark_creates_label_when_patch_404s() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _mock_gh("", returncode=1, stderr="gh: Not Found (HTTP 404)"),
            _mock_gh(""),
        ]
        store._set_watermark(5)
    argvs = _argvs(mock_run)
    assert len(argvs) == 2
    assert "PATCH" in argvs[0]
    assert "POST" in argvs[1]
    assert "description=5" in argvs[1]


@pytest.mark.unit
def test_set_watermark_reraises_non_404_patch_errors() -> None:
    store = _make_store()
    with (
        patch("subprocess.run") as mock_run,
        pytest.raises(RuntimeError, match="failed"),
    ):
        mock_run.return_value = _mock_gh("", returncode=1, stderr="Internal Server Error")
        store._set_watermark(5)
    assert len(_argvs(mock_run)) == 1, "must not attempt to create the label after a non-404 failure"


@pytest.mark.unit
def test_next_id_uses_watermark_without_rescanning_titles() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh("5"), _mock_gh("")]
        result = store.next_id("bug")
    assert result == "fieldkit-006"
    argvs = _argvs(mock_run)
    assert len(argvs) == 2, "must not touch the title-scan endpoint once a watermark exists"
    assert not any("state=all" in argv for argv in argvs)


@pytest.mark.unit
def test_next_id_bootstraps_from_title_scan_when_no_watermark() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _mock_gh(""),  # no watermark yet
            _mock_gh(json.dumps(["fieldkit-001: existing", "fieldkit-005: other"])),
            _mock_gh(""),  # persist watermark -> 6
        ]
        result = store.next_id("enhancement")
    assert result == "fieldkit-006"


@pytest.mark.unit
def test_next_id_key_not_reissued_after_issue_retitled() -> None:
    """Once a key is allocated, retitling the issue that
    held it must not free it for reissue.

    Simulates two points in time against the same repo:
      1. First allocation: no watermark yet, so next_id() bootstraps from a
         title scan that sees the highest live title "fieldkit-005: ...", and
         allocates and persists fieldkit-006.
      2. A later allocation, *after* the issue holding "fieldkit-006" has been
         retitled away (e.g. consolidated into a duplicate during triage).
         A title scan at this point would no longer find fieldkit-006 anywhere,
         and the pre-fix scan-only implementation would be fooled into
         reissuing it. The watermark-based implementation must instead read
         the persisted value and allocate fieldkit-007 without rescanning.
    """
    store = _make_store()

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _mock_gh("", returncode=1, stderr="gh: Not Found (HTTP 404)"),
            _mock_gh(json.dumps(["fieldkit-005: something is broken"])),
            _mock_gh(""),
        ]
        first_id = store.next_id("bug")
    assert first_id == "fieldkit-006"

    # The watermark label now persists "6" (written above). A title scan at
    # this point would no longer see fieldkit-006 anywhere, but next_id() must
    # not rescan at all once a watermark exists.
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh("6"), _mock_gh("")]
        second_id = store.next_id("bug")

    assert second_id == "fieldkit-007", "watermark must not be fooled by a retitled issue"
    argvs = _argvs(mock_run)
    assert len(argvs) == 2, "must not rescan issue titles once a watermark exists"
    assert not any("state=all" in argv for argv in argvs)


# ---------------------------------------------------------------------------
# GHIssueStore.create
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_returns_gh_issue() -> None:
    store = _make_store()
    watermark_response = _mock_gh("1")  # persisted watermark
    patch_response = _mock_gh("")  # persist watermark -> 2
    create_response = _mock_gh("43")  # REST API --jq .number returns just the number

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [watermark_response, patch_response, create_response]
        issue = store.create(
            issue_type="bug",
            title="new bug",
            body="desc",
            severity="high",
            module="sf",
            source="test",
        )

    assert issue.id == "fieldkit-002"
    assert issue.title == "new bug"
    assert issue.gh_number == 43
    assert issue.status == "open"


@pytest.mark.unit
def test_create_enh_gets_public_fieldkit_id() -> None:
    store = _make_store()
    # No watermark yet (label exists but has no valid int description), so
    # next_id() bootstraps from an (empty) title scan.
    watermark_response = _mock_gh("")
    scan_response = _mock_gh("[]")
    patch_response = _mock_gh("")  # persist watermark -> 1
    create_response = _mock_gh("100")  # REST API --jq .number returns just the number

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [watermark_response, scan_response, patch_response, create_response]
        issue = store.create(
            issue_type="enhancement",
            title="new feature",
            severity="low",
            module="gmail",
            source="user",
        )

    assert issue.id == "fieldkit-001"


# ---------------------------------------------------------------------------
# GHIssueStore.mark_fixed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mark_fixed_closes_with_completed_and_label() -> None:
    store = _make_store()
    find_response = _mock_gh(json.dumps([_OPEN_ISSUE]))
    close_response = _mock_gh("")
    label_response = _mock_gh("")
    comment_response = _mock_gh("")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [find_response, close_response, label_response, comment_response]
        result = store.mark_fixed("fieldkit-007", commit="abc1234", note="Tests pass")

    assert result is not None
    assert result.status == "fixed"
    # Verify close was called with "completed"
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("completed" in c for c in calls)


@pytest.mark.unit
def test_mark_fixed_returns_none_when_not_found() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("[]")
        result = store.mark_fixed("fieldkit-999")
    assert result is None


# ---------------------------------------------------------------------------
# GHIssueStore.update_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_update_status_wont_fix_uses_not_planned() -> None:
    store = _make_store()
    find_response = _mock_gh(json.dumps([_OPEN_ISSUE]))
    close_response = _mock_gh("")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [find_response, close_response]
        result = store.update_status("fieldkit-007", "wont-fix")

    assert result is not None
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("not planned" in c for c in calls)


@pytest.mark.unit
def test_update_status_open_reopens_issue() -> None:
    store = _make_store()
    find_response = _mock_gh(json.dumps([_CLOSED_COMPLETED]))
    reopen_response = _mock_gh("")
    edit_response = _mock_gh("")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [find_response, reopen_response, edit_response]
        result = store.update_status("fieldkit-003", "open")

    assert result is not None
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("reopen" in c for c in calls)


# ---------------------------------------------------------------------------
# GHIssueStore.add_note
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_add_note_posts_comment() -> None:
    store = _make_store()
    find_response = _mock_gh(json.dumps([_OPEN_ISSUE]))
    comment_response = _mock_gh("")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [find_response, comment_response]
        result = store.add_note("fieldkit-007", "Verified on staging")

    assert result is not None
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("comment" in c for c in calls)
    assert any("Verified on staging" in c for c in calls)


@pytest.mark.unit
def test_add_note_returns_none_when_not_found() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("[]")
        result = store.add_note("fieldkit-999", "note text")
    assert result is None


# ---------------------------------------------------------------------------
# GHIssueStore.edit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_edit_updates_title() -> None:
    store = _make_store()
    find_response = _mock_gh(json.dumps([_OPEN_ISSUE]))
    edit_response = _mock_gh("")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [find_response, edit_response]
        result = store.edit("fieldkit-007", title="new title")

    assert result is not None
    assert result.title == "new title"
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("fieldkit-007: new title" in c for c in calls)


@pytest.mark.unit
def test_edit_returns_none_when_not_found() -> None:
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("[]")
        result = store.edit("fieldkit-999", title="x")
    assert result is None


# ---------------------------------------------------------------------------
# GHIssueStore.known_modules
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_known_modules_contains_expected() -> None:
    store = _make_store()
    modules = store.known_modules()
    assert modules == {
        "auth",
        "brief",
        "cli",
        "companion",
        "config",
        "contact",
        "docs",
        "doctor",
        "driver",
        "enrich",
        "gmail",
        "health",
        "hooks",
        "ingest",
        "init",
        "issue",
        "lib",
        "meeting",
        "other",
        "pipeline",
        "pursuit",
        "sf",
        "shadowbot",
        "skill",
        "sync",
        "version",
        "watch",
        "web",
    }


# ---------------------------------------------------------------------------
# 4D.1 severity_rank
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_severity_rank_known_severity() -> None:
    """severity_rank returns 1 for 'high' severity."""
    import datetime

    from fieldkit.commands.issue.gh_store import GHIssue

    issue = GHIssue(
        id="fieldkit-001",
        type="bug",
        title="Test issue",
        status="open",
        severity="high",
        module="sf",
        gh_number=1,
        created=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    assert issue.severity_rank == 1


@pytest.mark.unit
def test_severity_rank_unknown_defaults_to_99() -> None:
    """severity_rank returns 99 for unrecognised severity."""

    from fieldkit.commands.issue.gh_store import _SEVERITY_RANK

    # Temporarily test via the dict directly since IssueSeverity is a Literal
    assert _SEVERITY_RANK.get("unknown", 99) == 99
    assert _SEVERITY_RANK.get("critical") == 0
    assert _SEVERITY_RANK.get("medium") == 2


# ---------------------------------------------------------------------------
# 4D.2 count_issues
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_count_issues_returns_correct_counts() -> None:
    """count_issues returns a dict with the right key names and integer values."""
    from unittest.mock import patch

    store = _make_store()

    with patch.object(store, "list_issues") as mock_list:
        mock_list.return_value = [
            _issue_from_gh(
                {
                    **_OPEN_ISSUE,
                    "labels": [{"name": "bug"}, {"name": "severity:high"}, {"name": "module:sf"}],
                    "state": "open",
                }
            ),
        ]
        result = store.count_issues()

    assert isinstance(result, dict)
    assert "open_bugs" in result
    assert "open_enhancements" in result
    assert "closed" in result
    assert "wontfix" in result
    assert all(isinstance(v, int) for v in result.values())


# ---------------------------------------------------------------------------
# GHIssueStore.update_status — remaining transitions
# GHIssueStore.list_by_milestone / link_milestone
#
# update_status sat at 57.1% and the two milestone methods at 0% while
# gh_store.py around them read higher. gaze-py <=0.8.2 applied that file
# aggregate to every function in it, so none was flagged; gaze-py 0.9.0
# attributes coverage per function and surfaced them carrying 87.4 CRAP.
#
# _gh() builds argv as ["gh", *args] and passes it positionally, so each call's
# argv is call.args[0].
# ---------------------------------------------------------------------------


def _argvs(mock_run: MagicMock) -> list[list[str]]:
    """Every argv list passed to subprocess.run, in call order."""
    return [call.args[0] for call in mock_run.call_args_list]


def _flat(mock_run: MagicMock) -> str:
    """All argv tokens joined, for coarse 'was this flag used anywhere' checks."""
    return " ".join(tok for argv in _argvs(mock_run) for tok in argv)


@pytest.mark.unit
def test_update_status_returns_none_when_issue_not_found() -> None:
    """An unknown issue id is reported as None rather than raising."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("[]")
        result = store.update_status("fieldkit-999", "closed")

    assert result is None
    assert len(_argvs(mock_run)) == 1, "must not attempt any write after a failed lookup"


@pytest.mark.unit
def test_update_status_planned_reopens_a_closed_issue_first() -> None:
    """Planning a closed issue must reopen it, or the label would land on a closed issue."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh(json.dumps([_CLOSED_COMPLETED])), _mock_gh(""), _mock_gh("")]
        result = store.update_status("fieldkit-003", "planned")

    assert result is not None
    assert result.status == "planned"
    flat = _flat(mock_run)
    assert "reopen" in flat
    assert "--add-label" in flat
    assert "status:planned" in flat


@pytest.mark.unit
def test_update_status_planned_does_not_reopen_an_already_open_issue() -> None:
    """An open issue is already in the right state; reopening it would be a redundant API write."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh(json.dumps([_OPEN_ISSUE])), _mock_gh("")]
        result = store.update_status("fieldkit-007", "planned")

    assert result is not None
    assert "reopen" not in _flat(mock_run)


@pytest.mark.unit
def test_update_status_fixed_closes_as_completed_and_labels() -> None:
    """'fixed' is a completed close plus the status:fixed label that distinguishes it from 'closed'."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh(json.dumps([_OPEN_ISSUE])), _mock_gh(""), _mock_gh("")]
        result = store.update_status("fieldkit-007", "fixed")

    assert result is not None
    assert result.status == "fixed"
    flat = _flat(mock_run)
    assert "close" in flat
    assert "completed" in flat
    assert "status:fixed" in flat


@pytest.mark.unit
def test_update_status_closed_does_not_add_the_fixed_label() -> None:
    """'closed' and 'fixed' differ only by that label, so closed must not acquire it."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh(json.dumps([_OPEN_ISSUE])), _mock_gh("")]
        result = store.update_status("fieldkit-007", "closed")

    assert result is not None
    flat = _flat(mock_run)
    assert "completed" in flat
    assert "status:fixed" not in flat


@pytest.mark.unit
def test_update_status_posts_the_note_as_a_comment() -> None:
    """A note accompanying a transition is recorded on the issue."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh(json.dumps([_OPEN_ISSUE])), _mock_gh(""), _mock_gh("")]
        result = store.update_status("fieldkit-007", "closed", note="superseded by fieldkit-003")

    assert result is not None
    argvs = _argvs(mock_run)
    assert any("comment" in argv and "superseded by fieldkit-003" in argv for argv in argvs)


# ── list_by_milestone ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_list_by_milestone_matches_the_title_case_insensitively() -> None:
    """Milestone titles are matched case-insensitively, then queried by number."""
    store = _make_store()
    milestones = json.dumps([{"number": 3, "title": "M001"}, {"number": 4, "title": "M002"}])
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh(milestones), _mock_gh(json.dumps([_OPEN_ISSUE]))]
        result = store.list_by_milestone("m001")

    assert len(result) == 1
    assert result[0].id == "fieldkit-007"
    # The issue query must use the resolved number, not the title.
    issue_query = _argvs(mock_run)[1]
    assert issue_query[issue_query.index("--milestone") + 1] == "3"


@pytest.mark.unit
def test_list_by_milestone_returns_empty_when_no_milestone_matches() -> None:
    """An unknown milestone yields no issues, and no issue query is made at all."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh(json.dumps([{"number": 3, "title": "M001"}]))
        result = store.list_by_milestone("M999")

    assert result == []
    assert len(_argvs(mock_run)) == 1, "must not query issues for a milestone that does not exist"


@pytest.mark.unit
def test_list_by_milestone_drops_issues_without_a_fieldkit_id() -> None:
    """Issues in the milestone that are not fieldkit-tracked are filtered out, not returned as None."""
    store = _make_store()
    milestones = json.dumps([{"number": 3, "title": "M001"}])
    items = json.dumps([_OPEN_ISSUE, {**_OPEN_ISSUE, "number": 99, "title": "Some unrelated issue"}])
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh(milestones), _mock_gh(items)]
        result = store.list_by_milestone("M001")

    assert len(result) == 1
    assert all(issue is not None for issue in result)


# ── link_milestone ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_link_milestone_returns_none_when_issue_not_found() -> None:
    """An unknown issue id is reported as None, and no milestone is created."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _mock_gh("[]")
        result = store.link_milestone("fieldkit-999", "M001")

    assert result is None
    assert len(_argvs(mock_run)) == 1, "must not create or query milestones after a failed lookup"


@pytest.mark.unit
def test_link_milestone_reuses_an_existing_milestone() -> None:
    """A milestone that already exists is reused rather than duplicated."""
    store = _make_store()
    milestones = json.dumps([{"number": 7, "title": "M001"}])
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_mock_gh(json.dumps([_OPEN_ISSUE])), _mock_gh(milestones), _mock_gh("")]
        result = store.link_milestone("fieldkit-007", "m001")

    assert result is not None
    assert result.id == "fieldkit-007"
    flat = _flat(mock_run)
    assert "POST" not in flat, "an existing milestone must not be re-created"
    edit = _argvs(mock_run)[-1]
    assert edit[edit.index("--milestone") + 1] == "7"


@pytest.mark.unit
def test_link_milestone_creates_the_milestone_when_absent() -> None:
    """A milestone that does not exist yet is created, then linked by its new number."""
    store = _make_store()
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _mock_gh(json.dumps([_OPEN_ISSUE])),
            _mock_gh(json.dumps([{"number": 7, "title": "M001"}])),
            _mock_gh(json.dumps({"number": 12, "title": "M002"})),
            _mock_gh(""),
        ]
        result = store.link_milestone("fieldkit-007", "M002")

    assert result is not None
    flat = _flat(mock_run)
    assert "POST" in flat
    assert "title=M002" in flat
    edit = _argvs(mock_run)[-1]
    assert edit[edit.index("--milestone") + 1] == "12"


@pytest.mark.unit
def test_link_milestone_posts_the_note_as_a_comment() -> None:
    """A note accompanying the link is recorded on the issue."""
    store = _make_store()
    milestones = json.dumps([{"number": 7, "title": "M001"}])
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _mock_gh(json.dumps([_OPEN_ISSUE])),
            _mock_gh(milestones),
            _mock_gh(""),
            _mock_gh(""),
        ]
        result = store.link_milestone("fieldkit-007", "M001", note="scheduled for the M001 batch")

    assert result is not None
    argvs = _argvs(mock_run)
    assert any("comment" in argv and "scheduled for the M001 batch" in argv for argv in argvs)
