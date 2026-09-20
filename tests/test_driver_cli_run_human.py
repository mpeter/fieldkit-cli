"""Coverage for the human-readable rendering branch of ``fieldkit driver run``.

``tests/test_json_flag_rollout.py`` already covers the ``--json`` branch of
this function. This file fills the remaining gap: the default
(``as_json=False``) status-line rendering path, including the ``skipped``
early return, the dry-run prefix, the status-symbol lookup, the four detail
lines, the two optional lines (``spend_note``/``error``), and the
human-readable exit-1-on-failure branch.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from fieldkit.commands.driver.cli import cli as driver_cli

pytestmark = pytest.mark.unit


def _make_result(
    outcome: str = "ok",
    issue_number: int = 1234,
    issue_title: str = "historic regression example",
    branch: str = "fix/historic regression",
    elapsed_seconds: float = 12.5,
    spend_note: str = "",
    error: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        issue_number=issue_number,
        issue_title=issue_title,
        branch=branch,
        outcome=outcome,
        elapsed_seconds=elapsed_seconds,
        spend_note=spend_note,
        error=error,
    )


def _invoke(tmp_path: Path, result: SimpleNamespace, args: list[str]) -> Result:
    with (
        patch("fieldkit.commands.driver.cli._repo_root", return_value=tmp_path),
        patch("fieldkit.driver.runner.run_driver", return_value=result),
    ):
        return CliRunner().invoke(driver_cli, ["run", *args])


def test_skipped_outcome_prints_only_the_skip_message(tmp_path: Path) -> None:
    """A skipped run prints the skip message and returns before any detail line."""
    result = _invoke(tmp_path, _make_result(outcome="skipped"), [])

    assert result.exit_code == 0, result.output
    assert "No agent-ready issues — nothing to do." in result.output
    assert "Issue #" not in result.output
    assert "Branch:" not in result.output
    assert "Outcome:" not in result.output
    assert "Elapsed:" not in result.output


def test_skipped_outcome_with_dry_run_gets_the_prefix(tmp_path: Path) -> None:
    """The dry-run prefix is prepended to the skip message too."""
    result = _invoke(tmp_path, _make_result(outcome="skipped"), ["--dry-run"])

    assert result.exit_code == 0, result.output
    assert "[dry-run] No agent-ready issues — nothing to do." in result.output


def test_dry_run_prefixes_the_status_line(tmp_path: Path) -> None:
    """--dry-run prepends '[dry-run] ' to the main status line for a non-skipped outcome."""
    result = _invoke(tmp_path, _make_result(outcome="dry-run"), ["--dry-run"])

    assert result.exit_code == 0, result.output
    assert "[dry-run] " in result.output
    status_line = next(line for line in result.output.splitlines() if "Issue #" in line)
    assert status_line.startswith("[dry-run] ")


def test_without_dry_run_the_prefix_is_absent(tmp_path: Path) -> None:
    """Without --dry-run, the status line has no '[dry-run] ' prefix."""
    result = _invoke(tmp_path, _make_result(outcome="ok"), [])

    assert result.exit_code == 0, result.output
    status_line = next(line for line in result.output.splitlines() if "Issue #" in line)
    assert not status_line.startswith("[dry-run] ")
    assert "[dry-run]" not in result.output


@pytest.mark.parametrize("outcome", ["ok", "dry-run", "failed"])
def test_known_outcomes_render_their_status_symbol(tmp_path: Path, outcome: str) -> None:
    """Each outcome present in the symbol lookup renders without falling back to '?'."""
    result = _invoke(tmp_path, _make_result(outcome=outcome), [])

    status_line = next(line for line in result.output.splitlines() if "Issue #" in line)
    assert "?" not in status_line


def test_unknown_outcome_falls_back_to_question_mark(tmp_path: Path) -> None:
    """An outcome absent from the symbol dict falls back to '?'."""
    result = _invoke(tmp_path, _make_result(outcome="weird-outcome"), [])

    assert result.exit_code == 0, result.output
    status_line = next(line for line in result.output.splitlines() if "Issue #" in line)
    assert "?" in status_line


def test_skipped_symbol_in_the_lookup_dict_is_unreachable(tmp_path: Path) -> None:
    """The 'skipped' entry in status_sym is dead code: the function returns
    early on outcome == 'skipped' before the dict lookup is ever reached."""
    result = _invoke(tmp_path, _make_result(outcome="skipped"), [])

    assert result.exit_code == 0, result.output
    # Only the skip message is printed — the status-symbol line (which would
    # have used the "skipped" -> "-" entry) never executes.
    assert result.output.strip() == "No agent-ready issues — nothing to do."


def test_detail_lines_render_the_result_fields(tmp_path: Path) -> None:
    """The four detail lines carry the exact values from the result object."""
    result = _invoke(
        tmp_path,
        _make_result(
            outcome="ok",
            issue_number=42,
            issue_title="implementation change do the thing",
            branch="feature/implementation change",
            elapsed_seconds=12.5,
        ),
        [],
    )

    assert result.exit_code == 0, result.output
    assert "Issue #42: implementation change do the thing" in result.output
    assert "Branch:  feature/implementation change" in result.output
    assert "Outcome: ok" in result.output
    assert "Elapsed: 12.5s" in result.output


def test_elapsed_seconds_is_formatted_to_one_decimal_place(tmp_path: Path) -> None:
    """elapsed_seconds is rounded to exactly one decimal place."""
    result = _invoke(tmp_path, _make_result(outcome="ok", elapsed_seconds=12.567), [])

    assert "Elapsed: 12.6s" in result.output
    assert "12.567" not in result.output
    assert "12.57" not in result.output


def test_spend_note_present_is_rendered(tmp_path: Path) -> None:
    """A non-empty spend_note is printed as its own line."""
    result = _invoke(tmp_path, _make_result(outcome="ok", spend_note="spend: $0.10"), [])

    assert "spend: $0.10" in result.output


def test_spend_note_absent_is_not_rendered(tmp_path: Path) -> None:
    """An empty spend_note prints nothing extra."""
    result = _invoke(tmp_path, _make_result(outcome="ok", spend_note=""), [])

    assert "spend" not in result.output.lower()
    assert result.output.count("\n") == 4


def test_error_present_is_rendered_to_stderr(tmp_path: Path) -> None:
    """A non-empty error is printed (in red, to stderr) as its own line."""
    result = _invoke(tmp_path, _make_result(outcome="failed", error="opencode exited 1"), [])

    assert "Error: opencode exited 1" in result.stderr


def test_error_absent_is_not_rendered(tmp_path: Path) -> None:
    """An empty error prints no Error line at all."""
    result = _invoke(tmp_path, _make_result(outcome="ok", error=""), [])

    assert "Error:" not in result.output
    assert "Error:" not in result.stderr


def test_failed_outcome_exits_1_in_human_path(tmp_path: Path) -> None:
    """A failed outcome exits 1 in the human-readable path, after printing detail lines."""
    result = _invoke(tmp_path, _make_result(outcome="failed", error="boom"), [])

    assert result.exit_code == 1
    assert "Outcome: failed" in result.output
    assert "Error: boom" in result.stderr


def test_non_failed_outcome_exits_0_in_human_path(tmp_path: Path) -> None:
    """A non-failed outcome exits 0 in the human-readable path."""
    result = _invoke(tmp_path, _make_result(outcome="ok"), [])

    assert result.exit_code == 0
