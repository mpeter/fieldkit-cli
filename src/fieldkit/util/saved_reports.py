"""Validation and truthful viewer outcomes for generated local reports."""

import webbrowser
from pathlib import Path

from fieldkit.util.text_snapshot import TextSnapshot, read_text_snapshot

MAX_SAVED_REPORT_BYTES = 4 * 1024 * 1024


def read_saved_report(path: Path, workspace: Path) -> TextSnapshot:
    """Validate a bounded report; configured workspace ancestors must stay stable.

    A browser subsequently resolves its own file URI: this validation is not
    a sandbox against another process replacing files after selection.
    """
    root = workspace.resolve()
    reports = root / "briefs"
    if reports.is_symlink() or path.parent.resolve() != reports or path.is_symlink():
        raise ValueError("Saved report must be a regular file in the workspace briefs directory")
    snapshot = read_text_snapshot(path, max_bytes=MAX_SAVED_REPORT_BYTES)
    if not snapshot.content.strip():
        raise ValueError("Saved report is empty")
    return snapshot


def open_saved_report(path: Path, *, no_open: bool) -> bool:
    """Return whether the browser accepted the URI, not whether it rendered it."""
    if no_open:
        return False
    try:
        return bool(webbrowser.open(path.as_uri()))
    except OSError:
        return False
