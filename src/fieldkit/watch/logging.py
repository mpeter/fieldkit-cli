"""lib/watcher_logging.py — Per-run log file setup for fieldkit watchers.

Each watcher run writes a timestamped log file to:
  <data_root>/logs/watchers/<watcher_name>-YYYY-MM-DD-HHMMSS.log

Usage
-----
    from fieldkit.watch.logging import setup_watcher_logging, teardown_watcher_logging

    def _run_my_watcher(...) -> int:
        log_path, handler, state = setup_watcher_logging("my-watcher")
        log.info("Log file: %s", log_path)
        try:
            ...
        finally:
            teardown_watcher_logging(handler, state)

The FileHandler is installed on the ``fieldkit.watch`` logger at DEBUG level so
watcher-module loggers inherit it without changing process-wide root logging.
``teardown_watcher_logging()`` removes the handler to prevent bleed between runs
or test cases.

Files older than 7 days are pruned on each call to setup_watcher_logging().

Alert dedup helpers
-------------------
``_alert_key_exists(alert_file, date_str, pursuit_slug)`` checks whether an
alert block for a given date and pursuit slug already exists in an alert file.
Watchers call this before appending to avoid duplicate alerts.

State pruning helpers
---------------------
``_prune_state(state, data_root)`` removes state entries for pursuit files
that no longer exist on disk and returns the pruned state with a removed count.
"""

import logging
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fieldkit.config import get_fieldkit_home

_KEEP_DAYS = 7
_WATCHER_LOGGER_NAME = "fieldkit.watch"
_LOG_FILENAME_RE = re.compile(r"^.+-\d{4}-\d{2}-\d{2}-\d{6,}\.log$")

# Matches stems ending with exactly 12 trailing digits (HHMMSS + 6 microsecond
# digits).  Used by list_recent_logs() for same-second deduplication (implementation change).
_MICROSECOND_SUFFIX_RE = re.compile(r"^(.+\d{6})\d{6}$")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatcherLoggerState:
    """Named watcher logger configuration to restore after a run."""

    level: int
    propagate: bool


def _logs_dir() -> Path:
    """Return the watcher logs directory (creates parent on first call)."""
    return Path(get_fieldkit_home()) / "logs" / "watchers"


def _prune_old_logs(logs_dir: Path, watcher_name: str, keep_days: int = _KEEP_DAYS) -> None:
    """Delete log files for *watcher_name* older than *keep_days* days."""
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    prefix = f"{watcher_name}-"
    for log_file in logs_dir.glob(f"{prefix}*.log"):
        if not _LOG_FILENAME_RE.match(log_file.name):
            continue
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime, tz=UTC)
            if mtime < cutoff:
                log_file.unlink()
                log.debug("Pruned old log file: %s", log_file)
        except OSError:
            log.debug("Could not stat/unlink %s — skipping", log_file)


def setup_watcher_logging(
    watcher_name: str,
) -> tuple[Path, logging.FileHandler, WatcherLoggerState]:
    """Set up a per-run log file for *watcher_name*.

    Creates the logs directory, prunes old files, and installs a FileHandler on
    the ``fieldkit.watch`` namespace at DEBUG level. Propagation is disabled so
    watcher messages do not duplicate through root terminal handlers.

    Always pass the returned state to :func:`teardown_watcher_logging` in a
    ``finally`` block so the handler is removed and the namespace configuration
    is restored.
    """
    logs_dir = _logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)

    _prune_old_logs(logs_dir, watcher_name)

    ts = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S%f")
    log_path = logs_dir / f"{watcher_name}-{ts}.log"

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)

    watcher_logger = logging.getLogger(_WATCHER_LOGGER_NAME)
    state = WatcherLoggerState(level=watcher_logger.level, propagate=watcher_logger.propagate)
    watcher_logger.addHandler(handler)
    watcher_logger.setLevel(logging.DEBUG)
    watcher_logger.propagate = False

    log.info("Watcher log: %s", log_path)
    return log_path, handler, state


