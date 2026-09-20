"""Fail-closed admission control for unattended developer automation."""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fieldkit.config import get_fieldkit_data
from fieldkit.driver.spend import evaluate_daily_developer_spend_cap
from fieldkit.util.atomic import locked_json_update

ADMITTED_JOBS = frozenset(
    {
        "assayer",
        "developer-health",
        "developer-pulse",
        "developer-session-monitor",
        "driver",
        "foreman",
        "proctor",
    }
)
_MAX_LEASE_AGE = timedelta(hours=3)


@dataclass(frozen=True)
class AdmissionDecision:
    """The auditable result of a developer-job admission attempt."""

    allowed: bool
    job: str
    reason_code: str
    detail: str
    cap_usd: float | None = None
    spend_usd: float | None = None


def _ledger_path() -> Path:
    return get_fieldkit_data() / "driver" / "developer-admission.json"


def _configured_positive_int(name: str) -> tuple[int | None, str | None]:
    raw = os.environ.get(name)
    if raw is None:
        return None, f"{name} is required for unattended developer automation"
    try:
        value = int(raw)
    except ValueError:
        return None, f"{name}={raw!r} is not an integer"
    if value < 1:
        return None, f"{name}={raw!r} must be at least 1"
    return value, None


def _denied(
    job: str, reason_code: str, detail: str, *, cap_usd: float | None = None, spend_usd: float | None = None
) -> AdmissionDecision:
    return AdmissionDecision(False, job, reason_code, detail, cap_usd, spend_usd)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lease_is_stale(active: dict[str, object]) -> bool:
    """Return whether an orphaned lease has exceeded the bounded session window."""
    started_at = active.get("started_at")
    if not isinstance(started_at, str):
        return True
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return True
    return datetime.now(UTC) - started > _MAX_LEASE_AGE


def _append_decision(ledger: dict[str, object], decision: AdmissionDecision) -> None:
    existing = ledger.get("decisions", [])
    decisions = list(existing) if isinstance(existing, list) else []
    decisions.append(
        {
            "job": decision.job,
            "allowed": decision.allowed,
            "reason_code": decision.reason_code,
            "detail": decision.detail,
            "ts": _timestamp(),
            "cap_usd": decision.cap_usd,
            "spend_usd": decision.spend_usd,
        }
    )
    ledger["decisions"] = decisions[-200:]


def _record(decision: AdmissionDecision) -> AdmissionDecision:
    with locked_json_update(_ledger_path()) as ledger:
        _append_decision(ledger, decision)
    return decision


def admit(job: str) -> AdmissionDecision:
    """Acquire the global developer-job lease after fail-closed budget checks."""
    if job not in ADMITTED_JOBS:
        return _record(_denied(job, "job-not-admitted", f"{job!r} is not an approved developer job"))
    if os.environ.get("CLAUDE_CODE_USE_VERTEX"):
        return _record(
            _denied(job, "vertex-env-present", "CLAUDE_CODE_USE_VERTEX must be unset for developer automation")
        )

    daily_limit, limit_error = _configured_positive_int("FIELDKIT_DEVELOPER_DAILY_RUN_LIMIT")
    if limit_error:
        return _record(_denied(job, "daily-run-limit-unset", limit_error))
    if daily_limit is None:  # Defensive: _configured_positive_int returned no error.
        return _record(_denied(job, "daily-run-limit-unset", "daily run limit could not be determined"))
    spend_check = evaluate_daily_developer_spend_cap(os.environ.get("FIELDKIT_DEVELOPER_SPEND_CAP"), required=True)
    if not spend_check.allowed:
        return _record(
            _denied(
                job,
                spend_check.reason_code,
                spend_check.detail,
                cap_usd=spend_check.cap_usd,
                spend_usd=spend_check.spend_usd,
            )
        )

    now = _timestamp()
    today = now[:10]
    with locked_json_update(_ledger_path()) as ledger:
        active = ledger.get("active")
        if isinstance(active, dict):
            if _lease_is_stale(active):
                stale_job = active.get("job", "unknown")
                ledger.pop("active", None)
                reason = "stale-lease-reaped" if isinstance(active.get("started_at"), str) else "malformed-lease-reaped"
                _append_decision(
                    ledger,
                    AdmissionDecision(
                        True,
                        str(stale_job),
                        reason,
                        f"reaped stale or malformed developer lease older than {_MAX_LEASE_AGE}",
                    ),
                )
            else:
                held_job = active.get("job", "unknown")
                decision = _denied(
                    job,
                    "lease-held",
                    f"developer lease is held by {held_job!r}",
                    cap_usd=spend_check.cap_usd,
                    spend_usd=spend_check.spend_usd,
                )
                _append_decision(ledger, decision)
                return decision
        all_runs = ledger.get("runs", [])
        runs = (
            [
                run
                for run in all_runs
                if isinstance(run, dict)
                and isinstance(run.get("started_at"), str)
                and run["started_at"].startswith(today)
            ]
            if isinstance(all_runs, list)
            else []
        )
        if len(runs) >= daily_limit:
            decision = _denied(
                job,
                "daily-run-limit-reached",
                f"daily developer run limit {daily_limit} has been reached",
                cap_usd=spend_check.cap_usd,
                spend_usd=spend_check.spend_usd,
            )
            _append_decision(ledger, decision)
            return decision
        run_history = list(all_runs) if isinstance(all_runs, list) else []
        run_history.append({"job": job, "started_at": now})
        ledger["runs"] = run_history[-200:]
        ledger["active"] = {"job": job, "started_at": now}
        decision = AdmissionDecision(
            True,
            job,
            "admitted",
            "developer job admitted",
            spend_check.cap_usd,
            spend_check.spend_usd,
        )
        _append_decision(ledger, decision)

    return decision


def release(job: str) -> AdmissionDecision:
    """Release *job*'s lease; fail closed if another job owns it."""
    with locked_json_update(_ledger_path()) as ledger:
        active = ledger.get("active")
        if not isinstance(active, dict):
            decision = _denied(job, "lease-not-held", "no developer lease is active")
            _append_decision(ledger, decision)
            return decision
        if active.get("job") != job:
            decision = _denied(job, "lease-owned-by-other", f"developer lease is held by {active.get('job')!r}")
            _append_decision(ledger, decision)
            return decision
        ledger.pop("active", None)
        decision = AdmissionDecision(True, job, "released", "developer lease released")
        _append_decision(ledger, decision)
    return decision
