"""Persistence and acknowledgement state for the pursuit-stall watcher."""

import copy
import datetime
import json
import logging
from pathlib import Path
from typing import Any

from fieldkit.config import get_watchers_dir
from fieldkit.watch.state import merge_state

log = logging.getLogger(__name__)

_SNOOZE_DAYS = 7
_ALERT_TIERS = (7, 14, 30)
_DETECTED_DATE_MAX_AGE_DAYS = 90


def state_file() -> Path:
    """Return the persisted pursuit-stall state path."""
    return get_watchers_dir() / "pursuit-stall-state.json"


def coerce_date(value: object) -> datetime.date | None:
    """Return a supported date value as a date, or ``None`` when invalid."""
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def load_state() -> dict[str, Any]:
    """Load persisted stall state; return an empty mapping when unavailable."""
    path = state_file()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as file_handle:
            data = json.load(file_handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read state file %s: %s", path, exc, exc_info=True)
        return {}


def save_state(state: dict[str, Any], *, previous_state: dict[str, Any] | None = None) -> None:
    """Persist stall-state changes without overwriting concurrent updates."""
    merge_state(state_file(), state, previous_state or {})


def prune_stale_state(state: dict[str, Any], live_pursuit_paths: list[tuple[Path, int]]) -> dict[str, Any]:
    """Remove state entries for pursuits whose files no longer exist."""
    live_keys: set[str] = set()
    for path, _ in live_pursuit_paths:
        parts = path.parts
        pursuits_idx = next((i for i, part in enumerate(parts) if part == "pursuits"), None)
        if pursuits_idx is not None and pursuits_idx >= 1:
            live_keys.add(f"{parts[pursuits_idx - 1]}/{path.stem}")

    pruned: dict[str, Any] = {}
    for key, entry in state.items():
        if key in live_keys:
            pruned[key] = entry
        else:
            log.info("historic regression: pruning stale state entry for deleted pursuit: %s", key)
    return pruned


def snooze_pursuit(state_key: str, *, days: int = _SNOOZE_DAYS) -> None:
    """Set ``snoozed_until`` for a pursuit while retaining its other state."""
    state = load_state()
    previous_state = copy.deepcopy(state)
    entry: dict[str, Any] = state.get(state_key, {})
    snooze_until = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=days)).isoformat()
    entry["snoozed_until"] = snooze_until
    state[state_key] = entry
    save_state(state, previous_state=previous_state)
    log.info("Snoozed %s until %s (%d days)", state_key, snooze_until, days)


def is_snoozed(entry: dict[str, Any], *, current_stage: str) -> tuple[bool, str]:
    """Return whether a pursuit is snoozed, clearing stale snoozes in place."""
    raw = entry.get("snoozed_until")
    if not raw:
        return False, ""

    snooze_date = coerce_date(raw)
    if snooze_date is None:
        return False, ""

    prior_stage = str(entry.get("stage", "")).strip().lower()
    if prior_stage and prior_stage != current_stage.strip().lower():
        entry.pop("snoozed_until", None)
        log.info(
            "Snooze cleared for %s/%s — stage changed %r → %r",
            entry.get("account", "?"),
            entry.get("pursuit", "?"),
            prior_stage,
            current_stage,
        )
        return False, ""

    today = datetime.datetime.now(tz=datetime.UTC).date()
    if snooze_date > today:
        remaining = (snooze_date - today).days
        return True, f"snoozed until {snooze_date.isoformat()} ({remaining}d remaining)"

    entry.pop("snoozed_until", None)
    return False, ""


def _days_tier(days: int) -> int:
    """Return the highest alert tier crossed by a stalled pursuit."""
    tier = 0
    for threshold in _ALERT_TIERS:
        if days >= threshold:
            tier = threshold
    return tier


