"""fieldkit.watch.status — Shared run-status writer for all fieldkit watchers.

Writes a single ``watcher-run-status.json`` file in the watchers directory
with one entry per watcher, updated atomically on every successful run.

A cold-start agent can read this file to verify that all scheduled watchers
are alive and when they last ran, without parsing alert files or log output.

Schema
------
The file is a JSON object keyed by watcher name::

    {
      "backstory-health": {
        "last_run": "2026-05-27T06:45:01Z",
        "outcome": "ok",          # "ok" | "partial" | "fatal"
        "accounts_checked": 3,
        "alerts_generated": 1,
        "api_failures": 0,
        "elapsed_seconds": 4.2,
        "dry_run": false
      },
      "pursuit-stalls": { ... },
      "slack-threads": { ... },
      "morning-brief": { ... }
    }

Outcome classification
----------------------
- ``ok``      — all records processed, zero failures
- ``partial`` — some records processed, some failures (retry may help)
- ``fatal``   — zero records processed (pipeline did not run meaningfully)

Usage
-----
    from fieldkit.watch.status import write_run_status

    write_run_status(
        watcher="pursuit-stalls",
        outcome="ok",
        records_checked=12,
        alerts_generated=2,
        failures=0,
        elapsed_seconds=1.8,
        dry_run=False,
    )
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from fieldkit.config import get_fieldkit_home
from fieldkit.util.atomic import locked_json_update
from fieldkit.watch.constants import KNOWN_WATCHERS

log = logging.getLogger(__name__)


def _load_run_status() -> dict[str, Any]:
    """Load the current watcher-run-status.json, returning {} on missing or corrupt file.

    If the config file is missing or invalid (ConfigError), returns {} so the
    write path also catches the same error gracefully via OSError swallowing.
    """
    run_status_file = get_fieldkit_home() / "watchers" / "watcher-run-status.json"
    if not run_status_file.exists():
        return {}
    try:
        with run_status_file.open(encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                return loaded
    except (OSError, json.JSONDecodeError):
        pass  # stale or corrupt — overwrite cleanly
    return {}


def was_run_today(watcher: str) -> bool:
    """Return True if ``watcher`` was last run on today's UTC date.

    Reads ``watcher-run-status.json`` and compares the ``last_run`` date
    component against today's UTC date.  Returns False when the file is
    missing, the entry is absent, or the timestamp cannot be parsed.

    Args:
        watcher: Stable watcher name key (e.g. ``"run-all"``).
    """
    if watcher not in KNOWN_WATCHERS:
        log.warning("was_run_today: unknown watcher %r — returning False", watcher)
        return False
    status = _load_run_status()
    entry = status.get(watcher)
    if not entry:
        return False
    last_run = entry.get("last_run", "")
    try:
        # Timestamps are stored as ISO-8601 UTC: "2026-06-06T06:45:01Z"
        run_date = datetime.strptime(last_run, "%Y-%m-%dT%H:%M:%SZ").date()
        return run_date == datetime.now(UTC).date()
    except ValueError:
        return False


def load_all_statuses() -> dict[str, dict[str, Any]]:
    """Return the full watcher-run-status.json contents, keyed by watcher name.

    Returns {} when the file is missing or corrupt. Used by `watch status` to
    render a summary table across all watchers.
    """
    return _load_run_status()


WatcherOutcome = Literal["ok", "partial", "fatal"]


def get_last_run_outcome(watcher: str) -> WatcherOutcome | None:
    """Return the outcome of the last run for ``watcher``, or None if not found.

    Returns ``None`` when the status file is missing, the watcher entry is
    absent, or the outcome field is not a recognised value.

    Args:
        watcher: Stable watcher name key (e.g. ``"pursuit-stalls"``).
    """
    status = _load_run_status()
    entry = status.get(watcher)
    if not entry:
        return None
    outcome = entry.get("outcome")
    if outcome == "ok":
        return "ok"
    if outcome == "partial":
        return "partial"
    if outcome == "fatal":
        return "fatal"
    return None


def write_run_status(
    *,
    watcher: str,
    outcome: WatcherOutcome,
    records_checked: int,
    alerts_generated: int,
    failures: int,
    elapsed_seconds: float,
    dry_run: bool,
) -> None:
    """Update watcher-run-status.json with this run's result.

    Written atomically via a .tmp rename.  Silently logs a warning on any
    filesystem error so a disk issue never blocks the main watcher pipeline.

    Args:
        watcher:          Stable watcher name key (e.g. ``"backstory-health"``).
        outcome:          ``"ok"`` | ``"partial"`` | ``"fatal"``.
        records_checked:  Number of accounts/files/threads examined.
        alerts_generated: Number of alert entries written.
        failures:         Number of records that could not be processed.
        elapsed_seconds:  Wall-clock duration of the run.
        dry_run:          If True, the entry is not written (dry-run results
                          are not meaningful to persist).
    """
    if dry_run:
        return

    watchers_dir = get_fieldkit_home() / "watchers"
    run_status_file = watchers_dir / "watcher-run-status.json"

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry: dict[str, Any] = {
        "last_run": ts,
        "outcome": outcome,
        "records_checked": records_checked,
        "alerts_generated": alerts_generated,
        "failures": failures,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "dry_run": False,
    }

    try:
        with locked_json_update(run_status_file) as existing:
            existing[watcher] = entry
        log.debug(
            "watcher-run-status: updated watcher=%s outcome=%s elapsed=%.1fs",
            watcher,
            outcome,
            elapsed_seconds,
        )
    except OSError as exc:
        log.warning(
            "watcher-run-status: could not write %s — %s",
            run_status_file,
            exc,
            exc_info=True,
        )
