"""Compare a pursuit's stored Salesforce snapshot with live Salesforce values.

Pure comparison rules for ``fieldkit sf drift``. The command adapter fetches the
live opportunity and maps it to :class:`LiveOpportunity`; nothing here performs
I/O, so every flag is unit-testable from plain data.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, TypedDict

from fieldkit.pursuit.stages import ALL_STAGES, TERMINAL_STAGES
from fieldkit.pursuit.utils import _parse_monetary

DriftLevel = Literal["RED", "YELLOW"]
DriftStatus = Literal["RED", "YELLOW", "GREEN"]
DriftCode = Literal[
    "stage-mismatch",
    "sf-stage-drift",
    "close-date-drift",
    "acv-drift",
    "overdue",
    "closing-14d",
    "closing-30d",
    "sf-closed-local-open",
    "opportunity-not-found",
    "sf-fetch-failed",
]

# Days before an open opportunity's close date at which it is flagged.
RED_CLOSE_WINDOW_DAYS = 14
YELLOW_CLOSE_WINDOW_DAYS = 30


class LiveOpportunity(TypedDict):
    """The live Salesforce values drift detection compares against."""

    stage: str | None
    close_date: str | None
    consulting_acv: float | None
    is_closed: bool


@dataclass(frozen=True)
class DriftFlag:
    """One divergence between a pursuit file and Salesforce."""

    level: DriftLevel
    code: DriftCode
    detail: str


def normalize_stage(value: object) -> str:
    """Lower-case a stage name and collapse its whitespace."""
    return " ".join(str(value or "").split()).lower()


def _lifecycle_stage(sf_stage: str) -> str | None:
    """Return the fieldkit lifecycle stage a Salesforce stage name matches, if any.

    Organizations name stages freely; only names that coincide with fieldkit's
    lifecycle (``Propose``, ``Closed Won``) can be compared with a local stage.
    """
    candidate = sf_stage.replace(" ", "-")
    return candidate if candidate in ALL_STAGES else None


def _iso_day(value: object) -> str:
    """Return the ``YYYY-MM-DD`` part of a date-like value, or ``""``."""
    return str(value or "")[:10]


def _field(frontmatter: Mapping[str, Any], key: str) -> Any:
    """Read an ``sf_*`` field, accepting the legacy hyphenated spelling (``sf-stage``)."""
    value = frontmatter.get(key)
    return value if value is not None else frontmatter.get(key.replace("_", "-"))


def _whole_dollars(value: object) -> int | None:
    """Parse an amount to whole dollars: blank reads as 0, non-numeric or non-finite as None."""
    if value is None or value == "":
        return 0
    parsed = _parse_monetary(value) if isinstance(value, (str, int, float)) else None
    if parsed is None:
        return 0
    if not isinstance(parsed, float) or not math.isfinite(parsed):
        return None
    return round(parsed)


def _stage_flags(frontmatter: Mapping[str, Any], live: LiveOpportunity) -> list[DriftFlag]:
    flags: list[DriftFlag] = []
    local = normalize_stage(frontmatter.get("stage"))
    sf_stage = normalize_stage(live["stage"])
    stored = normalize_stage(_field(frontmatter, "sf_stage"))
    if live["is_closed"]:
        if local not in TERMINAL_STAGES:
            flags.append(DriftFlag("RED", "sf-closed-local-open", f"SF '{live['stage']}', local '{local}'"))
    else:
        lifecycle = _lifecycle_stage(sf_stage)
        if lifecycle and local and local != lifecycle:
            flags.append(DriftFlag("YELLOW", "stage-mismatch", f"local '{local}' != SF '{live['stage']}'"))
    if sf_stage and stored != sf_stage:
        flags.append(DriftFlag("YELLOW", "sf-stage-drift", f"stored sf_stage '{stored}' != live '{live['stage']}'"))
    return flags


def _snapshot_flags(frontmatter: Mapping[str, Any], live: LiveOpportunity) -> list[DriftFlag]:
    flags: list[DriftFlag] = []
    stored_close = _iso_day(_field(frontmatter, "sf_close_date"))
    live_close = _iso_day(live["close_date"])
    if live_close and stored_close != live_close:
        flags.append(DriftFlag("YELLOW", "close-date-drift", f"stored {stored_close or '—'} != live {live_close}"))
    stored_acv = _whole_dollars(_field(frontmatter, "sf_consulting_acv"))
    live_acv = _whole_dollars(live["consulting_acv"])
    if stored_acv is None:
        flags.append(DriftFlag("YELLOW", "acv-drift", "stored consulting ACV is not a number"))
    elif live_acv is not None and stored_acv != live_acv:
        flags.append(DriftFlag("YELLOW", "acv-drift", f"stored consulting ACV {stored_acv:,} != live {live_acv:,}"))
    return flags


def _close_window_flags(live: LiveOpportunity, today: date) -> list[DriftFlag]:
    if live["is_closed"] or not live["close_date"]:
        return []
    try:
        days = (date.fromisoformat(_iso_day(live["close_date"])) - today).days
    except ValueError:
        return []
    if days < 0:
        return [DriftFlag("RED", "overdue", f"close date {-days}d past, still open")]
    if days <= RED_CLOSE_WINDOW_DAYS:
        return [DriftFlag("RED", "closing-14d", f"closes in {days}d")]
    if days <= YELLOW_CLOSE_WINDOW_DAYS:
        return [DriftFlag("YELLOW", "closing-30d", f"closes in {days}d")]
    return []


def assess_drift(frontmatter: Mapping[str, Any], live: LiveOpportunity, today: date) -> list[DriftFlag]:
    """Return every divergence between a pursuit's frontmatter and its live opportunity.

    Local ``stage`` is compared only when the Salesforce stage name is a fieldkit
    lifecycle stage; ``sf-stage-drift`` compares the stored ``sf_stage`` snapshot,
    which uses Salesforce's own vocabulary. A blank amount and ``$0`` are equal.
    """
    return [*_stage_flags(frontmatter, live), *_snapshot_flags(frontmatter, live), *_close_window_flags(live, today)]


def drift_status(flags: Sequence[DriftFlag]) -> DriftStatus:
    """Collapse flags to the worst level: RED, then YELLOW, else GREEN."""
    levels = {flag.level for flag in flags}
    if "RED" in levels:
        return "RED"
    return "YELLOW" if levels else "GREEN"
