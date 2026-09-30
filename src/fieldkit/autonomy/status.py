"""Assemble a provenance-bearing snapshot of autonomous fieldkit operation."""

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any

from fieldkit.driver.status_types import RUN_OUTCOMES

_HEALTH_FRESHNESS_WINDOW = timedelta(hours=26)
MAX_STATUS_RECORD_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SourceObservation:
    """One source's observed availability, provenance, and selected fields."""

    state: str
    detail: str
    observed_at: str | None = None
    values: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"state": self.state, "detail": self.detail}
        if self.observed_at is not None:
            result["observed_at"] = self.observed_at
        if self.values is not None:
            result.update(self.values)
        return result


@dataclass(frozen=True)
class NextAction:
    """A deterministic recommendation derived from observed state."""

    kind: str
    reason: str
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "reason": self.reason, "evidence": self.evidence}


@dataclass(frozen=True)
class AutonomyStatus:
    """Immutable snapshot rendered by the CLI without mutating any source."""

    generated_at: str
    health: SourceObservation
    driver: SourceObservation
    admission: SourceObservation
    spend: SourceObservation
    next_action: NextAction

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "health": self.health.to_dict(),
            "driver": self.driver.to_dict(),
            "admission": self.admission.to_dict(),
            "spend": self.spend.to_dict(),
            "next_action": self.next_action.to_dict(),
        }


def _load_latest(
    path: Path, *, label: str, collection: str = "runs"
) -> tuple[dict[str, Any] | None, SourceObservation | None]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None, SourceObservation("malformed", f"{label} record is not a regular file")
            raw = stream.read(MAX_STATUS_RECORD_BYTES + 1)
        if len(raw) > MAX_STATUS_RECORD_BYTES:
            return None, SourceObservation("malformed", f"{label} record exceeds the size limit")
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return None, SourceObservation("missing", f"{label} record does not exist")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None, SourceObservation("malformed", f"cannot read {label} record")
    if not isinstance(payload, dict):
        return None, SourceObservation("malformed", f"{label} record is not an object")
    runs = payload.get(collection)
    if not isinstance(runs, list):
        return None, SourceObservation("malformed", f"{label} record has no {collection} list")
    entries = [entry for entry in runs if isinstance(entry, dict)]
    if len(entries) != len(runs):
        return None, SourceObservation("malformed", f"{label} record contains a non-object entry")
    if not entries:
        return None, SourceObservation("missing", f"{label} record has no {collection}")
    return entries[-1], None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _health_observation(data_root: Path, now: datetime) -> SourceObservation:
    record, error = _load_latest(data_root / "logs" / "health" / "health-run-status.json", label="health")
    if error is not None:
        return error
    if record is None:
        return SourceObservation("malformed", "health record could not be selected")
    observed_at = record.get("ts")
    observed = _parse_timestamp(observed_at)
    if observed is None:
        return SourceObservation("malformed", "health record has an invalid timestamp")
    failures = record.get("gate_failures")
    if not isinstance(failures, list) or not all(isinstance(item, str) for item in failures):
        return SourceObservation("malformed", "health record has invalid gate failures", str(observed_at))
    state = "stale" if now - observed > _HEALTH_FRESHNESS_WINDOW else "available"
    detail = (
        "latest daily health record is outside its freshness window" if state == "stale" else "latest health record"
    )
    return SourceObservation(
        state,
        detail,
        str(observed_at),
        {"outcome": str(record.get("outcome", "unknown")), "gate_failures": failures},
    )


def observe_driver(data_root: Path) -> SourceObservation:
    """Read the latest local driver evidence without invoking or starting a driver."""
    record, error = _load_latest(data_root / "logs" / "driver" / "driver-run-status.json", label="driver")
    if error is not None:
        return error
    if record is None:
        return SourceObservation("malformed", "driver record could not be selected")
    observed_at = record.get("ts")
    if _parse_timestamp(observed_at) is None:
        return SourceObservation("malformed", "driver record has an invalid timestamp")
    outcome = record.get("outcome")
    if not isinstance(outcome, str) or outcome not in RUN_OUTCOMES:
        return SourceObservation("malformed", "driver record has an invalid outcome", str(observed_at))
    failure = record.get("error", "")
    if not isinstance(failure, str):
        return SourceObservation("malformed", "driver record has an invalid error", str(observed_at))
    values: dict[str, object] = {"outcome": outcome}
    if failure:
        values["error"] = failure
    return SourceObservation("available", "latest demand-driven driver record", str(observed_at), values)


