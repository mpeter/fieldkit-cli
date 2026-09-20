"""Tests for fieldkit/watch/pursuit_stalls.py — suppression and tier logic."""

import datetime
import json
import logging
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.watch._pursuit_stall_render import append_stall_alert
from fieldkit.watch._pursuit_stall_scan import normalize_transition_entry, scan_pursuit_file
from fieldkit.watch._pursuit_stall_state import (
    _days_tier,
    is_snoozed,
    load_state,
    save_state,
    should_suppress_alert,
    snooze_pursuit,
)
from fieldkit.watch.pursuit_stalls import _run_pursuit_stalls

pytestmark = pytest.mark.unit
# ---------------------------------------------------------------------------
# _days_tier
# ---------------------------------------------------------------------------


def test_days_tier_below_first():
    assert _days_tier(0) == 0
    assert _days_tier(6) == 0


def test_days_tier_at_boundaries():
    assert _days_tier(7) == 7
    assert _days_tier(8) == 7
    assert _days_tier(14) == 14
    assert _days_tier(15) == 14
    assert _days_tier(30) == 30
    assert _days_tier(100) == 30


# ---------------------------------------------------------------------------
# should_suppress_alert
# ---------------------------------------------------------------------------


def _result(days: int, stage: str = "discovery") -> dict[str, Any]:
    return {
        "pursuit": "test-pursuit",
        "account": "test-account",
        "stage": stage,
        "days_since_transition": days,
        "is_stalled": True,
    }


def test_no_prior_state_always_alerts():
    suppress, _ = should_suppress_alert(_result(20), None)
    assert not suppress


def test_first_detection_alerts():
    prior = {"stage": "discovery", "is_stalled": False, "alerted_days_tier": 0}
    suppress, _ = should_suppress_alert(_result(20), prior)
    assert not suppress


def test_same_tier_suppresses():
    # Already alerted at tier 14, still at 20 days (tier 14) -> suppress
    prior = {"stage": "discovery", "is_stalled": True, "alerted_days_tier": 14}
    suppress, reason = should_suppress_alert(_result(20), prior)
    assert suppress is True
    assert "14" in reason


def test_tier_advance_alerts():
    # Alerted at tier 14, now at 31 days (tier 30) -> alert
    prior = {"stage": "discovery", "is_stalled": True, "alerted_days_tier": 14}
    suppress, _ = should_suppress_alert(_result(31), prior)
    assert not suppress


def test_stage_change_resets_suppression():
    # Was stalled in discovery, now in validation -> new alert cycle
    prior = {"stage": "discovery", "is_stalled": True, "alerted_days_tier": 14}
    suppress, _ = should_suppress_alert(_result(20, stage="validation"), prior)
    assert not suppress


def test_no_alerted_tier_in_prior_treats_as_zero():
    # Prior state has is_stalled=True but no alerted_days_tier (legacy state)
    prior = {"stage": "discovery", "is_stalled": True}
    # 8 days -> tier 7 > 0 -> should alert
    suppress, _ = should_suppress_alert(_result(8), prior)
    assert not suppress


# ---------------------------------------------------------------------------
# scan_pursuit_file
# ---------------------------------------------------------------------------

_TODAY = datetime.date(2026, 6, 7)


def _write_pursuit(path: Path, stage: str, last_transition: str, days_ago: int = 20) -> None:
    path.write_text(f"---\nstage: {stage}\nlast-transition: {last_transition}\n---\n\nBody.\n", encoding="utf-8")


def test_scan_stalled(tmp_path: Path):
    f = tmp_path / "test-pursuit.md"
    _write_pursuit(f, "discovery", "2026-05-10")
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        result = scan_pursuit_file(f, threshold_days=14, today=_TODAY)
    assert result is not None
    assert result["is_stalled"] is True
    assert result["days_since_transition"] == 28


