"""Test module for fieldkit.watch.repair (domain module).

Covers:
- ``_repair_one(path, today, *, dry_run)`` -- returns a repair report dict or None

Tasks 2.2-2.4: _repair_one mtime/git/error paths
Tasks 2.5: _repair_one no-frontmatter

Patches target ``fieldkit.watch.repair.*`` — the domain module binding site.

CLI-facing tests for ``fieldkit pursuit repair-dates`` live in
``tests/test_pursuit_repair_dates.py``.
"""

import datetime
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fieldkit.watch.repair import _repair_one

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

TODAY = datetime.date(2026, 6, 14)
YESTERDAY = TODAY - datetime.timedelta(days=1)

# Minimal valid pursuit frontmatter whose last-transition equals TODAY.
_FRONTMATTER_TODAY = textwrap.dedent(f"""\
    ---
    title: Test Pursuit
    stage: qualify
    last-transition: {TODAY.isoformat()}
    ---
    Body text.
""")

# Minimal valid pursuit frontmatter whose last-transition is YESTERDAY (already correct).
_FRONTMATTER_YESTERDAY = textwrap.dedent(f"""\
    ---
    title: Test Pursuit
    stage: qualify
    last-transition: {YESTERDAY.isoformat()}
    ---
    Body text.
""")


# ---------------------------------------------------------------------------
# Task 2.2 — stale frontmatter: git date is in the past → repair dict returned
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_repair_one_updates_stale_frontmatter(tmp_path: Path) -> None:
    """_repair_one returns a report dict and calls write_frontmatter when git date < today.

    Scenario:
    - last-transition == TODAY (stale, set by spurious normalisation)
    - _git_last_commit_date returns YESTERDAY (a real past commit)
    - dry_run=False → write_frontmatter must be called exactly once
    - The returned dict carries proposed_date == YESTERDAY and written == True
    """
    # Build a realistic directory structure so account/pursuit extraction works.
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "cloud-migration.md"
    pursuit_file.write_text(_FRONTMATTER_TODAY, encoding="utf-8")

    with (
        patch(
            "fieldkit.watch.repair._git_last_commit_date",
            return_value=YESTERDAY,
        ),
        patch(
            "fieldkit.watch.repair.write_frontmatter",
        ) as mock_write,
    ):
        report = _repair_one(pursuit_file, TODAY, dry_run=False)

    assert report is not None, "_repair_one should return a report dict for a stale file"
    assert report["proposed_date"] == YESTERDAY
    assert report["written"] is True
    assert report["account"] == "acme-corp"
    assert report["pursuit"] == "cloud-migration"
    mock_write.assert_called_once()


@pytest.mark.parametrize("qualification_line", ["meddpicc: null", "legacy_meddpicc: null"])
@pytest.mark.unit
def test_repair_one_writes_pursuit_with_null_history(tmp_path: Path, qualification_line: str) -> None:
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "cloud-migration.md"
    pursuit_file.write_text(
        "---\n"
        "title: Test Pursuit\n"
        "stage: qualify\n"
        f"last-transition: {TODAY.isoformat()}\n"
        f"{qualification_line}\n"
        "custom-key: keep me\n"
        "---\n"
        "Body text.\n",
        encoding="utf-8",
    )

    with patch("fieldkit.watch.repair._git_last_commit_date", return_value=YESTERDAY):
        report = _repair_one(pursuit_file, TODAY, dry_run=False)

    assert report is not None
    assert report["written"] is True
    written = pursuit_file.read_text(encoding="utf-8")
    raw = yaml.safe_load(written.split("---", 2)[1])
    assert raw["last-transition"] == YESTERDAY
    assert raw["custom-key"] == "keep me"
    assert "meddpicc" not in raw
    assert "legacy_meddpicc" not in raw
    assert written.endswith("---\nBody text.\n")


# ---------------------------------------------------------------------------
# Task 2.3 — fresh file: last-transition is already in the past → None returned
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_repair_one_skips_fresh_file(tmp_path: Path) -> None:
    """_repair_one returns None when last-transition is already before today.

    Scenario:
    - last-transition == YESTERDAY (already correct — not today)
    - No git or mtime calls should be needed; the function exits early
    - write_frontmatter must NOT be called
    """
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "cloud-migration.md"
    pursuit_file.write_text(_FRONTMATTER_YESTERDAY, encoding="utf-8")

    with (
        patch("fieldkit.watch.repair._git_last_commit_date") as mock_git,
        patch("fieldkit.watch.repair.write_frontmatter") as mock_write,
    ):
        report = _repair_one(pursuit_file, TODAY, dry_run=False)

    assert report is None, "_repair_one should return None for a file that is already correct"
    mock_git.assert_not_called()
    mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# Task 2.4 — load_pursuit raises → None returned gracefully
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_repair_one_returns_false_on_git_error(tmp_path: Path) -> None:
    """_repair_one returns None (not an exception) when load_pursuit raises.

    Scenario:
    - load_pursuit raises an unexpected exception (e.g. malformed YAML)
    - _repair_one must catch it and return None without propagating
    - write_frontmatter must NOT be called
    """
    pursuit_dir = tmp_path / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "cloud-migration.md"
    # Write a file that exists on disk so the path is valid, but we'll mock
    # load_pursuit to raise regardless of content.
    pursuit_file.write_text(_FRONTMATTER_TODAY, encoding="utf-8")

    with (
        patch(
            "fieldkit.watch.repair.load_pursuit",
            side_effect=RuntimeError("simulated git/parse failure"),
        ),
        patch("fieldkit.watch.repair.write_frontmatter") as mock_write,
    ):
        report = _repair_one(pursuit_file, TODAY, dry_run=False)

    assert report is None, "_repair_one should return None when load_pursuit raises"
    mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# Task 2.5 — _repair_one returns None when file has no frontmatter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_repair_one_returns_false_no_frontmatter(tmp_path: Path) -> None:
    """A file with no '---' delimiters causes load_pursuit to raise ValueError.

    _repair_one catches all exceptions from load_pursuit and returns None,
    so a file without YAML frontmatter must yield None (i.e. no repair needed /
    not processable).

    Design note: _repair_one uses a broad ``except Exception`` guard around
    load_pursuit precisely to handle malformed files gracefully.  We verify
    that contract here without coupling to the internal exception type.
    """
    # Arrange: a plain markdown file with no YAML frontmatter delimiters
    pursuit_file = tmp_path / "no-frontmatter.md"
    pursuit_file.write_text("# Just a heading\n\nSome body text.\n", encoding="utf-8")

    # Mock the git/mtime helpers so the test never touches the real filesystem
    # or git process.  Their return values don't matter here because load_pursuit
    # will raise before they are called, but patching them prevents accidental
    # side-effects in CI.
    with (
        patch("fieldkit.watch.repair._git_last_commit_date", return_value=None),
        patch(
            "fieldkit.watch.repair._file_mtime_date",
            return_value=datetime.date(2026, 6, 10),
        ),
    ):
        result = _repair_one(pursuit_file, TODAY, dry_run=True)

    assert result is None, "_repair_one must return None for a file with no YAML frontmatter"
