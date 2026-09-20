"""Tests for driver/github.py label writes and idempotent commenting.

``add_label``, ``remove_label`` and ``comment_once`` sat at 0% line coverage
while the file around them read higher. gaze-py <=0.8.2 applied that file
aggregate to every function in it, so none was flagged; gaze-py 0.9.0
attributes coverage per function and surfaced them carrying 70.0 CRAP.

These three are the driver loop's write side against GitHub. The failure modes
worth guarding are not "does it call gh" but "does it call gh with the *right*
flag" (add and remove differ by one argv token) and "does a transient lookup
failure turn into comment spam" (``comment_once`` runs every tick).

``comment_on_issue`` and ``list_ready_issues`` are covered in test_driver.py.
"""

import json
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.driver.github import _GH_TIMEOUT, add_label, comment_once, remove_label

pytestmark = pytest.mark.unit

_RUN = "fieldkit.driver.github.subprocess.run"
_COMMENT = "fieldkit.driver.github.comment_on_issue"


def _ok(cmd: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout, "")


# ---------------------------------------------------------------------------
# add_label / remove_label — the two differ by a single argv token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("func", "expected_flag", "opposite_flag"),
    [
        (add_label, "--add-label", "--remove-label"),
        (remove_label, "--remove-label", "--add-label"),
    ],
    ids=["add_label", "remove_label"],
)
def test_label_write_sends_its_own_flag_and_not_the_opposite(func: Any, expected_flag: str, opposite_flag: str) -> None:
    """Each function must send its own flag and never the other one.

    add_label and remove_label are otherwise identical, so a copy-paste slip
    would invert a driver-loop state transition silently — the call still
    succeeds and still returns True.
    """
    with patch(_RUN, side_effect=lambda cmd, **kw: _ok(cmd)) as mock_run:
        result = func("owner/repo", 42, "agent-ready")

    assert result is True
    argv = mock_run.call_args.args[0]
    assert expected_flag in argv
    assert opposite_flag not in argv
    assert argv[argv.index(expected_flag) + 1] == "agent-ready"
    assert "42" in argv
    assert "owner/repo" in argv


@pytest.mark.parametrize("func", [add_label, remove_label], ids=["add_label", "remove_label"])
def test_label_write_passes_the_shared_timeout(func: Any) -> None:
    """A label write must not be able to hang the driver tick indefinitely."""
    with patch(_RUN, side_effect=lambda cmd, **kw: _ok(cmd)) as mock_run:
        func("owner/repo", 42, "agent-ready")

    assert mock_run.call_args.kwargs["timeout"] == _GH_TIMEOUT
    assert mock_run.call_args.kwargs["check"] is True


@pytest.mark.parametrize("func", [add_label, remove_label], ids=["add_label", "remove_label"])
@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "gh", stderr="label not found"),
        subprocess.TimeoutExpired("gh", 30),
    ],
    ids=["called-process-error", "timeout"],
)
def test_label_write_reports_failure_without_raising(func: Any, exc: Exception) -> None:
    """A failed label write returns False so the caller can decide, and never propagates.

    The driver treats a label write as best-effort; an exception escaping here
    would abort the whole tick rather than the single issue.
    """
    with patch(_RUN, side_effect=exc):
        result = func("owner/repo", 42, "agent-ready")

    assert result is False


# ---------------------------------------------------------------------------
# comment_once — idempotency is the entire point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stdout", ["0", "", "  \n"], ids=["zero", "empty", "whitespace"])
def test_comment_once_posts_when_no_marker_comment_exists(stdout: str) -> None:
    """No existing marker comment means this is the first occurrence, so post."""
    with (
        patch(_RUN, side_effect=lambda cmd, **kw: _ok(cmd, stdout)),
        patch(_COMMENT, return_value=True) as mock_comment,
    ):
        result = comment_once("owner/repo", 42, "<!-- marker -->", "body text")

    assert result is True
    mock_comment.assert_called_once_with("owner/repo", 42, "body text")


def test_comment_once_stays_quiet_when_a_marker_comment_already_exists() -> None:
    """A prior marker comment means the condition was already reported — do not repeat it.

    Without this the driver would append an identical comment on every tick.
    """
    with (
        patch(_RUN, side_effect=lambda cmd, **kw: _ok(cmd, "1")),
        patch(_COMMENT) as mock_comment,
    ):
        result = comment_once("owner/repo", 42, "<!-- marker -->", "body text")

    assert result is False
    mock_comment.assert_not_called()


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "gh", stderr="rate limited"),
        subprocess.TimeoutExpired("gh", 30),
        FileNotFoundError("gh not installed"),
    ],
    ids=["called-process-error", "timeout", "gh-missing"],
)
def test_comment_once_does_not_post_when_the_lookup_fails(exc: Exception) -> None:
    """A failed lookup must fail closed: not posting is the safe direction.

    Treating a transient `gh` error as "no existing comment" would repost on
    every tick for as long as the error persisted — exactly the comment spam
    this function exists to prevent.
    """
    with patch(_RUN, side_effect=exc), patch(_COMMENT) as mock_comment:
        result = comment_once("owner/repo", 42, "<!-- marker -->", "body text")

    assert result is False
    mock_comment.assert_not_called()


def test_comment_once_reports_failure_to_post() -> None:
    """True means a comment was actually posted, so a failed post must return False."""
    with (
        patch(_RUN, side_effect=lambda cmd, **kw: _ok(cmd, "0")),
        patch(_COMMENT, return_value=False),
    ):
        result = comment_once("owner/repo", 42, "<!-- marker -->", "body text")

    assert result is False


def test_comment_once_json_encodes_the_marker_in_the_gh_query() -> None:
    """The marker is JSON-encoded before interpolation, so quotes cannot break the query.

    The marker reaches a jq expression built by string interpolation. A raw
    marker containing a double quote would terminate the jq string early and
    corrupt the filter; json.dumps is what prevents that.
    """
    marker = 'sentinel "quoted"'

    with (
        patch(_RUN, side_effect=lambda cmd, **kw: _ok(cmd, "0")) as mock_run,
        patch(_COMMENT, return_value=True),
    ):
        comment_once("owner/repo", 42, marker, "body text")

    argv = mock_run.call_args.args[0]
    jq_filter = argv[argv.index("--jq") + 1]
    assert json.dumps(marker) in jq_filter
    assert 'startswith("sentinel \\"quoted\\"")' in jq_filter
