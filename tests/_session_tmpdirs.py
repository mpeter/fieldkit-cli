"""Session temp directories left behind by pytest runs that never finished.

``pytest_configure`` creates a config directory for every run, and
``pytest_unconfigure`` removes it. A run that is killed or times out never reaches
unconfigure, so the directory stays in the system temp directory, where nothing
else ages it out fast enough. Sweeping stale ones at the start of the next run
keeps that leak bounded without depending on how the previous run ended.
"""

import os
import shutil
import time
from pathlib import Path

SESSION_TMPDIR_PREFIXES = ("fieldkit-ci-",)

# No test session runs this long, so an older directory belongs to a run that was
# killed. Sessions running concurrently in other worktrees are always younger.
STALE_SESSION_TMPDIR_SECONDS = 24 * 60 * 60


def remove_stale_session_tmpdirs(parent: Path, *, now: float | None = None) -> list[Path]:
    """Remove this user's abandoned session directories under ``parent``.

    Returns the directories actually removed, sorted; one that deletion could not
    fully remove is left out. Symlinks, plain files, other users' directories, and
    directories younger than the staleness window are left alone.
    """
    cutoff = (time.time() if now is None else now) - STALE_SESSION_TMPDIR_SECONDS
    removed: list[Path] = []
    for prefix in SESSION_TMPDIR_PREFIXES:
        for path in parent.glob(f"{prefix}*"):
            try:
                if path.is_symlink() or not path.is_dir():
                    continue
                status = path.stat()
            except OSError:
                continue
            if status.st_uid != os.getuid() or status.st_mtime >= cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.lexists(path):
                removed.append(path)
    return sorted(removed)