def should_suppress_alert(
    result: dict[str, Any],
    prior_state: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Return whether a stalled pursuit has already alerted at its current tier."""
    if prior_state is None:
        return False, ""

    prior_stage = prior_state.get("stage", "")
    if prior_stage and prior_stage != result["stage"]:
        return False, ""
    if not prior_state.get("is_stalled"):
        return False, ""

    alerted_tier = prior_state.get("alerted_days_tier", 0)
    current_tier = _days_tier(result["days_since_transition"])
    if current_tier > alerted_tier:
        return False, ""
    return True, f"already alerted at tier={alerted_tier}d (now {result['days_since_transition']}d)"


def apply_detected_transition_date(
    result: dict[str, Any],
    prior: dict[str, Any] | None,
    *,
    stage_changed: bool,
    today: datetime.date,
) -> dict[str, Any]:
    """Apply the bounded historic regression transition-date sentinel without mutating input."""
    if stage_changed:
        updated_result = dict(result)
        if updated_result["last_transition_date"] == (prior.get("last_transition_date", "") if prior else ""):
            updated_result["detected_transition_date"] = today.isoformat()
            log.info(
                "[historic regression] Storing detected_transition_date=%s for %s/%s "
                "(frontmatter last-transition not yet updated by advance skill)",
                today.isoformat(),
                updated_result["account"],
                updated_result["pursuit"],
            )
        else:
            updated_result.pop("detected_transition_date", None)
        return updated_result

    if prior is None:
        return result
    detected_date = prior.get("detected_transition_date", "")
    if not detected_date:
        return result
    if result["last_transition_date"] >= detected_date:
        log.debug(
            "[historic regression] Frontmatter last-transition (%s) caught up to detected date (%s) for %s/%s",
            result["last_transition_date"],
            detected_date,
            result["account"],
            result["pursuit"],
        )
        return dict(result)

    updated_result = dict(result)
    try:
        detected_datetime = datetime.date.fromisoformat(detected_date)
    except ValueError:
        log.warning(
            "[historic regression] Malformed detected_transition_date %r for %s/%s — clearing sentinel",
            detected_date,
            updated_result["account"],
            updated_result["pursuit"],
        )
        return updated_result

    sentinel_age = (today - detected_datetime).days
    if sentinel_age > _DETECTED_DATE_MAX_AGE_DAYS:
        log.info(
            "[historic regression] detected_transition_date %s expired (%d days old > %d-day TTL) for %s/%s",
            detected_date,
            sentinel_age,
            _DETECTED_DATE_MAX_AGE_DAYS,
            updated_result["account"],
            updated_result["pursuit"],
        )
        return updated_result

    updated_result["last_transition_date"] = detected_date
    updated_result["detected_transition_date"] = detected_date
    updated_result["days_since_transition"] = sentinel_age
    updated_result["is_stalled"] = sentinel_age > updated_result["threshold_days"]
    return updated_result


def detect_stage_change(
    result: dict[str, Any],
    prior: dict[str, Any] | None,
    prior_entry: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Reset a changed stage's local stall state and clear its snooze."""
    if prior is None:
        return result, False
    prior_stage = str(prior.get("stage", "")).strip().lower()
    if not prior_stage or prior_stage == result["stage"]:
        return result, False

    log.info(
        "Stage change detected for %s/%s: %r → %r — resetting stall timer",
        result["account"],
        result["pursuit"],
        prior_stage,
        result["stage"],
    )
    updated_result = dict(result)
    updated_result["days_since_transition"] = 0
    updated_result["is_stalled"] = False
    prior_entry.pop("snoozed_until", None)
    return updated_result, True


def build_stall_state_entry(
    result: dict[str, Any],
    prior: dict[str, Any] | None,
    prior_entry: dict[str, Any],
    file_threshold: int,
    *,
    stage_changed: bool,
    suppress: bool,
) -> dict[str, Any]:
    """Build the persisted entry after one pursuit has been evaluated."""
    alerted_tier = 0 if stage_changed else (prior.get("alerted_days_tier", 0) if prior else 0)
    if result["is_stalled"] and not suppress:
        alerted_tier = _days_tier(result["days_since_transition"])

    entry: dict[str, Any] = {
        "account": result["account"],
        "pursuit": result["pursuit"],
        "stage": result["stage"],
        "last_transition_date": result["last_transition_date"],
        "days_since_transition": result["days_since_transition"],
        "is_stalled": result["is_stalled"],
        "threshold_days": file_threshold,
        "alerted_days_tier": alerted_tier,
        "checked_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if "detected_transition_date" in result:
        entry["detected_transition_date"] = result["detected_transition_date"]
    if "snoozed_until" in prior_entry:
        entry["snoozed_until"] = prior_entry["snoozed_until"]
    return entry
