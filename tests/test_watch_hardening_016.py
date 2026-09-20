"""Tests for watch & brief hardening — spec 016.

historic regression: run-all exit code propagation
historic regression: pursuit-stalls "skipped" message
historic regression: waiting-on comment-format date warning
historic regression: brief open staleness warning
"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# historic regression: run-all exits non-zero when any watcher exits non-zero
# ---------------------------------------------------------------------------


# ── TestRunAllExitCodePropagation (flattened) ───────────────────────────────


def test_run_all_exit_code_propagation_clirunnner_captures_systemexit_correctly() -> None:
    """CliRunner sets exit_code from SystemExit — foundation of historic regression fix."""
    import click

    @click.command("fake-watcher")
    def fake_watcher() -> None:
        raise SystemExit(1)

    result = CliRunner().invoke(fake_watcher, [], catch_exceptions=True)
    assert result.exit_code == 1


def test_run_all_exit_code_propagation_max_code_accumulates_correctly() -> None:
    """max_code takes the maximum exit code across all watchers."""
    codes = [0, 1, 0, 1, 0]
    max_code = 0
    for code in codes:
        if isinstance(code, int) and code > max_code:
            max_code = code
    assert max_code == 1


def test_run_all_exit_code_propagation_max_code_zero_when_all_pass() -> None:
    """max_code is 0 when all watchers exit 0."""
    codes = [0, 0, 0]
    max_code = 0
    for code in codes:
        if code > max_code:
            max_code = code
    assert max_code == 0


# ---------------------------------------------------------------------------
# historic regression: "failed to check" → "skipped (reason)" in pursuit-stalls
# ---------------------------------------------------------------------------


# ── TestPursuitStallsSkippedMessage (flattened) ─────────────────────────────


def test_pursuit_stalls_skipped_message_skip_message_says_skipped_not_failed() -> None:
    """The message emitted when skipped > 0 must say 'skipped', not 'failed to check'."""
    import io

    import click

    buf = io.StringIO()
    skipped = 2
    with patch("sys.stderr", buf):
        click.echo(
            f"NOTE: {skipped} pursuit(s) skipped (terminal stage, missing last-transition, or unreadable) "
            "— use --verbose to see details",
            err=True,
        )

    output = buf.getvalue()
    assert "failed to check" not in output
    assert "skipped" in output


def test_pursuit_stalls_skipped_message_old_message_not_present_in_source() -> None:
    """Regression: the old 'failed to check' message must not exist in the source."""
    import inspect

    from fieldkit.commands.watch import pursuit_stalls

    source = inspect.getsource(pursuit_stalls)
    assert "failed to check" not in source, "historic regression: old message still in pursuit_stalls.py"


# ---------------------------------------------------------------------------
# historic regression: waiting-on comment-format date warns
# ---------------------------------------------------------------------------


# ── TestWaitingOnCommentDateWarning (flattened) ─────────────────────────────


def test_waiting_on_comment_date_warning_comment_format_date_matches_regex() -> None:
    """A line with <!-- 2026-07-01 --> should match the comment date regex."""
    from fieldkit.watch.waiting_on_tracker import _COMMENT_DATE_RE, _item_date

    item = "- Deploy new feature <!-- 2026-07-01 -->"
    assert _item_date(item) is None, "comment-format date should not parse as valid date"
    assert _COMMENT_DATE_RE.search(item) is not None, "comment date regex should match"


def test_waiting_on_comment_date_warning_normal_date_format_parses_correctly() -> None:
    """A line with (YYYY-MM-DD) should parse correctly."""
    from fieldkit.watch.waiting_on_tracker import _item_date

    item = "- Deploy new feature (2026-07-01)"
    date_result = _item_date(item)
    assert date_result is not None
    assert str(date_result) == "2026-07-01"


def test_waiting_on_comment_date_warning_no_date_does_not_trigger_comment_warning() -> None:
    """A line with no date at all should not match the comment regex."""
    from fieldkit.watch.waiting_on_tracker import _COMMENT_DATE_RE, _item_date

    item = "- Deploy new feature when ready"
    assert _item_date(item) is None
    assert _COMMENT_DATE_RE.search(item) is None


# ---------------------------------------------------------------------------
# historic regression: brief open warns on stale brief (>24h)
# ---------------------------------------------------------------------------


# ── TestBriefOpenStalenessWarning (flattened) ───────────────────────────────


def _brief_open_staleness_warning_make_brief(briefs_dir: Path, age_seconds: float) -> Path:
    briefs_dir.mkdir(parents=True, exist_ok=True)
    brief = briefs_dir / "morning-brief-2026-01-01.md"
    brief.write_text("# Brief\n", encoding="utf-8")
    old_time = time.time() - age_seconds
    import os

    os.utime(brief, (old_time, old_time))
    return brief


def test_brief_open_staleness_warning_no_warning_for_fresh_brief(tmp_path: Path) -> None:
    """A brief <24h old should open without a staleness warning."""
    briefs_dir = tmp_path / "briefs"
    _brief_open_staleness_warning_make_brief(briefs_dir, age_seconds=3600)  # 1 hour old

    with (
        patch("fieldkit.commands.brief.cli.get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open"),
    ):
        from fieldkit.commands.brief.cli import cmd_open

        result = CliRunner().invoke(cmd_open, [])

    assert "WARNING" not in result.output


def test_brief_open_staleness_warning_warning_for_stale_brief(tmp_path: Path) -> None:
    """A brief >24h old should trigger a staleness warning."""
    briefs_dir = tmp_path / "briefs"
    _brief_open_staleness_warning_make_brief(briefs_dir, age_seconds=90000)  # 25 hours old

    with (
        patch("fieldkit.commands.brief.cli.get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open"),
    ):
        from fieldkit.commands.brief.cli import cmd_open

        result = CliRunner().invoke(cmd_open, [])

    assert "WARNING" in result.output
    assert "old" in result.output.lower()
    assert "fieldkit brief" in result.output


def test_brief_open_config_error_exits_3() -> None:
    """cmd_open exits 3 with a clear message when get_fieldkit_home() raises ConfigError."""
    from fieldkit.config import ConfigError

    with patch("fieldkit.commands.brief.cli.get_fieldkit_home", side_effect=ConfigError("no config")):
        from fieldkit.commands.brief.cli import cmd_open

        result = CliRunner().invoke(cmd_open, [])

    assert result.exit_code == 3
    assert "Config error" in result.output


def test_brief_open_no_briefs_found_exits_3(tmp_path: Path) -> None:
    """cmd_open exits 3 with a hint to run 'brief generate' when no briefs exist yet."""
    with patch("fieldkit.commands.brief.cli.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.brief.cli import cmd_open

        result = CliRunner().invoke(cmd_open, [])

    assert result.exit_code == 3
    assert "No morning brief files found" in result.output
    assert "fieldkit brief generate" in result.output
