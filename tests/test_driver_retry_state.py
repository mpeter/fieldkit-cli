"""Tests for the durable local driver retry ledger."""

import fcntl
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_reserve_attempt_seeds_from_highest_github_label_and_exhausts(tmp_path: Path) -> None:
    from fieldkit.driver.retry_state import complete_attempt, reserve_attempt

    reservation = reserve_attempt("owner/repo", 42, ["attempt:1", "attempt:2", "attempt:bad"], data_root=tmp_path)

    assert reservation.allowed is True
    assert reservation.attempt == 3
    completion = complete_attempt("owner/repo", 42, succeeded=False, outcome="failed", data_root=tmp_path)
    assert completion.phase == "exhausted"
    denied = reserve_attempt("owner/repo", 42, [], data_root=tmp_path)
    assert denied.allowed is False
    assert denied.phase == "exhausted"


def test_reserve_attempt_uses_local_history_after_initial_seed(tmp_path: Path) -> None:
    from fieldkit.driver.retry_state import complete_attempt, reserve_attempt

    first = reserve_attempt("owner/repo", 42, ["attempt:1"], data_root=tmp_path)
    complete_attempt("owner/repo", 42, succeeded=False, outcome="failed", data_root=tmp_path)
    second = reserve_attempt("owner/repo", 42, ["attempt:0"], data_root=tmp_path)

    assert first.attempt == 2
    assert second.attempt == 3


def test_check_eligibility_denies_malformed_or_unwritable_state(tmp_path: Path) -> None:
    from fieldkit.driver.retry_state import check_eligibility, reserve_attempt

    state_dir = tmp_path / "driver"
    state_dir.mkdir()
    (state_dir / "retry-state.json").write_text("not-json", encoding="utf-8")

    malformed = check_eligibility("owner/repo", 42, [], data_root=tmp_path)
    assert malformed.allowed is False
    assert "unavailable" in malformed.detail

    blocked_root = tmp_path / "blocked"
    blocked_root.write_text("file", encoding="utf-8")
    unwritable = reserve_attempt("owner/repo", 43, [], data_root=blocked_root)
    assert unwritable.allowed is False
    assert "could not persist" in unwritable.detail


def test_reserve_attempt_counts_interrupted_running_reservation(tmp_path: Path) -> None:
    from fieldkit.driver.retry_state import reserve_attempt

    initial = reserve_attempt("owner/repo", 42, [], data_root=tmp_path)
    resumed = reserve_attempt("owner/repo", 42, [], data_root=tmp_path)

    assert initial.attempt == 1
    assert resumed.attempt == 2
    assert resumed.phase == "running"


def test_reset_retry_records_audit_and_refuses_active_running_entry(tmp_path: Path) -> None:
    from fieldkit.driver.retry_state import complete_attempt, reserve_attempt, reset_retry

    reserve_attempt("owner/repo", 42, [], data_root=tmp_path)
    lock_path = tmp_path / "driver" / "driver.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = reset_retry("owner/repo", 42, "operator review", data_root=tmp_path)
        fcntl.flock(lock_file, fcntl.LOCK_UN)

    assert refused.reset is False
    assert "active driver" in refused.detail

    complete_attempt("owner/repo", 42, succeeded=False, outcome="failed", data_root=tmp_path)
    reset = reset_retry("owner/repo", 42, "operator review", data_root=tmp_path)
    state = json.loads((tmp_path / "driver" / "retry-state.json").read_text(encoding="utf-8"))

    assert reset.reset is True
    assert state["entries"]["owner/repo#42"]["started_attempts"] == 0
    assert state["entries"]["owner/repo#42"]["resets"][0]["reason"] == "operator review"


def test_reserve_attempt_serializes_concurrent_updates(tmp_path: Path) -> None:
    from fieldkit.driver.retry_state import reserve_attempt

    with ThreadPoolExecutor(max_workers=4) as pool:
        decisions = list(pool.map(lambda _: reserve_attempt("owner/repo", 42, [], data_root=tmp_path), range(4)))

    attempts = sorted(decision.attempt for decision in decisions if decision.allowed and decision.attempt is not None)
    assert attempts == [1, 2, 3]
    assert sum(not decision.allowed for decision in decisions) == 1
