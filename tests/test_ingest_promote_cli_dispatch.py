"""Coverage for the `promote` CLI adapter's file-resolution dispatch.

Targets `cli` in `commands/ingest/promote.py` (CRAP 48.61, complexity 11).
The setup (identity load, TASKS.md existence guard) is already covered by
test_ingest_promote_eof.py / test_ingest_promote_extended.py. This file
covers the three-way file-resolution branch that follows:

  - a positional MEETING_FILE bypasses --recent/--account entirely
  - --recent with/without --account selects the right glob pattern
  - .gitkeep and dotfiles are excluded from candidates
  - candidates are sorted by mtime descending and sliced to N
  - an empty glob result prints a message and exits 0 without processing
  - neither MEETING_FILE nor --recent prints a usage error and exits 1
  - the per-file loop stops on the first `_promote_file` quit-early signal
  - the per-file loop otherwise processes every file and prints completion
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_home(tmp_path: Path) -> Path:
    """Create a fieldkit home with TASKS.md so the CLI clears its setup guard."""
    (tmp_path / "TASKS.md").write_text("# Tasks\n\n## Active\n\n## Waiting On\n", encoding="utf-8")
    return tmp_path


def _make_meeting(data_root: Path, account: str, filename: str, *, mtime: float | None = None) -> Path:
    """Create a meeting note under accounts/<account>/meetings/<filename>."""
    meetings_dir = data_root / "accounts" / account / "meetings"
    meetings_dir.mkdir(parents=True, exist_ok=True)
    meeting = meetings_dir / filename
    meeting.write_text("---\naction_items: []\n---\n\nNotes.\n", encoding="utf-8")
    if mtime is not None:
        os.utime(meeting, (mtime, mtime))
    return meeting


def _invoke(runner: CliRunner, args: list[str]) -> Result:
    from fieldkit.commands.ingest.promote import cli

    return runner.invoke(cli, args)


# ---------------------------------------------------------------------------
# 1. positional MEETING_FILE bypasses the --recent/--account branch
# ---------------------------------------------------------------------------


def test_meeting_file_positional_skips_recent_dispatch(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)
    meeting = data_root / "standalone.md"
    meeting.write_text("---\naction_items: []\n---\n", encoding="utf-8")

    runner = CliRunner()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", return_value=False) as mock_promote,
    ):
        result = _invoke(runner, [str(meeting)])

    assert result.exit_code == 0
    assert "Promoting from" not in result.output
    mock_promote.assert_called_once()
    assert mock_promote.call_args[0][0] == meeting


# ---------------------------------------------------------------------------
# 2/3. --recent glob pattern selection: all accounts vs. one account
# ---------------------------------------------------------------------------


def test_recent_without_account_globs_all_accounts(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)
    meeting_a = _make_meeting(data_root, "acme-corp", "one.md")
    meeting_b = _make_meeting(data_root, "example-corp", "two.md")

    runner = CliRunner()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", return_value=False) as mock_promote,
    ):
        result = _invoke(runner, ["--recent", "5"])

    assert result.exit_code == 0
    called_files = {c.args[0] for c in mock_promote.call_args_list}
    assert called_files == {meeting_a, meeting_b}


def test_recent_with_account_scopes_glob_to_one_account(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)
    meeting_a = _make_meeting(data_root, "acme-corp", "one.md")
    meeting_b = _make_meeting(data_root, "example-corp", "two.md")

    runner = CliRunner()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", return_value=False) as mock_promote,
    ):
        result = _invoke(runner, ["--recent", "5", "--account", "acme-corp"])

    assert result.exit_code == 0
    called_files = [c.args[0] for c in mock_promote.call_args_list]
    assert called_files == [meeting_a]
    assert meeting_b not in called_files
    assert "in account 'acme-corp'" in result.output


# ---------------------------------------------------------------------------
# 4. .gitkeep and dotfiles are excluded from candidates
# ---------------------------------------------------------------------------


def test_gitkeep_and_dotfiles_excluded_from_candidates(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)
    real_meeting = _make_meeting(data_root, "acme-corp", "real.md")
    meetings_dir = data_root / "accounts" / "acme-corp" / "meetings"
    (meetings_dir / ".gitkeep").write_text("", encoding="utf-8")
    (meetings_dir / ".hidden.md").write_text("---\naction_items: []\n---\n", encoding="utf-8")

    runner = CliRunner()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", return_value=False) as mock_promote,
    ):
        result = _invoke(runner, ["--recent", "5"])

    assert result.exit_code == 0
    called_files = [c.args[0] for c in mock_promote.call_args_list]
    assert called_files == [real_meeting]


# ---------------------------------------------------------------------------
# 5. mtime-descending sort, sliced to N
# ---------------------------------------------------------------------------


def test_recent_sorts_by_mtime_descending_and_slices_to_n(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)
    oldest = _make_meeting(data_root, "acme-corp", "oldest.md", mtime=1_000_000)
    middle = _make_meeting(data_root, "acme-corp", "middle.md", mtime=2_000_000)
    newest = _make_meeting(data_root, "acme-corp", "newest.md", mtime=3_000_000)

    runner = CliRunner()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", return_value=False) as mock_promote,
    ):
        result = _invoke(runner, ["--recent", "2"])

    assert result.exit_code == 0
    called_files = [c.args[0] for c in mock_promote.call_args_list]
    assert called_files == [newest, middle]
    assert oldest not in called_files


# ---------------------------------------------------------------------------
# 6. empty glob result: message + exit 0, no processing
# ---------------------------------------------------------------------------


def test_recent_with_no_matches_prints_message_and_exits_zero(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)

    runner = CliRunner()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", return_value=False) as mock_promote,
    ):
        result = _invoke(runner, ["--recent", "5"])

    assert result.exit_code == 0
    assert "No meeting files found." in result.output
    assert "Promoting from" not in result.output
    mock_promote.assert_not_called()


# ---------------------------------------------------------------------------
# 7. neither MEETING_FILE nor --recent: usage error, exit 1
# ---------------------------------------------------------------------------


def test_neither_meeting_file_nor_recent_errors_and_exits_one(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)

    runner = CliRunner()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", return_value=False) as mock_promote,
    ):
        result = _invoke(runner, [])

    assert result.exit_code == 1
    assert "Provide a MEETING_FILE or use --recent N." in result.output
    mock_promote.assert_not_called()


# ---------------------------------------------------------------------------
# 8. per-file loop stops on the first quit-early signal
# ---------------------------------------------------------------------------


def test_per_file_loop_breaks_on_first_quit_early(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)
    _make_meeting(data_root, "acme-corp", "a.md", mtime=3_000_000)
    _make_meeting(data_root, "acme-corp", "b.md", mtime=2_000_000)
    _make_meeting(data_root, "acme-corp", "c.md", mtime=1_000_000)

    runner = CliRunner()
    mock_promote = MagicMock(side_effect=[True, False, False])
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", mock_promote),
    ):
        result = _invoke(runner, ["--recent", "3"])

    assert result.exit_code == 0
    assert mock_promote.call_count == 1


# ---------------------------------------------------------------------------
# 9. per-file loop processes every file on normal completion
# ---------------------------------------------------------------------------


def test_per_file_loop_processes_all_files_on_normal_completion(tmp_path: Path) -> None:
    data_root = _make_home(tmp_path)
    _make_meeting(data_root, "acme-corp", "a.md", mtime=3_000_000)
    _make_meeting(data_root, "acme-corp", "b.md", mtime=2_000_000)
    _make_meeting(data_root, "acme-corp", "c.md", mtime=1_000_000)

    runner = CliRunner()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("fieldkit.commands.ingest.promote._promote_file", return_value=False) as mock_promote,
    ):
        result = _invoke(runner, ["--recent", "3"])

    assert result.exit_code == 0
    assert mock_promote.call_count == 3
    assert "✓ Promotion complete. Review TASKS.md to verify." in result.output
