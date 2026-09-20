"""Durable, host-local retry reservations for driver issues."""

import fcntl
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fieldkit.config import get_fieldkit_data
from fieldkit.driver.github import MAX_ATTEMPTS
from fieldkit.util.atomic import locked_json_update

RetryPhase = Literal["running", "retryable", "succeeded", "exhausted"]
_PHASES: frozenset[str] = frozenset({"running", "retryable", "succeeded", "exhausted"})
_VERSION = 1


@dataclass(frozen=True)
class RetryDecision:
    """Local eligibility decision or durable reservation result."""

    allowed: bool
    issue_key: str
    phase: RetryPhase | None
    started_attempts: int
    attempt: int | None
    detail: str


@dataclass(frozen=True)
class RetryStatus:
    """Machine-readable view of one local retry entry."""

    issue_key: str
    phase: RetryPhase
    started_attempts: int
    initial_github_attempt: int
    outcome: str
    updated_at: str


@dataclass(frozen=True)
class ResetResult:
    """Result of an explicit local retry reset."""

    reset: bool
    issue_key: str
    detail: str


def _state_path(data_root: Path | None) -> Path:
    root = get_fieldkit_data() if data_root is None else data_root
    return root / "driver" / "retry-state.json"


def _driver_is_active(data_root: Path | None) -> bool:
    """Return whether another driver process currently holds its execution lock."""
    lock_path = _state_path(data_root).with_name("driver.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    return False


def _issue_key(repo: str, issue_number: int) -> str:
    return f"{repo}#{issue_number}"


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_attempt(labels: list[str]) -> int:
    attempts: list[int] = []
    for label in labels:
        if not label.startswith("attempt:"):
            continue
        try:
            attempt = int(label.split(":", 1)[1])
        except ValueError:
            continue
        if attempt >= 0:
            attempts.append(attempt)
    return max(attempts, default=0)


def _validate_entry(entry: object) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("retry entry must be an object")
    started = entry.get("started_attempts")
    initial = entry.get("initial_github_attempt")
    phase = entry.get("phase")
    if not isinstance(started, int) or started < 0:
        raise ValueError("retry entry has invalid started_attempts")
    if not isinstance(initial, int) or initial < 0:
        raise ValueError("retry entry has invalid initial_github_attempt")
    if phase not in _PHASES:
        raise ValueError("retry entry has invalid phase")
    for key in ("created_at", "updated_at", "outcome"):
        if not isinstance(entry.get(key), str):
            raise ValueError(f"retry entry has invalid {key}")
    if not isinstance(entry.get("resets"), list):
        raise ValueError("retry entry has invalid resets")
    return entry


def _validate_ledger(data: object) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("version") != _VERSION:
        raise ValueError("retry state has an unsupported schema")
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("retry state entries must be an object")
    for entry in entries.values():
        _validate_entry(entry)
    return entries


def _new_ledger(data: dict[str, Any]) -> dict[str, Any]:
    if not data:
        data.update({"version": _VERSION, "entries": {}})
    _validate_ledger(data)
    return data


def _new_entry(seed: int, now: str) -> dict[str, Any]:
    return {
        "started_attempts": seed,
        "initial_github_attempt": seed,
        "phase": "retryable",
        "created_at": now,
        "updated_at": now,
        "outcome": "",
        "resets": [],
    }


def _decision_from_entry(issue_key: str, entry: dict[str, Any]) -> RetryDecision:
    phase = entry["phase"]
    started = entry["started_attempts"]
    if phase in ("succeeded", "exhausted"):
        return RetryDecision(False, issue_key, phase, started, None, f"local state is {phase}")
    if started >= MAX_ATTEMPTS:
        return RetryDecision(False, issue_key, "exhausted", started, None, "local attempt cap reached")
    return RetryDecision(True, issue_key, phase, started, started + 1, "local retry budget available")


def check_eligibility(
    repo: str,
    issue_number: int,
    labels: list[str],
    *,
    data_root: Path | None = None,
) -> RetryDecision:
    """Return the local retry decision without mutating the ledger.

    A malformed or unreadable ledger denies execution.  The initial GitHub seed
    is only used while no entry exists; a later reservation persists it.
    """
    issue_key = _issue_key(repo, issue_number)
    try:
        path = _state_path(data_root)
        if not path.exists():
            seed = _seed_attempt(labels)
            if seed >= MAX_ATTEMPTS:
                return RetryDecision(False, issue_key, "exhausted", seed, None, "GitHub seed reached attempt cap")
            return RetryDecision(True, issue_key, None, seed, seed + 1, "no local reservation yet")
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = _validate_ledger(data)
        entry = entries.get(issue_key)
        if entry is None:
            seed = _seed_attempt(labels)
            if seed >= MAX_ATTEMPTS:
                return RetryDecision(False, issue_key, "exhausted", seed, None, "GitHub seed reached attempt cap")
            return RetryDecision(True, issue_key, None, seed, seed + 1, "no local reservation yet")
        return _decision_from_entry(issue_key, _validate_entry(entry))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return RetryDecision(False, issue_key, None, 0, None, f"retry state unavailable: {exc}")


def reserve_attempt(
    repo: str,
    issue_number: int,
    labels: list[str],
    *,
    data_root: Path | None = None,
) -> RetryDecision:
    """Atomically reserve the next local attempt before driver work starts."""
    issue_key = _issue_key(repo, issue_number)
    try:
        with locked_json_update(_state_path(data_root)) as data:
            ledger = _new_ledger(data)
            entries = ledger["entries"]
            entry = entries.get(issue_key)
            now = _timestamp()
            if entry is None:
                entry = _new_entry(_seed_attempt(labels), now)
                entries[issue_key] = entry
            entry = _validate_entry(entry)
            decision = _decision_from_entry(issue_key, entry)
            if not decision.allowed:
                if decision.phase == "exhausted" and entry["phase"] != "exhausted":
                    entry["phase"] = "exhausted"
                    entry["outcome"] = "attempt cap reached"
                    entry["updated_at"] = now
                return decision
            if decision.attempt is None:
                raise ValueError("retry decision missing attempt")
            entry["started_attempts"] = decision.attempt
            entry["phase"] = "running"
            entry["outcome"] = ""
            entry["updated_at"] = now
            return RetryDecision(True, issue_key, "running", decision.attempt, decision.attempt, "attempt reserved")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return RetryDecision(False, issue_key, None, 0, None, f"could not persist retry reservation: {exc}")


def complete_attempt(
    repo: str,
    issue_number: int,
    *,
    succeeded: bool,
    outcome: str,
    data_root: Path | None = None,
) -> RetryDecision:
    """Persist the local terminal or retryable result before GitHub projection."""
    issue_key = _issue_key(repo, issue_number)
    try:
        with locked_json_update(_state_path(data_root)) as data:
            entries = _validate_ledger(data)
            entry = entries.get(issue_key)
            if entry is None:
                raise ValueError("retry reservation is missing")
            entry = _validate_entry(entry)
            if entry["phase"] != "running":
                raise ValueError(f"retry reservation is not running ({entry['phase']})")
            phase: RetryPhase = "succeeded" if succeeded else "retryable"
            if not succeeded and entry["started_attempts"] >= MAX_ATTEMPTS:
                phase = "exhausted"
            entry["phase"] = phase
            entry["outcome"] = outcome
            entry["updated_at"] = _timestamp()
            return RetryDecision(
                True, issue_key, phase, entry["started_attempts"], entry["started_attempts"], "attempt finalized"
            )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return RetryDecision(False, issue_key, None, 0, None, f"could not finalize retry state: {exc}")


def retry_status(*, data_root: Path | None = None) -> tuple[RetryStatus, ...]:
    """Return all local retry entries without consulting GitHub."""
    try:
        path = _state_path(data_root)
        if not path.exists():
            return ()
        entries = _validate_ledger(json.loads(path.read_text(encoding="utf-8")))
        return tuple(
            RetryStatus(
                issue_key=key,
                phase=entry["phase"],
                started_attempts=entry["started_attempts"],
                initial_github_attempt=entry["initial_github_attempt"],
                outcome=entry["outcome"],
                updated_at=entry["updated_at"],
            )
            for key, raw_entry in sorted(entries.items())
            for entry in [_validate_entry(raw_entry)]
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"could not read retry state: {exc}") from exc


def reset_retry(
    repo: str,
    issue_number: int,
    reason: str,
    *,
    data_root: Path | None = None,
) -> ResetResult:
    """Explicitly reset a non-running local entry and preserve its audit trail."""
    issue_key = _issue_key(repo, issue_number)
    if not reason.strip():
        return ResetResult(False, issue_key, "reset reason must not be empty")
    try:
        with locked_json_update(_state_path(data_root)) as data:
            entries = _validate_ledger(data)
            entry = entries.get(issue_key)
            if entry is None:
                return ResetResult(False, issue_key, "no local retry entry")
            entry = _validate_entry(entry)
            if entry["phase"] == "running" and _driver_is_active(data_root):
                return ResetResult(False, issue_key, "cannot reset a reservation held by an active driver")
            now = _timestamp()
            resets = entry["resets"]
            resets.append({"at": now, "reason": reason.strip(), "previous_attempts": entry["started_attempts"]})
            entry["started_attempts"] = 0
            entry["phase"] = "retryable"
            entry["outcome"] = "reset"
            entry["updated_at"] = now
            return ResetResult(True, issue_key, "retry state reset")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return ResetResult(False, issue_key, f"could not reset retry state: {exc}")