def teardown_watcher_logging(
    handler: logging.FileHandler,
    state: WatcherLoggerState,
) -> None:
    """Remove *handler* and restore the matching watcher logger state."""
    watcher_logger = logging.getLogger(_WATCHER_LOGGER_NAME)
    watcher_logger.removeHandler(handler)
    handler.close()
    watcher_logger.setLevel(state.level)
    watcher_logger.propagate = state.propagate


@contextmanager
def watcher_logging(watcher_name: str) -> Generator[None, None, None]:
    """Context manager that sets up and tears down watcher logging.

    Guarantees ``teardown_watcher_logging()`` is called even if the watcher
    body raises an exception — preventing permanent logging suppression (historic regression).

    Usage::

        with watcher_logging("morning-brief"):
            run_watcher()

    Args:
        watcher_name: The watcher name passed to :func:`setup_watcher_logging`.

    Yields:
        Nothing — the caller's body runs inside the try/finally block.
    """
    _log_path, log_handler, state = setup_watcher_logging(watcher_name)
    try:
        yield
    finally:
        teardown_watcher_logging(log_handler, state)


def list_recent_logs(watcher_name: str | None = None, *, n: int = 10) -> list[Path]:
    """Return up to *n* recent log files, newest first.

    If *watcher_name* is given, filter to that watcher's files.

    implementation change: Deduplicates by same-second prefix.  Filenames use microsecond
    precision (``%Y-%m-%d-%H%M%S%f``); same-second reruns produce two files
    that differ only in the last 6 characters of the stem.  After sorting by
    mtime (newest first), we keep only the first file seen per same-second
    prefix, so ``watch logs`` shows one entry per logical run.
    """
    logs_dir = _logs_dir()
    if not logs_dir.exists():
        return []

    pattern = f"{watcher_name}-*.log" if watcher_name else "*.log"
    files = [f for f in logs_dir.glob(pattern) if _LOG_FILENAME_RE.match(f.name)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # implementation change: deduplicate by same-second prefix.  The stem format produced by
    # setup_watcher_logging is ``{watcher}-YYYY-MM-DD-HHMMSS{ffffff}`` (12
    # trailing digits).  We use _MICROSECOND_SUFFIX_RE to detect this format
    # and strip the last 6 microsecond digits to get the same-second prefix.
    # Stems that do not match (e.g. legacy 6-digit format without microseconds)
    # are used as-is — they cannot produce same-second duplicates.
    # Because files are sorted newest-first, the first occurrence per prefix
    # is the most recent.
    seen_prefixes: set[str] = set()
    deduped: list[Path] = []
    for f in files:
        m = _MICROSECOND_SUFFIX_RE.match(f.stem)
        prefix = m.group(1) if m else f.stem
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            deduped.append(f)

    return deduped[:n]


# ---------------------------------------------------------------------------
# Alert dedup helper
# ---------------------------------------------------------------------------


def _is_date_header(line: str) -> bool:
    """Return True if *line* is a date header in the watcher alert log format.

    Date headers delimit alert blocks. The format is ``## YYYY-MM-DD`` (a
    Markdown H2 heading followed by an ISO-8601 date string).

    Args:
        line: A single line from the alert file (may include trailing newline).

    Returns:
        True when the stripped line starts with ``## `` and is long enough to
        contain at least a year (``## YYYY`` = 7 characters minimum).
    """
    stripped = line.strip()
    # Minimum: "## YYYY" = 7 chars; full date "## YYYY-MM-DD" = 13 chars.
    return stripped.startswith("## ") and len(stripped) > 6


def _alert_key_exists(alert_file: Path, date_str: str, pursuit_slug: str) -> bool:
    """Return True if an alert block for *date_str* + *pursuit_slug* already exists.

    Reads *alert_file* line-by-line (returns False if the file is missing).
    Looks for a heading line starting with ``## {date_str}``.  When found,
    scans subsequent lines until the next date header (or EOF) for a line that
    contains *pursuit_slug*, scoping the check to that date's block.

    G2b: Replaced fixed 50-line lookahead with scan-to-next-date-header logic.
    This correctly handles alert blocks of any size — the previous fixed window
    caused duplicate alerts when a block exceeded 50 lines (historic regression partial fix).

    Args:
        alert_file: Path to the markdown alert file (e.g. pursuit-stall-alerts.md).
            Returns False immediately if the file does not exist.
        date_str: ISO-8601 date string (``YYYY-MM-DD``) identifying the block.
        pursuit_slug: The pursuit stem / slug to search for within the block
            (e.g. ``"my-deal"``).

    Returns:
        True  when a block for *date_str* already contains *pursuit_slug*.
        False when the file is absent, the date heading is not found, or
              *pursuit_slug* does not appear within the block.
    """
    if not alert_file.exists():
        return False

    date_header = f"## {date_str}"
    try:
        with alert_file.open(encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        log.warning("Could not read alert file %s: %s", alert_file, exc)
        return False

    for idx, line in enumerate(lines):
        if not line.startswith(date_header):
            continue
        # Found the date header — scan until the next date header (or EOF).
        # Use a word-boundary-aware pattern so "deal" does not match inside
        # "big-deal".  Slugs are hyphen-separated identifiers, so we treat any
        # character that is NOT alphanumeric or a hyphen as a boundary.
        slug_pattern = r"(?<![A-Za-z0-9-])" + re.escape(pursuit_slug) + r"(?![A-Za-z0-9-])"
        for lookahead_line in lines[idx + 1 :]:
            # Stop at the next date header — we've left this block.
            if _is_date_header(lookahead_line):
                break
            if re.search(slug_pattern, lookahead_line):
                log.debug(
                    "Alert key exists for date=%s slug=%s — skipping write",
                    date_str,
                    pursuit_slug,
                )
                return True
        # Date header matched but slug not found within the block — not a dup
        return False

    return False


# ---------------------------------------------------------------------------
# State pruning helper
# ---------------------------------------------------------------------------


def _prune_state(state: dict[str, Any], data_root: Path) -> tuple[dict[str, Any], int]:
    """Remove state entries for pursuit files that no longer exist on disk.

    State keys use the format ``"<account>/<pursuit-stem>"`` (e.g.
    ``"acme-corp/virtualization-deal"``).  For each key the corresponding
    pursuit file is expected at::

        <data_root>/accounts/<account>/pursuits/<pursuit-stem>.md

    Keys whose file is absent are dropped from the returned state.

    Args:
        state: Current persisted state dict (keys are ``"account/pursuit"``).
        data_root: Root of the fieldkit data directory (from :func:`get_fieldkit_home`).

    Returns:
        A ``(pruned_state, removed_count)`` tuple where *pruned_state* contains
        only entries with a corresponding live file and *removed_count* is the
        number of entries that were removed.
    """
    pruned: dict[str, Any] = {}
    removed_count = 0

    for key, entry in state.items():
        # Keys must be "account/pursuit"; skip malformed entries
        parts = key.split("/", 1)
        if len(parts) != 2:
            log.warning("Malformed state key %r — keeping to avoid data loss", key)
            pruned[key] = entry
            continue

        account, pursuit_stem = parts

        # Guard: reject components that contain path separators.  A legitimate
        # account slug or pursuit stem never contains "/" or "\"; their presence
        # indicates a tampered state file attempting path traversal.
        if "/" in account or "\\" in account or "/" in pursuit_stem or "\\" in pursuit_stem:
            log.warning("State key %r contains path separator — skipping", key)
            continue

        pursuit_path = data_root / "accounts" / account / "pursuits" / f"{pursuit_stem}.md"

        # Guard: verify the resolved path stays within the accounts directory.
        # Segments like ".." in account or pursuit_stem could otherwise escape
        # the expected root even without a literal "/" separator (e.g. on some
        # platforms).  Resolving before the .exists() call prevents information
        # disclosure (confirming whether arbitrary paths exist on disk).
        expected_root = (data_root / "accounts").resolve()
        try:
            pursuit_path.resolve().relative_to(expected_root)
        except ValueError:
            log.warning("State key %r resolves outside accounts dir — skipping", key)
            continue

        if pursuit_path.exists():
            pruned[key] = entry
        else:
            removed_count += 1
            log.info("Pruning stale state entry for deleted pursuit: %s", key)

    return pruned, removed_count
