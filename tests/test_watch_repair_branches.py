"""Tests for _repair_one() — covering uncovered branches.

cc=13, cov=58%, target: no last_transition, git+mtime=today skip,
dry_run=True (written=False), write_frontmatter error, string date.

Patches target ``fieldkit.watch.repair.*`` — the domain module binding site.
"""

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.watch.repair import _git_last_commit_date, _repair_one

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TODAY = datetime.date(2026, 6, 19)
YESTERDAY = TODAY - datetime.timedelta(days=1)


def _write_pursuit(path: Path, last_transition: str | None = None) -> None:
    """Write a minimal valid pursuit frontmatter file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", "title: Test Pursuit", "stage: propose"]
    if last_transition:
        lines.append(f"last_transition: {last_transition}")
    lines += ["---", "Body text.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# no last_transition → None returned (not affected)
# ---------------------------------------------------------------------------


# ── TestRepairOneNoLastTransition (flattened) ───────────────────────────────


def test_repair_one_no_last_transition_no_last_transition_returns_none(tmp_path: Path) -> None:
    """_repair_one returns None when last_transition is absent from frontmatter."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    _write_pursuit(pursuit_file, last_transition=None)

    result = _repair_one(pursuit_file, TODAY, dry_run=True)

    assert result is None


# ---------------------------------------------------------------------------
# last_transition != today → None returned (not affected)
# ---------------------------------------------------------------------------


# ── TestRepairOneNotToday (flattened) ───────────────────────────────────────


def test_repair_one_not_today_past_date_returns_none(tmp_path: Path) -> None:
    """_repair_one returns None when last-transition is already in the past."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    _write_pursuit(pursuit_file, last_transition=YESTERDAY.isoformat())

    result = _repair_one(pursuit_file, TODAY, dry_run=True)

    assert result is None


# ---------------------------------------------------------------------------
# git date = today, mtime = today → None (cannot improve)
# ---------------------------------------------------------------------------


# ── TestRepairOneGitAndMtimeToday (flattened) ───────────────────────────────


def test_repair_one_git_and_mtime_today_git_today_mtime_today_returns_none(tmp_path: Path) -> None:
    """Returns None when both git and mtime show today — no improvement possible."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    _write_pursuit(pursuit_file, last_transition=TODAY.isoformat())

    with (
        patch("fieldkit.watch.repair._git_last_commit_date", return_value=TODAY),
        patch("fieldkit.watch.repair._file_mtime_date", return_value=TODAY),
    ):
        result = _repair_one(pursuit_file, TODAY, dry_run=True)

    assert result is None


def test_repair_one_git_and_mtime_today_git_none_mtime_today_returns_none(tmp_path: Path) -> None:
    """Returns None when git is unavailable (None) and mtime is today."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    _write_pursuit(pursuit_file, last_transition=TODAY.isoformat())

    with (
        patch("fieldkit.watch.repair._git_last_commit_date", return_value=None),
        patch("fieldkit.watch.repair._file_mtime_date", return_value=TODAY),
    ):
        result = _repair_one(pursuit_file, TODAY, dry_run=True)

    assert result is None


# ---------------------------------------------------------------------------
# git date is past → repair using git date
# ---------------------------------------------------------------------------


# ── TestRepairOneGitPastDate (flattened) ────────────────────────────────────


def test_repair_one_git_past_date_git_yesterday_proposes_yesterday(tmp_path: Path) -> None:
    """When git date is in the past, proposed_date is the git date."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    _write_pursuit(pursuit_file, last_transition=TODAY.isoformat())

    with (
        patch("fieldkit.watch.repair._git_last_commit_date", return_value=YESTERDAY),
        patch("fieldkit.watch.repair.write_frontmatter"),
    ):
        result = _repair_one(pursuit_file, TODAY, dry_run=False)

    assert result is not None
    assert result["proposed_date"] == YESTERDAY
    assert result["written"] is True


# ---------------------------------------------------------------------------
# git date = today but mtime is past → repair using mtime
# ---------------------------------------------------------------------------


# ── TestRepairOneMtimeFallback (flattened) ──────────────────────────────────


def test_repair_one_mtime_fallback_git_today_mtime_past_uses_mtime(tmp_path: Path) -> None:
    """When git date is today but mtime is past, proposed_date = mtime."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    _write_pursuit(pursuit_file, last_transition=TODAY.isoformat())

    past_date = TODAY - datetime.timedelta(days=5)

    with (
        patch("fieldkit.watch.repair._git_last_commit_date", return_value=TODAY),
        patch("fieldkit.watch.repair._file_mtime_date", return_value=past_date),
        patch("fieldkit.watch.repair.write_frontmatter"),
    ):
        result = _repair_one(pursuit_file, TODAY, dry_run=False)

    assert result is not None
    assert result["proposed_date"] == past_date


# ---------------------------------------------------------------------------
# dry_run=True → written=False, write_frontmatter not called
# ---------------------------------------------------------------------------


# ── TestRepairOneDryRun (flattened) ─────────────────────────────────────────


def test_repair_one_dry_run_dry_run_does_not_write(tmp_path: Path) -> None:
    """dry_run=True: written=False and write_frontmatter is not called."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    _write_pursuit(pursuit_file, last_transition=TODAY.isoformat())

    with (
        patch("fieldkit.watch.repair._git_last_commit_date", return_value=YESTERDAY),
        patch("fieldkit.watch.repair.write_frontmatter") as mock_write,
    ):
        result = _repair_one(pursuit_file, TODAY, dry_run=True)

    assert result is not None
    assert result["written"] is False
    mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# write_frontmatter raises → written=False but report returned
