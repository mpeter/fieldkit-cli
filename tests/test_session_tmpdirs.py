"""Regression tests for sweeping session temp directories left by killed runs."""

import os
from pathlib import Path

import pytest
from _session_tmpdirs import STALE_SESSION_TMPDIR_SECONDS, remove_stale_session_tmpdirs

pytestmark = pytest.mark.unit

NOW = 2_000_000_000.0
STALE = NOW - STALE_SESSION_TMPDIR_SECONDS - 60
FRESH = NOW - 60


def _session_dir(parent: Path, name: str, mtime: float) -> Path:
    """Create a session-shaped directory with a config file and the given mtime."""
    path = parent / name
    (path / "home").mkdir(parents=True)
    (path / "home" / "config.yaml").write_text("fieldkit_home: x\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_stale_session_directory_is_removed(tmp_path: Path) -> None:
    """A killed run's directory past the staleness window is swept."""
    stale = _session_dir(tmp_path, "fieldkit-ci-abc123", STALE)

    removed = remove_stale_session_tmpdirs(tmp_path, now=NOW)

    assert removed == [stale]
    assert not stale.exists()


@pytest.mark.parametrize(
    ("name", "mtime"),
    [
        ("fieldkit-ci-running", FRESH),
        ("fieldkit-quality-abc123", STALE),
        ("pytest-of-someone", STALE),
    ],
)
def test_fresh_or_unrelated_directory_is_kept(tmp_path: Path, name: str, mtime: float) -> None:
    """A concurrent run's directory and other tools' directories survive."""
    kept = _session_dir(tmp_path, name, mtime)

    removed = remove_stale_session_tmpdirs(tmp_path, now=NOW)

    assert removed == []
    assert (kept / "home" / "config.yaml").exists()


def test_symlink_with_session_prefix_is_kept_with_its_target(tmp_path: Path) -> None:
    """The sweep never follows a symlink into a directory it does not own."""
    target = _session_dir(tmp_path, "elsewhere", STALE)
    link = tmp_path / "fieldkit-ci-link"
    link.symlink_to(target, target_is_directory=True)

    removed = remove_stale_session_tmpdirs(tmp_path, now=NOW)

    assert removed == []
    assert link.is_symlink()
    assert (target / "home" / "config.yaml").exists()


def test_plain_file_with_session_prefix_is_kept(tmp_path: Path) -> None:
    """Only directories are swept; a file that shares the prefix is left alone."""
    stray = tmp_path / "fieldkit-ci-notes.log"
    stray.write_text("log\n", encoding="utf-8")
    os.utime(stray, (STALE, STALE))

    removed = remove_stale_session_tmpdirs(tmp_path, now=NOW)

    assert removed == []
    assert stray.exists()


def test_directory_that_deletion_cannot_remove_is_not_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deletion failure swallowed by rmtree is not reported as a removal."""
    stale = _session_dir(tmp_path, "fieldkit-ci-stuck", STALE)
    attempted: list[Path] = []
    monkeypatch.setattr("_session_tmpdirs.shutil.rmtree", lambda path, ignore_errors: attempted.append(path))

    removed = remove_stale_session_tmpdirs(tmp_path, now=NOW)

    assert removed == []
    assert attempted == [stale]
    assert (stale / "home" / "config.yaml").exists()


def test_other_users_stale_directory_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale directory owned by a different UID is never swept."""
    stale = _session_dir(tmp_path, "fieldkit-ci-other-user", STALE)
    owner = stale.stat().st_uid
    monkeypatch.setattr("_session_tmpdirs.os.getuid", lambda: owner + 1)

    removed = remove_stale_session_tmpdirs(tmp_path, now=NOW)

    assert removed == []
    assert (stale / "home" / "config.yaml").exists()
