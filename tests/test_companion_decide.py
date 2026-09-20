"""Tests for fieldkit.companion.decide — deterministic item → ProposedAction."""

import pytest

from fieldkit.companion.decide import ProposedAction, propose_for
from fieldkit.companion.feed import AttentionItem
from fieldkit.companion.gate import is_allowed
from fieldkit.companion.mapping import ALERT_SKILL_MAP, WATCHER_SEVERITY_MAP

pytestmark = pytest.mark.unit


def _item(**overrides: object) -> AttentionItem:
    base: dict[str, object] = {
        "item_id": "abc123",
        "source": "alert/pursuit-stall",
        "account": "acme",
        "severity": "warning",
        "summary": "stalled in validate",
        "evidence_path": "/x/pursuit-stall-alerts.md",
        "suggested_skill": "grill",
        "observed_at": "2026-07-01",
    }
    base.update(overrides)
    return AttentionItem(**base)  # type: ignore[arg-type]


def test_propose_for_account_alert_reads_pursuit_health() -> None:
    action = propose_for(_item())
    assert isinstance(action, ProposedAction)  # return-bound var asserted first (gaze CR-014)
    assert action.command_argv == ("pursuit", "health", "--account", "acme", "--json")
    assert action.enrichment_argv == action.command_argv
    assert action.skill == "grill"
    assert "acme" in action.rationale


def test_propose_for_run_status_points_at_watch_logs() -> None:
    action = propose_for(_item(source="run-status/pursuit-stalls", account=None, suggested_skill=None))
    assert action.command_argv == ("watch", "logs", "pursuit-stalls")
    assert action.skill is None
    assert action.enrichment_argv is None


@pytest.mark.parametrize(
    "watcher",
    ["unknown-watcher", "", "--tail", "*.log"],
    ids=["unknown", "empty", "option-like", "glob"],
)
def test_propose_for_untrusted_run_status_omits_watch_logs_command(watcher: str) -> None:
    action = propose_for(_item(source=f"run-status/{watcher}", account=None, suggested_skill=None))
    assert action.command_argv is None


def test_propose_for_item_without_account_has_no_read() -> None:
    action = propose_for(_item(source="tasks/waiting-on", account=None, suggested_skill="task-management"))
    assert action.command_argv is None
    assert action.enrichment_argv is None
    assert action.skill == "task-management"


def test_deterministic_command_is_always_read_only() -> None:
    """Safety invariant: the emitted command passes the gate at the lowest tier."""
    account_action = propose_for(_item())
    watcher_action = propose_for(_item(source="run-status/account-health", account=None, suggested_skill=None))
    assert is_allowed(list(account_action.command_argv or []), "read", []) is True
    assert is_allowed(list(watcher_action.command_argv or []), "read", []) is True


@pytest.mark.parametrize("skill", sorted(set(ALERT_SKILL_MAP.values())))
def test_every_mapped_alert_skill_yields_read_only_account_pull(skill: str) -> None:
    action = propose_for(_item(suggested_skill=skill))
    assert action.command_argv == ("pursuit", "health", "--account", "acme", "--json")
    assert action.skill == skill


@pytest.mark.parametrize("watcher", sorted(WATCHER_SEVERITY_MAP))
def test_every_watcher_run_status_maps_to_watch_logs(watcher: str) -> None:
    action = propose_for(_item(source=f"run-status/{watcher}", account=None, suggested_skill=None))
    assert action.command_argv == ("watch", "logs", watcher)