def observe_admission(data_root: Path) -> SourceObservation:
    """Read the latest admission decision, preserving missing versus malformed state."""
    latest, error = _load_latest(
        data_root / "driver" / "developer-admission.json", label="developer admission", collection="decisions"
    )
    if error is not None:
        return error
    if latest is None:
        return SourceObservation("malformed", "developer admission decision could not be selected")
    observed_at = latest.get("ts")
    if _parse_timestamp(observed_at) is None:
        return SourceObservation("malformed", "developer admission record has an invalid timestamp")
    allowed = latest.get("allowed")
    reason = latest.get("reason_code")
    detail = latest.get("detail")
    if not isinstance(allowed, bool) or not isinstance(reason, str) or not isinstance(detail, str):
        return SourceObservation("malformed", "developer admission record has an invalid decision", str(observed_at))
    return SourceObservation(
        "available",
        "latest developer admission decision",
        str(observed_at),
        {"allowed": allowed, "reason_code": reason, "detail": detail},
    )


def _spend_observation(spend_reader: Callable[[], float | None]) -> SourceObservation:
    try:
        amount = spend_reader()
    except Exception as exc:  # noqa: BLE001 - the reader boundary must remain observable
        return SourceObservation("unavailable", f"developer spend could not be read: {exc}")
    if amount is None or not isfinite(amount):
        return SourceObservation("unavailable", "developer spend could not be determined")
    return SourceObservation("available", "measured developer spend for the current UTC day", values={"usd": amount})


def _next_action(
    health: SourceObservation,
    driver: SourceObservation,
    admission: SourceObservation,
    spend: SourceObservation,
) -> NextAction:
    if health.state == "missing":
        return NextAction("health-record-missing", "No health evidence is available", health.detail)
    if health.state == "malformed":
        return NextAction("health-state-malformed", "Health evidence cannot be trusted", health.detail)
    if driver.state == "malformed":
        return NextAction("driver-state-malformed", "Driver evidence cannot be trusted", driver.detail)
    if admission.state == "malformed":
        return NextAction("admission-state-malformed", "Admission evidence cannot be trusted", admission.detail)
    if health.state == "stale":
        return NextAction("health-stale", "The daily health evidence is stale", health.detail)
    if driver.values is not None and driver.values.get("outcome") == "failed":
        return NextAction(
            "driver-failed", "The latest driver run failed", str(driver.values.get("error", driver.detail))
        )
    if admission.values is not None and admission.values.get("allowed") is False:
        return NextAction(
            "admission-denied",
            "The latest developer admission was denied",
            str(admission.values.get("reason_code", admission.detail)),
        )
    if health.values is not None and health.values.get("gate_failures"):
        return NextAction(
            "health-regressions-open", "The latest health run observed unresolved regressions", health.detail
        )
    if spend.state == "unavailable":
        return NextAction("spend-unavailable", "Developer spend cannot be verified", spend.detail)
    return NextAction(
        "none", "Recorded autonomy evidence needs no immediate action", "All available sources are current"
    )


def build_status(
    data_root: Path,
    *,
    now: datetime,
    spend_reader: Callable[[], float | None],
) -> AutonomyStatus:
    """Build a side-effect-free status snapshot from existing local records."""
    health = _health_observation(data_root, now)
    driver = observe_driver(data_root)
    admission = observe_admission(data_root)
    spend = _spend_observation(spend_reader)
    return AutonomyStatus(
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        health=health,
        driver=driver,
        admission=admission,
        spend=spend,
        next_action=_next_action(health, driver, admission, spend),
    )
