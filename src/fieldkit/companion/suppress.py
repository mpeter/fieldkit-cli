"""fieldkit.companion.suppress — what is currently retired from the feed.

Separate from the journal on purpose. The journal is append-only *evidence*
(what ran, what exit code, when) and the sole input to tier graduation; this
module is *control* (what the feed should not re-deliver right now). When one
JSONL line did both jobs, a fact could not be recorded without changing
behavior, and the only way to un-hide a wrongly-retired item was to delete a
line from the audit trail.

The attention feed is a **condition** feed, not an event feed: a stall alert
exists because the pursuit is still stalled. Having observed a condition once is
not a reason to stop reporting it while it is still true. So retirement here is
either time-bounded (a cooldown that heals itself) or backed by a durable
artifact the operator can see and delete.

Two sources, unioned by ``retired_item_ids``:

* **Cooldowns** (``companion-suppress.json``) — written only on success paths,
  with a TTL. An expired entry stops suppressing without any cleanup step.
* **Pending proposals** — at propose/act tier the proposal file *is* the record.
  Handle it and delete the file, and if the condition still holds it returns on
  the next pass, which is the correct behavior for a condition that survived
  your intervention.

Failures — auth (exit 2), timeouts, any nonzero, gate denials — never suppress.
They are journaled as evidence and the item stays live.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fieldkit.companion.outbox import proposal_item_ids
from fieldkit.util.atomic import locked_json_update

_SUPPRESS_FILENAME = "companion-suppress.json"

# How long a successful pass keeps an item out of the feed. Long enough that an
# hourly timer does not re-spawn the same enrichment subprocess all day, short
# enough that a still-true condition resurfaces the next morning.
DEFAULT_COOLDOWN = timedelta(hours=24)


def suppress_path(data_path: Path) -> Path:
    """Return the cooldown map path (``companion-suppress.json``)."""
    return data_path / _SUPPRESS_FILENAME


def _parse_until(raw: Any) -> datetime | None:
    """Return the parsed ``until`` timestamp, or None when unusable."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def active_cooldowns(data_path: Path, *, now: datetime | None = None) -> set[str]:
    """Return item_ids whose cooldown has not yet expired.

    A malformed or unparseable entry is treated as expired rather than as a
    permanent suppression — failing open here re-surfaces an item, which is the
    safe direction for an attention feed.
    """
    moment = now if now is not None else datetime.now(tz=UTC)
    path = suppress_path(data_path)
    if not path.is_file():
        return set()
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, dict):
        return set()

    active: set[str] = set()
    for item_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        until = _parse_until(entry.get("until"))
        if until is not None and until > moment:
            active.add(str(item_id))
    return active


def add_cooldown(
    data_path: Path,
    item_id: str,
    *,
    reason: str,
    ttl: timedelta | None = None,
    now: datetime | None = None,
) -> None:
    """Put *item_id* on cooldown for *ttl*, recording *reason*.

    Call only on a success path. Lock-serialized because `companion feed` and a
    timer-driven loop pass can run concurrently (implementation note).
    """
    moment = now if now is not None else datetime.now(tz=UTC)
    until = moment + (ttl if ttl is not None else DEFAULT_COOLDOWN)
    with locked_json_update(suppress_path(data_path)) as data:
        # Drop entries that expired before this write so the file cannot grow
        # without bound over a long-running install.
        for existing_id in [key for key, value in data.items() if isinstance(value, dict)]:
            existing_until = _parse_until(data[existing_id].get("until"))
            if existing_until is None or existing_until <= moment:
                del data[existing_id]
        data[item_id] = {
            "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": reason,
        }


def retired_item_ids(data_path: Path, *, now: datetime | None = None) -> set[str]:
    """Return every item_id the feed should currently withhold.

    The union of unexpired cooldowns and items with a proposal still sitting in
    the outbox. This is the single definition of "retired" — `companion feed`
    and the loop must agree, or the operator sees a different feed than the one
    the loop acted on.
    """
    return active_cooldowns(data_path, now=now) | proposal_item_ids(data_path)