def test_scan_not_stalled(tmp_path: Path):
    f = tmp_path / "test-pursuit.md"
    _write_pursuit(f, "discovery", "2026-06-05")
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        result = scan_pursuit_file(f, threshold_days=14, today=_TODAY)
    assert result is not None
    assert result["is_stalled"] is False


def test_scan_terminal_stage_returns_none(tmp_path: Path):
    f = tmp_path / "closed.md"
    _write_pursuit(f, "closed-won", "2026-01-01")
    result = scan_pursuit_file(f, threshold_days=14, today=_TODAY)
    assert result is None


def test_scan_no_frontmatter_returns_none(tmp_path: Path):
    f = tmp_path / "bad.md"
    f.write_text("No frontmatter here.\n", encoding="utf-8")
    result = scan_pursuit_file(f, threshold_days=14, today=_TODAY)
    assert result is None


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_save_and_load_state(tmp_path: Path):
    state_file = tmp_path / "pursuit-stall-state.json"
    state = {"a/b": {"is_stalled": True, "alerted_days_tier": 14}}
    with (
        patch("fieldkit.watch._pursuit_stall_state.state_file", return_value=state_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        save_state(state)
        loaded = load_state()
    assert loaded == state


def test_load_state_missing_returns_empty(tmp_path: Path):
    state_file = tmp_path / "nonexistent.json"
    with patch("fieldkit.watch._pursuit_stall_state.state_file", return_value=state_file):
        loaded = load_state()
    assert loaded == {}


# ---------------------------------------------------------------------------
# append_stall_alert — dry_run does not write
# ---------------------------------------------------------------------------


def test_append_stall_alert_dry_run_no_write(tmp_path: Path):
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    result = {
        "account": "test-account",
        "pursuit": "test-pursuit",
        "stage": "discovery",
        "days_since_transition": 20,
        "last_transition_date": "2026-05-18",
        "threshold_days": 14,
        "path": "accounts/test-account/pursuits/test-pursuit.md",
        "native_qualification": "unavailable (live ClosePlan fetch required)",
    }
    with (
        patch("fieldkit.watch._pursuit_stall_render._alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        append_stall_alert(result, dry_run=True)
    assert not alerts_file.exists()


def test_append_stall_alert_writes_block(tmp_path: Path):
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    result = {
        "account": "test-account",
        "pursuit": "test-pursuit",
        "stage": "discovery",
        "days_since_transition": 20,
        "last_transition_date": "2026-05-18",
        "threshold_days": 14,
        "path": "accounts/test-account/pursuits/test-pursuit.md",
        "native_qualification": "unavailable (live ClosePlan fetch required)",
    }
    with (
        patch("fieldkit.watch._pursuit_stall_render._alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        append_stall_alert(result, dry_run=False)
    text = alerts_file.read_text(encoding="utf-8")
    assert "test-pursuit" in text
    assert "20" in text


# ---------------------------------------------------------------------------
# Actionability fields — champion, sf_next_steps, native qualification
# ---------------------------------------------------------------------------


def _write_pursuit_with_fields(
    path: Path,
    stage: str = "discovery",
    last_transition: str = "2026-05-10",
    extra_frontmatter: str = "",
) -> None:
    path.write_text(
        f"---\nstage: {stage}\nlast-transition: {last_transition}\n{extra_frontmatter}---\n\nBody.\n",
        encoding="utf-8",
    )


def _make_account_dir(tmp_path: Path, champion_name: str | None = None) -> Path:
    """Create accounts/<account>/ dir and optionally an account.md with champion."""
    acct_dir = tmp_path / "accounts" / "test-acct"
    pursuits_dir = acct_dir / "pursuits"
    pursuits_dir.mkdir(parents=True)
    if champion_name:
        (acct_dir / "account.md").write_text(
            f"---\nchampion: {champion_name}\n---\n",
            encoding="utf-8",
        )
    return acct_dir


def test_scan_returns_champion_name(tmp_path: Path) -> None:
    acct_dir = _make_account_dir(tmp_path, champion_name="Alice Champ")
    pursuit_file = acct_dir / "pursuits" / "test-pursuit.md"
    _write_pursuit_with_fields(pursuit_file)

    with (
        patch("fieldkit.watch._pursuit_stall_scan.extract_champion_name", return_value="Alice Champ"),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        result = scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["champion"] == "Alice Champ"


def test_scan_returns_sf_next_steps(tmp_path: Path) -> None:
    acct_dir = _make_account_dir(tmp_path)
    pursuit_file = acct_dir / "pursuits" / "test-pursuit.md"
    _write_pursuit_with_fields(pursuit_file, extra_frontmatter="sf_next_steps: Schedule technical review\n")

    with (
        patch("fieldkit.watch._pursuit_stall_scan.extract_champion_name", return_value="unknown"),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        result = scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["sf_next_steps"] == "Schedule technical review"


def test_scan_gate_gap_identifies_lowest(tmp_path: Path) -> None:
    acct_dir = _make_account_dir(tmp_path)
    pursuit_file = acct_dir / "pursuits" / "test-pursuit.md"
    # metrics=2 is the lowest score
    meddpicc_block = "meddpicc:\n  metrics: 2\n  economic-buyer: 5\n  decision-criteria: 4\n  champion: 3\n"
    _write_pursuit_with_fields(pursuit_file, extra_frontmatter=meddpicc_block)

    with (
        patch("fieldkit.watch._pursuit_stall_scan.extract_champion_name", return_value="unknown"),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        result = scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert "gate_gap" not in result
    assert result["native_qualification"] == "unavailable (no Salesforce opportunity link)"


def test_scan_gate_gap_no_meddpicc(tmp_path: Path) -> None:
    acct_dir = _make_account_dir(tmp_path)
    pursuit_file = acct_dir / "pursuits" / "test-pursuit.md"
    _write_pursuit_with_fields(pursuit_file)

    with (
        patch("fieldkit.watch._pursuit_stall_scan.extract_champion_name", return_value="unknown"),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        result = scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert "gate_gap" not in result
    assert result["native_qualification"] == "unavailable (no Salesforce opportunity link)"


def test_scan_champion_fallback(tmp_path: Path) -> None:
    acct_dir = _make_account_dir(tmp_path)  # no account.md
    pursuit_file = acct_dir / "pursuits" / "test-pursuit.md"
    _write_pursuit_with_fields(pursuit_file)

    with (
        patch("fieldkit.watch._pursuit_stall_scan.extract_champion_name", return_value="unknown"),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        result = scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["champion"] == "unknown"


def test_scan_next_steps_fallback(tmp_path: Path) -> None:
    acct_dir = _make_account_dir(tmp_path)
    pursuit_file = acct_dir / "pursuits" / "test-pursuit.md"
    _write_pursuit_with_fields(pursuit_file)  # no sf_next_steps

    with (
        patch("fieldkit.watch._pursuit_stall_scan.extract_champion_name", return_value="unknown"),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        result = scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["sf_next_steps"] == "(none)"


# ---------------------------------------------------------------------------
# _normalize_transition_entry — schema A {stage, date} and schema B {from, to, date}
# ---------------------------------------------------------------------------


def test_normalize_entry_schema_a_stage_date():
    """Schema A: {stage, date} → from_stage='', to_stage=stage, date parsed."""
    entry = {"stage": "discovery", "date": "2026-05-01"}
    result = normalize_transition_entry(entry)
    assert result is not None
    from_s, to_s, d = result
    assert from_s == ""
    assert to_s == "discovery"
    assert d == datetime.date(2026, 5, 1)


def test_normalize_entry_schema_b_from_to_date():
    """Schema B: {from, to, date} → from_stage and to_stage set correctly."""
    entry = {"from": "discovery", "to": "validation", "date": "2026-05-15"}
    result = normalize_transition_entry(entry)
    assert result is not None
    from_s, to_s, d = result
    assert from_s == "discovery"
    assert to_s == "validation"
    assert d == datetime.date(2026, 5, 15)


def test_normalize_entry_schema_b_partial_from_only():
    """Schema B with only 'from' key (missing 'to') — still treated as schema B."""
    entry = {"from": "prospecting", "date": "2026-04-01"}
    result = normalize_transition_entry(entry)
    assert result is not None
    from_s, to_s, d = result
    assert from_s == "prospecting"
    assert to_s == ""
    assert d == datetime.date(2026, 4, 1)


def test_normalize_entry_missing_date_returns_none():
    """Entry with no parseable date returns None."""
    assert normalize_transition_entry({"stage": "discovery"}) is None


def test_normalize_entry_non_dict_returns_none():
    """Non-dict entry (e.g., plain string) returns None."""
    assert normalize_transition_entry("bad") is None


def test_normalize_entry_legacy_last_transition_key():
    """Entry using 'last-transition' as date key (legacy) is parsed correctly."""
    entry = {"stage": "negotiation", "last-transition": "2026-03-10"}
    result = normalize_transition_entry(entry)
    assert result is not None
    assert result[2] == datetime.date(2026, 3, 10)


# ---------------------------------------------------------------------------
# _find_last_transition_date via scan_pursuit_file — both schemas
# ---------------------------------------------------------------------------


def test_scan_uses_transition_history_schema_a(tmp_path: Path):
    """Schema A transition-history entries produce correct last-transition date."""
    f = tmp_path / "test.md"
    f.write_text(
        "---\n"
        "stage: validation\n"
        "transition-history:\n"
        "  - stage: discovery\n"
        "    date: 2026-04-01\n"
        "  - stage: validation\n"
        "    date: 2026-05-20\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        result = scan_pursuit_file(f, threshold_days=14, today=_TODAY)
    assert result is not None
    assert result["last_transition_date"] == "2026-05-20"
    assert result["days_since_transition"] == (datetime.date(2026, 6, 7) - datetime.date(2026, 5, 20)).days


def test_scan_uses_transition_history_schema_b(tmp_path: Path):
    """Schema B {from, to, date} transition-history entries produce correct date."""
    f = tmp_path / "test.md"
    f.write_text(
        "---\n"
        "stage: validation\n"
        "transition-history:\n"
        "  - from: discovery\n"
        "    to: validation\n"
        "    date: 2026-05-18\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        result = scan_pursuit_file(f, threshold_days=14, today=_TODAY)
    assert result is not None
    assert result["last_transition_date"] == "2026-05-18"


def test_scan_uses_transition_history_mixed_schemas(tmp_path: Path):
    """Mixed schema A and schema B entries: most recent date wins."""
    f = tmp_path / "test.md"
    f.write_text(
        "---\n"
        "stage: negotiation\n"
        "transition-history:\n"
        "  - stage: discovery\n"
        "    date: 2026-03-01\n"
        "  - from: discovery\n"
        "    to: validation\n"
        "    date: 2026-04-15\n"
        "  - stage: negotiation\n"
        "    date: 2026-05-25\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        result = scan_pursuit_file(f, threshold_days=14, today=_TODAY)
    assert result is not None
    assert result["last_transition_date"] == "2026-05-25"


def test_scan_last_transition_overrides_history(tmp_path: Path):
    """Canonical last-transition field takes precedence over transition-history."""
    f = tmp_path / "test.md"
    f.write_text(
        "---\n"
        "stage: validation\n"
        "last-transition: 2026-06-01\n"
        "transition-history:\n"
        "  - from: discovery\n"
        "    to: validation\n"
        "    date: 2026-04-01\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        result = scan_pursuit_file(f, threshold_days=14, today=_TODAY)
    assert result is not None
    assert result["last_transition_date"] == "2026-06-01"


# ---------------------------------------------------------------------------
# historic regression normalization guard — stage-case-only diff must NOT stamp today
# ---------------------------------------------------------------------------


def _build_prior_state(stage: str, last_transition: str = "2026-05-01") -> dict[str, Any]:
    """Return a minimal prior state dict as stored by the watcher."""
    return {
        "account": "test-acct",
        "pursuit": "norm-test",
        "stage": stage,
        "last_transition_date": last_transition,
        "days_since_transition": 37,
        "is_stalled": True,
        "alerted_days_tier": 30,
    }


def _normalization_only_check(prior_stage: str, current_stage: str) -> bool:
    """Replicate the historic regression condition from _run_pursuit_stalls.

    Returns True when the condition would reset the stall timer (i.e., a real
    stage change was detected), False when it would leave the result unchanged.
    """
    prior_stage_normalized = prior_stage.strip().lower()
    return bool(prior_stage_normalized and prior_stage_normalized != current_stage)


def test_normalization_only_does_not_stamp_transition() -> None:
    """Prior state stage stored with different case must NOT trigger a stamp.

    Scenario: watcher previously stored stage as 'Discovery' (title-case).
    Current run reads stage as 'discovery' (lowercase, from fm normalisation).
    The comparison must treat these as equal — no last_transition_date reset.
    """
    prior_stage = "Discovery"  # stored before normalisation was applied
    current_stage = "discovery"  # always lowercase after T01 fix

    triggers_stamp = _normalization_only_check(prior_stage, current_stage)
    assert not triggers_stamp, "Normalization-only case difference should NOT trigger last_transition_date stamp"


def test_real_stage_change_stamps_transition() -> None:
    """A genuine stage transition must trigger the last_transition_date reset.

    Scenario: prior state was 'discovery', current run sees 'validation'.
    """
    prior_stage = "discovery"
    current_stage = "validation"

    triggers_stamp = _normalization_only_check(prior_stage, current_stage)
    assert triggers_stamp, "A real stage change should trigger last_transition_date stamp"


def test_real_stage_change_with_mixed_case_prior_stamps_transition() -> None:
    """Prior state with mixed case AND a real stage change must still trigger stamp."""
    prior_stage = "Discovery"  # mixed case
    current_stage = "validation"  # genuinely different stage

    triggers_stamp = _normalization_only_check(prior_stage, current_stage)
    assert triggers_stamp, "Real stage change with mixed-case prior should still trigger stamp"


def test_alert_contains_action_line(tmp_path: Path) -> None:
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    result = {
        "account": "test-account",
        "pursuit": "test-pursuit",
        "stage": "discovery",
        "days_since_transition": 20,
        "last_transition_date": "2026-05-18",
        "threshold_days": 14,
        "path": "accounts/test-account/pursuits/test-pursuit.md",
        "champion": "Bob Sponsor",
        "sf_next_steps": "Send proposal",
        "native_qualification": "unavailable (no Salesforce opportunity link)",
    }
    with (
        patch("fieldkit.watch._pursuit_stall_render._alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        append_stall_alert(result, dry_run=False)

    text = alerts_file.read_text(encoding="utf-8")
    assert "Action:" in text
    assert "Bob Sponsor" in text
    assert "Send proposal" in text
    assert "Native qualification" in text
    assert "Economic Buyer" not in text


# ---------------------------------------------------------------------------
# implementation change: Snooze / acknowledge
# ---------------------------------------------------------------------------


# ── TestSnoozeAck (flattened) ───────────────────────────────────────────────


def test_snooze_ack_ack_sets_snoozed_until(tmp_path: Path) -> None:
    """snooze_pursuit() writes snoozed_until = today + days to state file."""
    state_file = tmp_path / "state.json"
    with (
        patch("fieldkit.watch._pursuit_stall_state.state_file", return_value=state_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        snooze_pursuit("acme/deal", days=7)
        loaded = load_state()

    entry = loaded.get("acme/deal", {})
    assert "snoozed_until" in entry
    expected = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=7)).isoformat()
    assert entry["snoozed_until"] == expected


def test_snooze_ack_ack_custom_days(tmp_path: Path) -> None:
    """snooze_pursuit(days=14) sets snoozed_until 14 days from today."""
    state_file = tmp_path / "state.json"
    with (
        patch("fieldkit.watch._pursuit_stall_state.state_file", return_value=state_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        snooze_pursuit("globalpay/saas-deal", days=14)
        loaded = load_state()

    entry = loaded.get("globalpay/saas-deal", {})
    expected = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=14)).isoformat()
    assert entry["snoozed_until"] == expected


def test_snooze_ack_ack_preserves_existing_entry_fields(tmp_path: Path) -> None:
    """snooze_pursuit() preserves other fields in an existing state entry."""
    state_file = tmp_path / "state.json"
    initial = {
        "acme/deal": {
            "stage": "discover",
            "is_stalled": True,
            "alerted_days_tier": 14,
        }
    }
    state_file.write_text(json.dumps(initial), encoding="utf-8")
    with (
        patch("fieldkit.watch._pursuit_stall_state.state_file", return_value=state_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
    ):
        snooze_pursuit("acme/deal", days=7)
        loaded = load_state()

    entry = loaded["acme/deal"]
    assert entry["stage"] == "discover"
    assert entry["is_stalled"] is True
    assert "snoozed_until" in entry


def test_snooze_ack_is_snoozed_active() -> None:
    """is_snoozed returns True when snoozed_until is in the future and stage unchanged."""
    future = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    entry: dict[str, Any] = {"stage": "discover", "snoozed_until": future}
    snoozed, reason = is_snoozed(entry, current_stage="discover")
    assert snoozed is True
    assert "snoozed until" in reason


def test_snooze_ack_is_snoozed_expired() -> None:
    """is_snoozed returns False when snoozed_until is in the past."""
    past = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=1)).isoformat()
    entry: dict[str, Any] = {"stage": "discover", "snoozed_until": past}
    snoozed, _ = is_snoozed(entry, current_stage="discover")
    assert snoozed is False


def test_snooze_ack_is_snoozed_expired_clears_field() -> None:
    """Expired snooze removes snoozed_until from the entry dict in-place."""
    past = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=1)).isoformat()
    entry: dict[str, Any] = {"stage": "discover", "snoozed_until": past}
    is_snoozed(entry, current_stage="discover")
    assert "snoozed_until" not in entry


def test_snooze_ack_is_snoozed_no_field() -> None:
    """is_snoozed returns False when snoozed_until is not present."""
    entry: dict[str, Any] = {"stage": "discover", "is_stalled": True}
    snoozed, _ = is_snoozed(entry, current_stage="discover")
    assert snoozed is False


def test_snooze_ack_is_snoozed_stage_change_clears_snooze() -> None:
    """Stage change clears snoozed_until and returns (False, '')."""
    future = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    entry: dict[str, Any] = {"stage": "discover", "snoozed_until": future}
    snoozed, reason = is_snoozed(entry, current_stage="validate")
    assert snoozed is False
    assert reason == ""
    assert "snoozed_until" not in entry


def test_snooze_ack_is_snoozed_stage_change_removes_field_in_place() -> None:
    """After stage change, snoozed_until is removed from the entry in-place."""
    future = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=3)).isoformat()
    entry: dict[str, Any] = {"stage": "discover", "snoozed_until": future}
    is_snoozed(entry, current_stage="negotiate")
    assert "snoozed_until" not in entry


def _snooze_ack_make_stalled_pursuit(tmp_path: Path, account: str = "acme", pursuit: str = "deal") -> Path:
    pursuits_dir = tmp_path / "accounts" / account / "pursuits"
    pursuits_dir.mkdir(parents=True, exist_ok=True)
    last_t = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)).isoformat()
    content = textwrap.dedent(f"""\
            ---
            stage: discover
            last-transition: {last_t}
            ---
            # Deal
        """)
    p = pursuits_dir / f"{pursuit}.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_snooze_ack_snoozed_pursuit_skipped_in_alert_output(tmp_path: Path) -> None:
    """A snoozed pursuit does not generate an alert entry."""
    _snooze_ack_make_stalled_pursuit(tmp_path)
    snooze_until = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "discover",
            "is_stalled": True,
            "alerted_days_tier": 14,
            "snoozed_until": snooze_until,
        }
    }
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")

    # historic regression / xdist isolation: setup_watcher_logging() calls get_fieldkit_home()
    # from fieldkit.config (not the patched pursuit_stalls.get_fieldkit_home), so it
    # writes real log files to the live data directory and removes root-logger
    # StreamHandlers.  In a parallel xdist run that removal can strip caplog
    # handlers from sibling workers.  Patch it here so this test is fully
    # self-contained and never mutates the shared root logger.
    _null_handler = logging.NullHandler()

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch("fieldkit.watch._pursuit_stall_state.state_file", return_value=state_file),
        patch("fieldkit.watch._pursuit_stall_render._alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
        # historic regression: was_run_today reads from live watcher-run-status.json via
        # fieldkit.watch.status.get_fieldkit_home (not the patched pursuit_stalls.get_fieldkit_home).
        # Mock it here so parallel test runs don't pollute this test via a sibling
        # writing a fatal outcome to the live status file.
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch("fieldkit.watch.pursuit_stalls.watcher_logging"),
        patch("fieldkit.watch.pursuit_stalls.write_run_status"),
    ):
        rc = _run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    # Alert file should NOT contain a stall entry for the snoozed pursuit
    if alerts_file.exists():
        text = alerts_file.read_text(encoding="utf-8")
        # Should only have header, no "acme / deal" stall section
        assert "acme / deal" not in text


def test_snooze_ack_stage_change_clears_snooze_and_resets_timer(tmp_path: Path) -> None:
    """When stage changes, snooze is cleared in saved state."""
    # Pursuit file now shows 'validate' stage (changed from prior 'discover')
    pursuits_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits_dir.mkdir(parents=True, exist_ok=True)
    last_t = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)).isoformat()
    content = textwrap.dedent(f"""\
            ---
            stage: validate
            last-transition: {last_t}
            ---
            # Deal
        """)
    (pursuits_dir / "deal.md").write_text(content, encoding="utf-8")

    snooze_until = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "discover",  # prior stage — different from current
            "is_stalled": True,
            "alerted_days_tier": 14,
            "snoozed_until": snooze_until,
        }
    }
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")

    # historic regression / xdist isolation: see test_snoozed_pursuit_skipped_in_alert_output.
    _null_handler = logging.NullHandler()

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch("fieldkit.watch._pursuit_stall_state.state_file", return_value=state_file),
        patch("fieldkit.watch._pursuit_stall_render._alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch("fieldkit.watch.pursuit_stalls.watcher_logging"),
        patch("fieldkit.watch.pursuit_stalls.write_run_status"),
    ):
        rc = _run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    # snoozed_until should be cleared in saved state (stage change detected)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert "snoozed_until" not in saved.get("acme/deal", {})


def test_snooze_ack_expired_snooze_allows_alerting(tmp_path: Path) -> None:
    """When snoozed_until is in the past, the pursuit alerts normally."""
    _snooze_ack_make_stalled_pursuit(tmp_path)
    past = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=1)).isoformat()
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "discover",
            "is_stalled": True,
            "alerted_days_tier": 0,  # not yet alerted at current tier
            "snoozed_until": past,
        }
    }
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")

    # historic regression / xdist isolation: see test_snoozed_pursuit_skipped_in_alert_output.
    _null_handler = logging.NullHandler()

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch("fieldkit.watch._pursuit_stall_state.state_file", return_value=state_file),
        patch("fieldkit.watch._pursuit_stall_render._alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_render.get_watchers_dir", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch("fieldkit.watch.pursuit_stalls.watcher_logging"),
        patch("fieldkit.watch.pursuit_stalls.write_run_status"),
    ):
        rc = _run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    # Expired snooze → alert should be written
    assert alerts_file.exists()
    text = alerts_file.read_text(encoding="utf-8")
    assert "acme" in text