# ---------------------------------------------------------------------------


# ── TestRepairOneWriteError (flattened) ─────────────────────────────────────


def test_repair_one_write_error_write_error_sets_written_false(tmp_path: Path) -> None:
    """When write_frontmatter raises, written=False but report is still returned."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    _write_pursuit(pursuit_file, last_transition=TODAY.isoformat())

    with (
        patch("fieldkit.watch.repair._git_last_commit_date", return_value=YESTERDAY),
        patch(
            "fieldkit.watch.repair.write_frontmatter",
            side_effect=OSError("disk full"),
        ),
    ):
        result = _repair_one(pursuit_file, TODAY, dry_run=False)

    assert result is not None
    assert result["written"] is False
    assert result["proposed_date"] == YESTERDAY


# ---------------------------------------------------------------------------
# account/pursuit extraction from path
# ---------------------------------------------------------------------------


# ── TestRepairOnePathExtraction (flattened) ─────────────────────────────────


def test_repair_one_path_extraction_account_and_pursuit_names_extracted(tmp_path: Path) -> None:
    """account and pursuit fields are correctly extracted from the file path."""
    pursuit_dir = tmp_path / "globalpay" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "big-contract.md"
    _write_pursuit(pursuit_file, last_transition=TODAY.isoformat())

    with (
        patch("fieldkit.watch.repair._git_last_commit_date", return_value=YESTERDAY),
        patch("fieldkit.watch.repair.write_frontmatter"),
    ):
        result = _repair_one(pursuit_file, TODAY, dry_run=False)

    assert result is not None
    assert result["account"] == "globalpay"
    assert result["pursuit"] == "big-contract"


# ---------------------------------------------------------------------------
# _git_last_commit_date — git not found, empty output, invalid date
# ---------------------------------------------------------------------------


# ── TestGitLastCommitDate (flattened) ───────────────────────────────────────


def test_git_last_commit_date_git_not_found_returns_none(tmp_path: Path) -> None:
    """When git binary is not found (FileNotFoundError), returns None."""
    path = tmp_path / "file.md"
    path.write_text("content", encoding="utf-8")

    with patch("fieldkit.watch.repair.subprocess.run", side_effect=FileNotFoundError):
        result = _git_last_commit_date(path)

    assert result is None


def test_git_last_commit_date_empty_git_output_returns_none(tmp_path: Path) -> None:
    """When git returns empty stdout (untracked file), returns None."""
    path = tmp_path / "file.md"
    path.write_text("content", encoding="utf-8")

    mock_result = MagicMock()
    mock_result.stdout = ""
    with patch("fieldkit.watch.repair.subprocess.run", return_value=mock_result):
        result = _git_last_commit_date(path)

    assert result is None


def test_git_last_commit_date_invalid_date_output_returns_none(tmp_path: Path) -> None:
    """When git returns non-ISO date string, returns None."""
    path = tmp_path / "file.md"
    path.write_text("content", encoding="utf-8")

    mock_result = MagicMock()
    mock_result.stdout = "not-a-date\n"
    with patch("fieldkit.watch.repair.subprocess.run", return_value=mock_result):
        result = _git_last_commit_date(path)

    assert result is None


def test_git_last_commit_date_valid_date_output_returns_date(tmp_path: Path) -> None:
    """When git returns a valid ISO date, returns that date."""
    path = tmp_path / "file.md"
    path.write_text("content", encoding="utf-8")

    mock_result = MagicMock()
    mock_result.stdout = "2026-06-01\n"
    with patch("fieldkit.watch.repair.subprocess.run", return_value=mock_result):
        result = _git_last_commit_date(path)

    assert result == datetime.date(2026, 6, 1)


# ---------------------------------------------------------------------------
# _repair_one — non-ISO last_transition (string date that's not ISO)
# ---------------------------------------------------------------------------


# ── TestRepairOneNonIsoDate (flattened) ─────────────────────────────────────


def test_repair_one_non_iso_date_non_iso_last_transition_returns_none(tmp_path: Path) -> None:
    """_repair_one returns None when last_transition is not a valid ISO date string."""
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    # Write a file with a non-ISO date that load_pursuit will return as a string
    pursuit_file.write_text(
        "---\ntitle: Test\nstage: propose\nlast_transition: not-a-date\n---\nBody.\n",
        encoding="utf-8",
    )

    mock_model = MagicMock()
    mock_model.last_transition = "not-a-date"

    with patch("fieldkit.watch.repair.load_pursuit", return_value=(mock_model, "", None)):
        result = _repair_one(pursuit_file, TODAY, dry_run=True)

    assert result is None
