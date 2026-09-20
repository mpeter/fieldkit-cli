"""Unit tests for fieldkit/commands/issue/gh_store.py.

Focus: _gh_json() JSONDecodeError handling (implementation note) and the downstream
list_issues() graceful-empty-result path.
"""

from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.issue import gh_store
from fieldkit.commands.issue.gh_store import GHIssueStore, _gh_json

pytestmark = pytest.mark.unit

REPO = "owner/test-repo"


# ---------------------------------------------------------------------------
# _gh_json — JSONDecodeError → RuntimeError (implementation note)
# ---------------------------------------------------------------------------


def test_gh_json_raises_runtime_error_on_invalid_json() -> None:
    """_gh_json() must raise RuntimeError with 'invalid JSON' when gh returns garbage."""
    bad_stdout = "not valid json {"

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = bad_stdout
    fake_result.stderr = ""

    with (
        patch.object(gh_store.subprocess, "run", return_value=fake_result),
        pytest.raises(RuntimeError, match="invalid JSON") as exc_info,
    ):
        _gh_json("issue", "list", "--repo", REPO)

    # The original JSONDecodeError must be chained
    assert exc_info.value.__cause__ is not None


def test_gh_json_includes_length_in_error_message() -> None:
    """RuntimeError message must include the byte length of the bad payload."""
    bad_stdout = "not valid json {"

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = bad_stdout
    fake_result.stderr = ""

    with (
        patch.object(gh_store.subprocess, "run", return_value=fake_result),
        pytest.raises(RuntimeError, match=rf"len={len(bad_stdout)}"),
    ):
        _gh_json("issue", "list", "--repo", REPO)


def test_gh_json_returns_parsed_data_on_valid_json() -> None:
    """_gh_json() must return parsed data when gh returns valid JSON."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '[{"number": 1, "title": "historic regression: test"}]'
    fake_result.stderr = ""

    with patch.object(gh_store.subprocess, "run", return_value=fake_result):
        result = _gh_json("issue", "list", "--repo", REPO)

    assert result == [{"number": 1, "title": "historic regression: test"}]


def test_gh_json_returns_empty_list_on_empty_stdout() -> None:
    """_gh_json() must return [] when gh produces no output (nothing to list)."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = ""
    fake_result.stderr = ""

    with patch.object(gh_store.subprocess, "run", return_value=fake_result):
        result = _gh_json("issue", "list", "--repo", REPO)

    assert result == []


# ---------------------------------------------------------------------------
# list_issues — invalid JSON → graceful [] (implementation note)
# ---------------------------------------------------------------------------


def test_list_issues_returns_empty_on_invalid_json() -> None:
    """list_issues() must return [] (not crash) when gh returns invalid JSON.

    The RuntimeError raised by _gh_json() is caught by list_issues()'s existing
    RuntimeError handler, so the caller sees an empty list rather than a traceback.
    """
    bad_stdout = "not valid json {"

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = bad_stdout
    fake_result.stderr = ""

    store = GHIssueStore(REPO)
    with patch.object(gh_store.subprocess, "run", return_value=fake_result):
        result = store.list_issues()

    assert result == []


def test_list_issues_returns_empty_on_gh_cli_failure() -> None:
    """list_issues() must return [] when gh CLI exits non-zero (existing behaviour)."""
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "gh: command not found"

    store = GHIssueStore(REPO)
    with patch.object(gh_store.subprocess, "run", return_value=fake_result):
        result = store.list_issues()

    assert result == []
