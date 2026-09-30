"""fieldkit.watch.status — Shared run-status writer for all fieldkit watchers.

Writes a single ``watcher-run-status.json`` file in the watchers directory
with one entry per watcher, updated atomically when a run's status is persisted.

A cold-start agent can read this file to verify that all scheduled watchers
are alive and when they last ran, without parsing alert files or log output.

Schema
------
The file is a JSON object keyed by watcher name::

    {
      "backstory-health": {
        "last_run": "2026-05-27T06:45:01Z",
        "outcome": "ok",          # "ok" | "partial" | "fatal"
        "records_checked": 3,
        "alerts_generated": 1,
        "failures": 0,
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
- ``fatal``   — the run did not meaningfully succeed, including total scan
               failure or failure of required state, alert, or status persistence

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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, get_args

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
    return get_daily_run_snapshot(watcher).ran_today


def load_all_statuses() -> dict[str, dict[str, Any]]:
    """Return the full watcher-run-status.json contents, keyed by watcher name.

    Returns {} when the file is missing or corrupt. Used by `watch status` to
    render a summary table across all watchers.
    """
    return _load_run_status()


WatcherOutcome = Literal["ok", "partial", "fatal"]
RunStatusWriteResult = Literal["written", "skipped", "failed"]


@dataclass(frozen=True)
class WatcherRunResult:
    """Facts from one invocation, independent of aggregate admission policy."""

    outcome: WatcherOutcome
    completed: bool
    status_write: RunStatusWriteResult | None
    failure_code: Literal[1, 2, 3] = 1

    def __post_init__(self) -> None:
        if type(self.outcome) is not str or self.outcome not in get_args(WatcherOutcome):
            raise ValueError("outcome must be a canonical watcher outcome")
        if type(self.completed) is not bool:
            raise ValueError("completed must be an exact bool")
        if self.status_write is not None and (
            type(self.status_write) is not str or self.status_write not in get_args(RunStatusWriteResult)
        ):
            raise ValueError("status_write must be a canonical write result or None")
        if type(self.failure_code) is not int or self.failure_code not in get_args(Literal[1, 2, 3]):
            raise ValueError("failure_code must be an exact integer 1, 2, or 3")
        if self.status_write == "failed" and self.outcome != "fatal":
            raise ValueError("failed status write requires fatal outcome")

    @property
    def exit_code(self) -> Literal[0, 1, 2, 3]:
        """Derive the process status without storing a second authority."""
        return 0 if self.outcome == "ok" else self.failure_code

    @property
    def completed_partial(self) -> bool:
        """Whether this invocation supplies the facts for partial allowance."""
        return self.outcome == "partial" and self.exit_code == 1 and self.completed and self.status_write == "written"


def validate_watcher_result(result: object, *, dry_run: bool) -> WatcherRunResult:
    """Admit invocation facts using the same live persistence policy for every step."""
    if type(result) is not WatcherRunResult:
        return WatcherRunResult("fatal", False, None)
    if not dry_run and (
        result.status_write == "skipped"
        or (result.completed and result.outcome in ("ok", "partial") and result.status_write != "written")
    ):
        return WatcherRunResult("fatal", result.completed, result.status_write, result.failure_code)
    return result


def classify_watcher_outcome(*, checked: int, failures: int) -> WatcherOutcome:
    """Classify a completed scan without hiding record failures."""
    if failures == 0:
        return "ok"
    return "fatal" if checked == 0 else "partial"


def get_last_run_outcome(watcher: str) -> WatcherOutcome | None:
    """Return the outcome of the last run for ``watcher``, or None if not found.

    Returns ``None`` when the status file is missing, the watcher entry is
    absent, or the outcome field is not a recognised value.

    Args:
        watcher: Stable watcher name key (e.g. ``"pursuit-stalls"``).
    """
    return get_daily_run_snapshot(watcher).outcome


@dataclass(frozen=True)
class WatcherDailySnapshot:
    """Daily suppression facts read from one persisted record."""

    ran_today: bool
    outcome: WatcherOutcome | None


def get_daily_run_snapshot(watcher: str) -> WatcherDailySnapshot:
    """Load date and recognized outcome together; never certify invocation completion."""
    if watcher not in KNOWN_WATCHERS:
        log.warning("get_daily_run_snapshot: unknown watcher %r", watcher)
        return WatcherDailySnapshot(False, None)
    entry = _load_run_status().get(watcher)
    if not isinstance(entry, dict):
        return WatcherDailySnapshot(False, None)
    raw_outcome = entry.get("outcome")
    outcome: WatcherOutcome | None = None
    if raw_outcome == "ok":
        outcome = "ok"
    elif raw_outcome == "partial":
        outcome = "partial"
    elif raw_outcome == "fatal":
        outcome = "fatal"
    last_run = entry.get("last_run")
    ran_today = False
    if isinstance(last_run, str):
        try:
            run_date = datetime.strptime(last_run, "%Y-%m-%dT%H:%M:%SZ").date()
            ran_today = run_date == datetime.now(UTC).date()
        except ValueError:
            pass
    return WatcherDailySnapshot(ran_today, outcome)


def write_run_status(
    *,
    watcher: str,
    outcome: WatcherOutcome,
    records_checked: int,
    alerts_generated: int,
    failures: int,
    elapsed_seconds: float,
    dry_run: bool,
) -> RunStatusWriteResult:
    """Update watcher-run-status.json with this run's result.

    Written atomically via a .tmp rename. Returns an explicit result so each
    watcher can fail closed when status persistence is part of its contract.

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
        return "skipped"

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
        return "written"
    except (OSError, TypeError, ValueError):
        log.warning("watcher-run-status: write failed")
        return "failed"
