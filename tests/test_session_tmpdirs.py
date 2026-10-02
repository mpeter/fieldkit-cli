"""Synthetic-root regression tests for pytest session temp directory ownership."""

import os
import shutil
from pathlib import Path

import pytest
from _session_tmpdirs import create_session_tmpdir, remove_stale_session_tmpdirs

pytestmark = pytest.mark.unit


def test_dead_session_directory_is_removed_at_next_start(tmp_path: Path) -> None:
    session, lock = create_session_tmpdir(tmp_path)
    (session / "home").mkdir()
    (session / "home" / "config.yaml").write_text("fieldkit_home: x\n", encoding="utf-8")
    lock.close()  # Simulate a killed owner process without touching a real process.

    assert remove_stale_session_tmpdirs(tmp_path) == [session]
    assert not session.exists()


def test_live_session_survives_even_when_directory_is_old(tmp_path: Path) -> None:
    session, lock = create_session_tmpdir(tmp_path)
    try:
        os.utime(session, (1, 1))
        assert remove_stale_session_tmpdirs(tmp_path) == []
        assert session.exists()
    finally:
        lock.close()
        shutil.rmtree(session)


def test_concurrent_live_session_is_kept_while_dead_session_is_removed(tmp_path: Path) -> None:
    live, live_lock = create_session_tmpdir(tmp_path)
    dead, dead_lock = create_session_tmpdir(tmp_path)
    dead_lock.close()
    try:
        assert remove_stale_session_tmpdirs(tmp_path) == [dead]
        assert live.exists() and not dead.exists()
    finally:
        live_lock.close()
        shutil.rmtree(live)


def test_unmarked_legacy_and_retired_harness_directories_are_kept(tmp_path: Path) -> None:
    legacy = tmp_path / "fieldkit-ci-legacy"
    retired = tmp_path / "fieldkit-harness-ci-legacy"
    legacy.mkdir()
    retired.mkdir()
    os.utime(legacy, (1, 1))
    os.utime(retired, (1, 1))

    assert remove_stale_session_tmpdirs(tmp_path) == []
    assert legacy.exists() and retired.exists()


def test_symlink_and_plain_file_with_session_prefix_are_kept(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "fieldkit-ci-link"
    link.symlink_to(target, target_is_directory=True)
    plain = tmp_path / "fieldkit-ci-notes.log"
    plain.write_text("log\n", encoding="utf-8")

    assert remove_stale_session_tmpdirs(tmp_path) == []
    assert link.is_symlink() and target.exists() and plain.exists()


def test_other_users_session_directory_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, lock = create_session_tmpdir(tmp_path)
    lock.close()
    owner = session.stat().st_uid
    monkeypatch.setattr("_session_tmpdirs.os.getuid", lambda: owner + 1)

    assert remove_stale_session_tmpdirs(tmp_path) == []
    assert session.exists()


def test_cleanup_failure_warns_and_does_not_block_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, lock = create_session_tmpdir(tmp_path)
    lock.close()

    def denied(path: Path) -> None:
        raise PermissionError(f"synthetic deletion denied: {path.name}")

    monkeypatch.setattr("_session_tmpdirs.shutil.rmtree", denied)
    with pytest.warns(RuntimeWarning, match="Could not remove stale pytest session directory"):
        assert remove_stale_session_tmpdirs(tmp_path) == []
    assert session.exists()
