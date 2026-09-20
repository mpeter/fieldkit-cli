"""Waiting-on tracker — domain logic for detecting stale TASKS.md items.

Moved from ``commands/watch/waiting_on_tracker.py`` (watch-domain-migration, implementation change).
Contains the pure business logic for parsing the "## Waiting On" section and
generating escalation alerts. No Click imports — CLI wiring stays in
``commands/watch/waiting_on_tracker.py``.
"""

import datetime
import hashlib
import json
import logging
import re
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import get_fieldkit_home
from fieldkit.config import get_watchers_dir as get_watchers_dir
from fieldkit.watch.logging import watcher_logging
from fieldkit.watch.state import merge_state
from fieldkit.watch.state import state_write_failed as _state_write_failed
from fieldkit.watch.status import WatcherOutcome, write_run_status

log = logging.getLogger(__name__)

_DEFAULT_THRESHOLD_DAYS = 7
_DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)\s*$")
_SENT_DATE_RE = re.compile(r"—\s*sent\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)

# historic regression: detect comment-format placeholder dates like <!-- 2026-MM-DD -->
_COMMENT_DATE_RE = re.compile(r"<!--\s*(\d{4}-\d{2}-\d{2}[^-]*)\s*-->")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


@cache
def _alerts_file() -> Path:
    return get_watchers_dir() / "waiting-on-alerts.md"


@cache
def _state_file() -> Path:
    return get_watchers_dir() / "waiting-on-state.json"


# ---------------------------------------------------------------------------
# TASKS.md parsing
# ---------------------------------------------------------------------------


def _read_tasks_md() -> str | None:
    """Read TASKS.md from data root; return None if missing."""
    tasks_path = get_fieldkit_home() / "TASKS.md"
    if not tasks_path.exists():
        log.info("TASKS.md not found at %s — skipping waiting-on check", tasks_path)
        return None
    try:
        return tasks_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Cannot read TASKS.md: %s", exc)
        return None


def _extract_waiting_on_lines(content: str) -> list[str]:
    """Extract non-empty item lines from the '## Waiting On' section."""
    lines = content.splitlines()
    in_section = False
    items: list[str] = []
    for line in lines:
        if re.match(r"^##\s+Waiting On", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section:
            if re.match(r"^##\s+", line):
                break  # next section starts
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                items.append(stripped)
    return items


def _parse_date(match: re.Match[str], *, label: str) -> tuple[datetime.date | None, str | None]:
    """Parse a matched date, returning a skip reason when it is invalid."""
    try:
        return datetime.date.fromisoformat(match.group(1)), None
    except ValueError:
        return None, f"invalid {label} date {match.group(1)!r}"


def _item_date_with_reason(line: str) -> tuple[datetime.date | None, str | None]:
    """Extract a sent date first, then a trailing parenthesized date."""
    sent_match = _SENT_DATE_RE.search(line)
    if sent_match:
        return _parse_date(sent_match, label="sent")

    trailing_match = _DATE_RE.search(line)
    if trailing_match:
        return _parse_date(trailing_match, label="trailing")

    return None, "no recognized date"


def _item_date(line: str) -> datetime.date | None:
    """Extract a sent date first, then a trailing parenthesized date."""
    item_date, _ = _item_date_with_reason(line)
    return item_date


def _item_key(line: str) -> str:
    """Stable hash key for a waiting-on item (based on full text)."""
    return hashlib.sha256(line.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, Any]:
    if not _state_file().exists():
        return {}
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read state file %s: %s", _state_file(), exc)
        return {}


def _save_state(state: dict[str, Any], *, previous_state: dict[str, Any]) -> None:
    """Persist tracker-state changes without overwriting concurrent updates."""
    merge_state(_state_file(), state, previous_state)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def _ensure_alerts_header() -> None:
    get_watchers_dir().mkdir(parents=True, exist_ok=True)
    if not _alerts_file().exists():
        _alerts_file().write_text(
            "# Waiting-On Escalation Alerts\n\nAutomated alerts written by waiting_on_tracker.py.\n",
            encoding="utf-8",
        )


def _append_alert(item: str, days: int, item_date: datetime.date, *, dry_run: bool) -> None:
    now_utc = datetime.datetime.now(datetime.UTC)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_label = now_utc.strftime("%Y-%m-%d")
    lines = [
        "",
        f"## {date_label} — Waiting On escalation",
        "",
        f"- **Item:** {item}",
        f"- **Since:** {item_date.isoformat()} ({days} days ago)",
        f"- **Detected at:** {ts}",
        "",
    ]
    alert_text = "\n".join(lines)
    if dry_run:
        log.info("[DRY RUN] Would append alert:\n%s", alert_text)
        return
    _ensure_alerts_header()
    with _alerts_file().open("a", encoding="utf-8") as fh:
        fh.write(alert_text)
    log.info("Alert emitted: %s (%d days silent)", item[:80], days)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _run(*, threshold: int, dry_run: bool, as_json: bool = False) -> int:
    with watcher_logging("waiting-on-tracker"):
        return _run_inner(threshold=threshold, dry_run=dry_run, as_json=as_json)


def _emit_run_json(
    *, outcome: WatcherOutcome, checked: int, alerts: int, failures: int, elapsed: float, dry_run: bool
) -> None:
    """Emit the run-status document on stdout.

    historic regression: ``outcome`` is reported verbatim; only ``"fatal"`` denotes a failed run.

    ``failures`` is a parameter rather than a hardcoded ``0`` so it is carried from
    the same state that produced ``outcome`` — the two describe one run and must not
    be able to contradict each other. Matches the ``draft_queue`` signature.
    """
    print(
        json.dumps(
            {
                "watcher": "waiting-on-tracker",
                "outcome": outcome,
                "records_checked": checked,
                "alerts_generated": alerts,
                "failures": failures,
                "elapsed_seconds": round(elapsed, 1),
                "dry_run": dry_run,
            },
            indent=2,
            default=str,
        )
    )


def _run_inner(*, threshold: int, dry_run: bool, as_json: bool = False) -> int:
    """Inner logic (separated for testability); returns POSIX exit code.

    ``as_json`` emits the run-status document on stdout; the exit code is unaffected.
    """
    import time

    start = time.monotonic()
    today = datetime.datetime.now(tz=datetime.UTC).date()

    content = _read_tasks_md()
    if content is None:
        # An absent TASKS.md is nothing to check, not a failure — one binding feeds
        # both the persisted status and the emitted document.
        no_file_outcome: WatcherOutcome = "ok"
        no_file_failures = 0
        write_run_status(
            watcher="waiting-on-tracker",
            outcome=no_file_outcome,
            records_checked=0,
            alerts_generated=0,
            failures=no_file_failures,
            elapsed_seconds=0.0,
            dry_run=dry_run,
        )
        if as_json:
            _emit_run_json(
                outcome=no_file_outcome,
                checked=0,
                alerts=0,
                failures=no_file_failures,
                elapsed=0.0,
                dry_run=dry_run,
            )
        return 0

    items = _extract_waiting_on_lines(content)
    log.info("Waiting On section: %d item(s) found", len(items))

    state = _load_state()
    updated_state: dict[str, Any] = dict(state)

    alerts = 0
    checked = 0

    for item in items:
        item_date, skip_reason = _item_date_with_reason(item)
        if item_date is None:
            # historic regression: warn on comment-format placeholder dates instead of silently skipping
            if _COMMENT_DATE_RE.search(item):
                log.warning(
                    "TASKS.md item has comment-format placeholder date — update to (YYYY-MM-DD) format: %s",
                    item[:80],
                )
            else:
                log.info("Skipping (%s): %s", skip_reason, item[:80])
            continue

        checked += 1
        days = (today - item_date).days
        key = _item_key(item)
        log.info("Checking item: %s | days_silent=%d", item[:80], days)

        if days >= threshold:
            already_alerted = state.get(key, {}).get("alerted", False)
            if not already_alerted:
                _append_alert(item, days, item_date, dry_run=dry_run)
                alerts += 1
            else:
                log.info("Suppressed (already alerted): %s", item[:80])

        updated_state[key] = {
            "item": item,
            "item_date": item_date.isoformat(),
            "days_silent": days,
            "alerted": state.get(key, {}).get("alerted", False) or (days >= threshold),
            "checked_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    state_write_failed = _state_write_failed(
        dry_run=dry_run,
        write=lambda: _save_state(updated_state, previous_state=state),
        logger=log,
        message="State file write failed — waiting-on state not persisted.",
    )

    elapsed = time.monotonic() - start
    log.info(
        "Run complete: checked=%d alerts=%d elapsed=%.1fs dry_run=%s",
        checked,
        alerts,
        elapsed,
        dry_run,
    )
    # Items that fail to yield a date are skipped, not counted against the run, so
    # this scan has no failure mode short of an exception: outcome and failures are
    # derived together here and handed to both sinks, never restated at the sink.
    failures = int(state_write_failed)
    outcome: WatcherOutcome = "fatal" if state_write_failed else "ok"
    write_run_status(
        watcher="waiting-on-tracker",
        outcome=outcome,
        records_checked=checked,
        alerts_generated=alerts,
        failures=failures,
        elapsed_seconds=elapsed,
        dry_run=dry_run,
    )
    if as_json:
        _emit_run_json(
            outcome=outcome, checked=checked, alerts=alerts, failures=failures, elapsed=elapsed, dry_run=dry_run
        )
        return 1 if state_write_failed else 0
    # historic regression: log dry-run summary so --dry-run is useful as a preview.
    if dry_run:
        log.info("[DRY-RUN] waiting-on-tracker: %d item(s) scanned, %d alert(s) would fire", checked, alerts)
    return 1 if state_write_failed else 0
