"""Unit tests for the canonical GitHub-backed issue store.

Provider failures propagate instead of becoming successful empty query results.
"""

from unittest.mock import MagicMock, patch

import pytest

from fieldkit.errors import GitHubDataError, GitHubRequestError
from fieldkit.issue import GHIssueStore
from fieldkit.issue.github import _gh_json

pytestmark = pytest.mark.unit

REPO = "owner/test-repo"


# ---------------------------------------------------------------------------
# _gh_json — typed provider failures
# ---------------------------------------------------------------------------


def test_gh_json_raises_provider_error_on_invalid_json() -> None:
    """Invalid JSON raises the typed provider failure."""
    bad_stdout = "not valid json {"

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = bad_stdout
    fake_result.stderr = ""

    with (
        patch("fieldkit.issue.github.subprocess.run", return_value=fake_result),
        pytest.raises(GitHubDataError, match="invalid JSON") as exc_info,
    ):
        _gh_json("issue", "list", "--repo", REPO)

    assert exc_info.value.__cause__ is None


def test_gh_json_error_does_not_include_provider_payload() -> None:
    """The typed diagnostic contains no malformed provider content."""
    bad_stdout = "not valid json {"

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = bad_stdout
    fake_result.stderr = ""

    with (
        patch("fieldkit.issue.github.subprocess.run", return_value=fake_result),
        pytest.raises(GitHubDataError, match="invalid JSON") as error,
    ):
        _gh_json("issue", "list", "--repo", REPO)
    assert bad_stdout not in str(error.value)


def test_gh_json_returns_parsed_data_on_valid_json() -> None:
    """_gh_json() must return parsed data when gh returns valid JSON."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '[{"number": 1, "title": "historic regression: test"}]'
    fake_result.stderr = ""

    with patch("fieldkit.issue.github.subprocess.run", return_value=fake_result):
        result = _gh_json("issue", "list", "--repo", REPO)

    assert result == [{"number": 1, "title": "historic regression: test"}]


def test_gh_json_rejects_empty_stdout() -> None:
    """An empty stdout is not a valid JSON empty list."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = ""
    fake_result.stderr = ""

    with (
        patch("fieldkit.issue.github.subprocess.run", return_value=fake_result),
        pytest.raises(GitHubDataError, match="invalid JSON"),
    ):
        _gh_json("issue", "list", "--repo", REPO)


# ---------------------------------------------------------------------------
# list_issues — provider failures propagate
# ---------------------------------------------------------------------------


def test_list_issues_propagates_invalid_json() -> None:
    """A malformed response cannot produce a successful empty query."""
    bad_stdout = "not valid json {"

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = bad_stdout
    fake_result.stderr = ""

    store = GHIssueStore(REPO)
    with (
        patch("fieldkit.issue.github.subprocess.run", return_value=fake_result),
        pytest.raises(GitHubDataError, match="invalid JSON"),
    ):
        store.list_issues()


def test_list_issues_propagates_gh_cli_failure() -> None:
    """A failed request cannot produce a successful empty query."""
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "gh: command not found"

    store = GHIssueStore(REPO)
    with (
        patch("fieldkit.issue.github.subprocess.run", return_value=fake_result),
        pytest.raises(GitHubRequestError, match="request failed"),
    ):
        store.list_issues()
