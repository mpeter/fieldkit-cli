"""Tests for fieldkit/watch/contract_expiry.py.

Covers:
- _classify_tier() buckets: >60 skip, 31-60 yellow, 15-30 orange, 1-14 red,
  <=0 expired
- should_suppress(): same tier suppressed, tier escalation fires new alert
- Date parsing: ISO string and datetime.date both work
- dry-run: no file writes
- Missing sf_contract_end: skipped silently
- Completed/Closed sf_stage: skipped silently
- Account filter: only matching account is scanned
- Monkeypatched data_root and file reads; no real filesystem access
"""

import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.watch.contract_expiry as ce
from fieldkit.commands.watch.contract_expiry import cli  # noqa: F401

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# _classify_tier
# ---------------------------------------------------------------------------


# ── TestClassifyTier (flattened) ─────────────────────────────────────────────


def test_classify_tier_skip_far_future() -> None:
    assert ce._classify_tier(61) is None


def test_classify_tier_skip_exactly_61() -> None:
    assert ce._classify_tier(61) is None


def test_classify_tier_yellow_boundary_60() -> None:
    assert ce._classify_tier(60) == "yellow"


def test_classify_tier_yellow_boundary_31() -> None:
    assert ce._classify_tier(31) == "yellow"


def test_classify_tier_orange_boundary_30() -> None:
    assert ce._classify_tier(30) == "orange"


def test_classify_tier_orange_boundary_15() -> None:
    assert ce._classify_tier(15) == "orange"


def test_classify_tier_red_boundary_14() -> None:
    assert ce._classify_tier(14) == "red"


def test_classify_tier_red_boundary_1() -> None:
    assert ce._classify_tier(1) == "red"


def test_classify_tier_expired_zero_is_red() -> None:
    # day 0 = contract end today; code uses < 0 for expired, so 0 → red
    assert ce._classify_tier(0) == "red"


def test_classify_tier_expired_negative() -> None:
    assert ce._classify_tier(-5) == "expired"


# ---------------------------------------------------------------------------
# should_suppress
# ---------------------------------------------------------------------------


# ── TestShouldSuppress (flattened) ─────────────────────────────────────────────


def test_should_suppress_first_run_not_suppressed() -> None:
    assert ce.should_suppress("acme/proj/red", "red", {}) is False


def test_should_suppress_same_tier_suppressed() -> None:
    state = {"acme/proj/red": {"alerted_tier": "red"}}
    assert ce.should_suppress("acme/proj/red", "red", state) is True


def test_should_suppress_escalation_not_suppressed() -> None:
    # prior state had orange; now key is red (more urgent) → should NOT suppress
    assert ce.should_suppress("acme/proj/red", "red", {"acme/proj/red": {"alerted_tier": "orange"}}) is False


def test_should_suppress_different_key_not_suppressed() -> None:
    state = {"acme/other/red": {"alerted_tier": "red"}}
    assert ce.should_suppress("acme/proj/red", "red", state) is False


def test_should_suppress_lower_urgency_suppressed() -> None:
    # expired was alerted, now orange (less urgent) → suppress
    state = {"acme/proj/orange": {"alerted_tier": "expired"}}
    assert ce.should_suppress("acme/proj/orange", "orange", state) is True


# ---------------------------------------------------------------------------
# append_alert — dry-run prevents writes
# ---------------------------------------------------------------------------


# ── TestAppendAlert (flattened) ─────────────────────────────────────────────


def _result_append_alert(tier: str = "red", days: int = 5) -> dict[str, Any]:
    return {
        "account": "acme",
        "pursuit": "hcs-drawdown",
        "tier": tier,
        "days_until_end": days,
        "close_date": "2026-06-11",
        "sf_opportunity": "OPP-001",
    }


def test_append_alert_dry_run_no_writes(tmp_path: Path) -> None:
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(ce, "_alerts_file", return_value=alerts_file),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path),
    ):
        ce.append_alert(_result_append_alert(), dry_run=True)
    assert not alerts_file.exists()


def test_append_alert_wet_run_writes_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(ce, "_alerts_file", return_value=alerts_file),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path),
    ):
        ce.append_alert(_result_append_alert(), dry_run=False)
    assert alerts_file.exists()
    content = alerts_file.read_text()
    assert "acme" in content
    assert "hcs-drawdown" in content
    assert "🔴" in content


def test_append_alert_expired_label(tmp_path: Path) -> None:
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(ce, "_alerts_file", return_value=alerts_file),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path),
    ):
        ce.append_alert(_result_append_alert(tier="expired", days=-10), dry_run=False)
    content = alerts_file.read_text()
    assert "EXPIRED" in content
    assert "💀" in content
    assert "0-30 days overdue" in content


def test_append_alert_expired_90plus_escalation(tmp_path: Path) -> None:
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(ce, "_alerts_file", return_value=alerts_file),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path),
    ):
        ce.append_alert(_result_append_alert(tier="expired", days=-91), dry_run=False)
    content = alerts_file.read_text()
    assert "90+ days overdue" in content


def test_append_alert_yellow_label(tmp_path: Path) -> None:
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(ce, "_alerts_file", return_value=alerts_file),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path),
    ):
        ce.append_alert(_result_append_alert(tier="yellow", days=45), dry_run=False)
    content = alerts_file.read_text()
    assert "🟡" in content


def test_append_alert_dedup_same_day_skips_second_write(tmp_path: Path) -> None:
    """historic regression/121: calling append_alert twice on the same day writes only one heading."""
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(ce, "_alerts_file", return_value=alerts_file),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path),
    ):
        ce.append_alert(_result_append_alert(), dry_run=False)
        ce.append_alert(_result_append_alert(), dry_run=False)
    content = alerts_file.read_text()
    today_str = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    # Only one ## heading for this date/account/project combination
    assert content.count(f"## {today_str} — [pursuit-close-date] acme/hcs-drawdown") == 1


def test_append_alert_does_not_treat_legacy_project_heading_as_a_pursuit_alert(tmp_path: Path) -> None:
    """A project-era heading cannot suppress the versioned pursuit close-date alert."""
    alerts_file = tmp_path / "alerts.md"
    today_str = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    alerts_file.write_text(f"## {today_str} — acme/hcs-drawdown — expires in 5 day(s) 🔴\n", encoding="utf-8")

    with (
        patch.object(ce, "_alerts_file", return_value=alerts_file),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path),
    ):
        wrote = ce.append_alert(_result_append_alert(), dry_run=False)

    assert wrote is True
    assert "## " + today_str + " — [pursuit-close-date] acme/hcs-drawdown" in alerts_file.read_text()


def test_append_alert_dedup_different_projects_both_written(tmp_path: Path) -> None:
    """Two different projects on the same day → both alerts written."""
    alerts_file = tmp_path / "alerts.md"
    result_a = {**_result_append_alert(), "pursuit": "proj-alpha"}
    result_b = {**_result_append_alert(), "pursuit": "proj-beta"}
    with (
        patch.object(ce, "_alerts_file", return_value=alerts_file),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path),
    ):
        ce.append_alert(result_a, dry_run=False)
        ce.append_alert(result_b, dry_run=False)
    content = alerts_file.read_text()
    assert "proj-alpha" in content
    assert "proj-beta" in content


# ---------------------------------------------------------------------------
# _run_contract_expiry — integration over mocked project files
# ---------------------------------------------------------------------------

_FM_RED = {
    "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=7)).isoformat(),
    "sf_stage": "active",
    "sf_opportunity": "OPP-001",
}

_FM_YELLOW = {
    "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=45)).isoformat(),
    "sf_stage": "active",
    "sf_opportunity": "OPP-002",
}

_FM_SKIP_FAR = {
    "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=90)).isoformat(),
    "sf_stage": "active",
}

_FM_EXPIRED = {
    "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=5)).isoformat(),
    "sf_stage": "active",
}

_FM_COMPLETED = {
    "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=5)).isoformat(),
    "sf_stage": "Completed",
}

_FM_NO_DATE: dict[str, Any] = {"sf_stage": "active"}


def _make_project_file(tmp_path: Path, account: str, project: str, fm: dict[str, Any]) -> Path:
    """Create a fake pursuit .md file with canonical Salesforce frontmatter."""
    stage = str(fm.get("stage") or "qualify")
    if str(fm.get("sf_stage") or "").lower() in {"completed", "closed"}:
        stage = "closed-lost"
    pursuit_fm = {
        "stage": stage,
        "sf_close_date": fm.get("sf_close_date") or fm.get("sf_contract_end"),
        "sf_opportunity_id": fm.get("sf_opportunity_id") or fm.get("sf_opportunity"),
    }
    lines = ["---"]
    for k, v in pursuit_fm.items():
        if v is None:
            continue
        lines.append(f"{k}: {v}")
    lines += ["---", "", "Pursuit body."]
    p = tmp_path / "accounts" / account / "pursuits" / f"{project}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _run(
    tmp_path: Path,
    *,
    account_filter: str | None = None,
    dry_run: bool = False,
    state: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run _run_contract_expiry with data_root=tmp_path; return (exit_code, saved_state)."""
    saved: dict[str, Any] = {}

    def _fake_save_state(s: dict[str, Any], **_: Any) -> None:
        saved.update(s)

    with (
        patch("fieldkit.watch.contract_expiry.get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "_load_state", return_value=state or {}),
        patch.object(ce, "_save_state", side_effect=_fake_save_state),
        patch.object(ce, "write_run_status", return_value=None),
    ):
        # Clear functools.cache so _data_root monkeypatching works transitively
        rc = ce._run_contract_expiry(account_filter=account_filter, dry_run=dry_run)
    return rc, saved


# ── TestRunContractExpiry (flattened) ─────────────────────────────────────────────


def test_run_contract_expiry_red_tier_fires_alert(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)
    with patch.object(ce, "append_alert") as mock_alert:
        rc, _ = _run(tmp_path)
    assert rc == 0
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["tier"] == "red"
    assert result["account"] == "acme"
    assert result["pursuit"] == "hcs-drawdown"


def test_run_contract_expiry_yellow_tier_fires_alert(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_YELLOW)
    with patch.object(ce, "append_alert") as mock_alert:
        rc, _ = _run(tmp_path)
    assert rc == 0
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0]["tier"] == "yellow"


def test_run_contract_expiry_skip_far_future(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_SKIP_FAR)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_not_called()


def test_run_contract_expiry_expired_fires_alert(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_EXPIRED)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0]["tier"] == "expired"


def test_run_contract_expiry_completed_stage_skipped(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "done-proj", _FM_COMPLETED)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_not_called()


def test_run_contract_expiry_missing_sf_contract_end_skipped(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "no-date-proj", _FM_NO_DATE)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_not_called()


def test_run_contract_expiry_date_as_string_parses(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "string-date", _FM_RED)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_called_once()


def test_run_contract_expiry_date_as_date_object(tmp_path: Path) -> None:
    """A date scalar in pursuit frontmatter is normalized and alerts correctly."""
    fm = {"sf_close_date": datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=7)}
    _make_project_file(tmp_path, "acme", "date-obj", fm)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0]["tier"] == "red"


def test_run_contract_expiry_suppression_same_tier(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)
    # implementation note: state key now includes threshold suffix (default 14-30-60)
    state = {"pursuit-sf-close-date/v1/acme/hcs-drawdown/red/14-30-60": {"alerted_tier": "red"}}
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, state=state)
    mock_alert.assert_not_called()


def test_run_contract_expiry_legacy_project_state_does_not_suppress_pursuit(tmp_path: Path) -> None:
    """Legacy project state must not suppress the versioned pursuit close-date signal."""
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)
    legacy_state = {"acme/hcs-drawdown/red/14-30-60": {"alerted_tier": "red"}}

    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, state=legacy_state)

    mock_alert.assert_called_once()


def test_run_contract_expiry_suppression_tier_escalation_fires(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)
    # implementation note: prior state had yellow tier alerted; urgency increase (yellow→red) re-fires
    # Key includes threshold suffix (default 14-30-60)
    state = {"pursuit-sf-close-date/v1/acme/hcs-drawdown/yellow/14-30-60": {"alerted_tier": "yellow"}}
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, state=state)
    # red key not in state AND urgency increased → fires
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0]["tier"] == "red"


def test_run_contract_expiry_dry_run_passed_to_append_alert(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, dry_run=True)
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][1] is True  # dry_run positional arg


def test_run_contract_expiry_dry_run_no_state_save(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)
    save_mock = MagicMock()
    with (
        patch("fieldkit.watch.contract_expiry.get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state", side_effect=save_mock),
        patch.object(ce, "write_run_status", return_value=None),
        patch.object(ce, "append_alert"),
    ):
        ce._run_contract_expiry(account_filter=None, dry_run=True)
    save_mock.assert_not_called()


def test_run_contract_expiry_account_filter_skips_others(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)
    _make_project_file(tmp_path, "globalpay", "another-proj", _FM_RED)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, account_filter="acme")
    assert mock_alert.call_count == 1
    assert mock_alert.call_args[0][0]["account"] == "acme"


@pytest.mark.characterization
def test_run_contract_expiry_account_filter_is_exact(tmp_path: Path) -> None:
    """The account filter must not include similarly prefixed account directories."""
    _make_project_file(tmp_path, "acme", "renewal", _FM_RED)
    _make_project_file(tmp_path, "acme-west", "renewal", _FM_RED)

    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, account_filter="acme")

    mock_alert.assert_called_once()
    assert mock_alert.call_args.args[0]["account"] == "acme"


@pytest.mark.characterization
def test_run_contract_expiry_excludes_template_and_support_pursuits(tmp_path: Path) -> None:
    """The pursuit iterator must ignore template and gmail-intel support files."""
    _make_project_file(tmp_path, "acme", "renewal", _FM_RED)
    _make_project_file(tmp_path, "acme", "template", _FM_RED)
    _make_project_file(tmp_path, "acme", "gmail-intel", _FM_RED)

    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)

    mock_alert.assert_called_once()
    assert mock_alert.call_args.args[0]["pursuit"] == "renewal"


def test_run_contract_expiry_no_projects_dir_returns_error(tmp_path: Path) -> None:
    # No accounts dir at all → should log and return 1
    rc, _ = _run(tmp_path)
    assert rc == 1


def test_run_contract_expiry_two_projects_two_alerts(tmp_path: Path) -> None:
    _make_project_file(tmp_path, "acme", "proj-a", _FM_RED)
    _make_project_file(tmp_path, "acme", "proj-b", _FM_YELLOW)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    assert mock_alert.call_count == 2


# ---------------------------------------------------------------------------
# _classify_escalation_tier
# ---------------------------------------------------------------------------


# ── TestClassifyEscalationTier (flattened) ─────────────────────────────────────────────


def test_classify_escalation_tier_1_day_overdue() -> None:
    assert ce._classify_escalation_tier(-1) == "0-30 days overdue"


def test_classify_escalation_tier_30_days_overdue() -> None:
    assert ce._classify_escalation_tier(-30) == "0-30 days overdue"


def test_classify_escalation_tier_31_days_overdue() -> None:
    assert ce._classify_escalation_tier(-31) == "30-90 days overdue"


def test_classify_escalation_tier_90_days_overdue() -> None:
    assert ce._classify_escalation_tier(-90) == "30-90 days overdue"


def test_classify_escalation_tier_91_days_overdue() -> None:
    assert ce._classify_escalation_tier(-91) == "90+ days overdue"


def test_classify_escalation_tier_365_days_overdue() -> None:
    assert ce._classify_escalation_tier(-365) == "90+ days overdue"


# ---------------------------------------------------------------------------
# historic regression: expired projects — each overdue band fires once (not suppressed)
# ---------------------------------------------------------------------------


# ── TestBug215ExpiredBandSuppression (flattened) ─────────────────────────────────────────────


def _make_expired_bug215_expired_band_suppression(
    tmp_path: Path, days_past: int, project: str = "hcs-drawdown"
) -> Path:
    fm = {
        "sf_contract_end": (
            datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=days_past)
        ).isoformat(),
        "sf_stage": "active",
        "sf_opportunity": "OPP-EXP",
    }
    return _make_project_file(tmp_path, "acme", project, fm)


def test_expired_first_run_fires(tmp_path: Path) -> None:
    """First encounter of an expired project always fires."""
    _make_expired_bug215_expired_band_suppression(tmp_path, days_past=5)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0]["tier"] == "expired"


def test_expired_same_band_suppressed_on_rerun(tmp_path: Path) -> None:
    """Re-running in the same overdue band on the same day suppresses the alert.

    implementation note: key now includes today's date — same-day re-runs are suppressed;
    a new run on a different calendar day re-alerts.
    """
    _make_expired_bug215_expired_band_suppression(tmp_path, days_past=5)
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    # implementation note + implementation note: key includes today's date between band and threshold suffix
    state = {f"pursuit-sf-close-date/v1/acme/hcs-drawdown/expired/0-30d/{today}/14-30-60": {"alerted_tier": "expired"}}
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, state=state)
    mock_alert.assert_not_called()


def test_expired_new_band_fires_after_0_30(tmp_path: Path) -> None:
    """Crossing from 0-30d band to 30-90d band fires a new alert.

    Even with today's date in the key, a different band is a different key.
    """
    # Project is now 45 days overdue (30-90d band)
    _make_expired_bug215_expired_band_suppression(tmp_path, days_past=45)
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    # Only the 0-30d key exists — 30-90d band key will be new
    state = {f"acme/hcs-drawdown/expired/0-30d/{today}/14-30-60": {"alerted_tier": "expired"}}
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, state=state)
    mock_alert.assert_called_once()


def test_expired_new_band_fires_after_30_90(tmp_path: Path) -> None:
    """Crossing from 30-90d band to 90d+ band fires a new alert."""
    # Project is now 100 days overdue (90d+ band)
    _make_expired_bug215_expired_band_suppression(tmp_path, days_past=100)
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    state = {
        f"pursuit-sf-close-date/v1/acme/hcs-drawdown/expired/0-30d/{today}/14-30-60": {"alerted_tier": "expired"},
        f"acme/hcs-drawdown/expired/30-90d/{today}/14-30-60": {"alerted_tier": "expired"},
    }
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, state=state)
    mock_alert.assert_called_once()


def test_expired_90d_plus_band_suppressed_on_rerun(tmp_path: Path) -> None:
    """Same-day re-runs in the 90d+ band are suppressed.

    implementation note: suppression is scoped to the current calendar day. A new day
    produces a fresh key and re-alerts.
    """
    _make_expired_bug215_expired_band_suppression(tmp_path, days_past=120)
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    state = {
        f"pursuit-sf-close-date/v1/acme/hcs-drawdown/expired/0-30d/{today}/14-30-60": {"alerted_tier": "expired"},
        f"pursuit-sf-close-date/v1/acme/hcs-drawdown/expired/30-90d/{today}/14-30-60": {"alerted_tier": "expired"},
        f"pursuit-sf-close-date/v1/acme/hcs-drawdown/expired/90d+/{today}/14-30-60": {"alerted_tier": "expired"},
    }
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path, state=state)
    mock_alert.assert_not_called()


def test_expired_state_key_includes_band(tmp_path: Path) -> None:
    """After a run, the saved state key must include the overdue band."""
    _make_expired_bug215_expired_band_suppression(tmp_path, days_past=5)
    with patch.object(ce, "append_alert"):
        _, saved = _run(tmp_path)
    # The key must be band-qualified, not bare "acme/hcs-drawdown/expired"
    assert any("expired/0-30d" in k for k in saved), f"Band-qualified key not found in state: {list(saved)}"
    assert "pursuit-sf-close-date/v1/acme/hcs-drawdown/expired" not in saved, "Bare expired key must not be written"


# ---------------------------------------------------------------------------
# historic regression: template files and dot-directories excluded from watcher scan
# ---------------------------------------------------------------------------


# ── TestBug060TemplateFilter (flattened) ─────────────────────────────────────────────


def test_template_file_not_scanned(tmp_path: Path) -> None:
    """A file named template.md must not trigger an alert."""
    fm = {
        "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=7)).isoformat(),
        "sf_stage": "active",
        "sf_opportunity": "OPP-TMPL",
    }
    _make_project_file(tmp_path, "acme", "template", fm)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_not_called()


def test_dot_directory_not_scanned(tmp_path: Path) -> None:
    """Projects under a dot-prefixed account directory must be excluded."""
    fm = {
        "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=7)).isoformat(),
        "sf_stage": "active",
        "sf_opportunity": "OPP-DOT",
    }
    # Create a project under .archive/ (dot-directory)
    _make_project_file(tmp_path, ".archive", "some-proj", fm)
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    mock_alert.assert_not_called()


def test_real_project_still_scanned(tmp_path: Path) -> None:
    """Normal project files are still scanned after the filter is applied."""
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)
    _make_project_file(tmp_path, "acme", "template", _FM_RED)  # should be excluded
    with patch.object(ce, "append_alert") as mock_alert:
        _run(tmp_path)
    # Only hcs-drawdown fires; template is excluded
    assert mock_alert.call_count == 1
    assert mock_alert.call_args[0][0]["pursuit"] == "hcs-drawdown"


def test_template_dot_dir_project_excluded(tmp_path: Path) -> None:
    """historic regression regression (sub-case of historic regression): projects under .template/ must be excluded.

    The guard at contract_expiry.py uses p.parts[...].startswith('.') to exclude
    any account directory whose name starts with a dot. This test specifically
    exercises the .template/ case with a non-template filename (some-project.md),
    isolating the dot-directory guard from the stem guard tested by
    test_template_file_not_scanned.
    """
    # Positive: real project must fire an alert
    _make_project_file(tmp_path, "acme", "real-project", _FM_RED)
    # Excluded: .template/ starts with '.' — excluded by dot-directory guard
    _make_project_file(tmp_path, ".template", "some-project", _FM_RED)
    with patch.object(ce, "append_alert") as mock_alert:
        rc, _ = _run(tmp_path)
    assert rc == 0
    # Only the real project fires; .template/ is excluded by dot-directory guard
    assert mock_alert.call_count == 1
    assert mock_alert.call_args[0][0]["account"] == "acme"


# ---------------------------------------------------------------------------
# Task 4.6 — Branch coverage: suppression and alert emission
# ---------------------------------------------------------------------------


def _make_ce_date_proxy(fixed_today: datetime.date) -> type:
    """Return a datetime proxy class for contract_expiry that returns a fixed today().

    Patches ``datetime`` in the module so that ``datetime.now(tz=...).date()``
    returns the fixed date.  Using a real subclass of datetime.datetime preserves
    isinstance() semantics.
    """

    class _CEDatetimeProxy(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.timezone | None = None) -> datetime.datetime:  # type: ignore[override]
            return datetime.datetime(fixed_today.year, fixed_today.month, fixed_today.day, tzinfo=tz)

    return _CEDatetimeProxy


# ── TestBranchCoverage (flattened) ─────────────────────────────────────────────


def test_suppresses_already_alerted_contract(tmp_path: Path) -> None:
    """Task 4.6a: alert is suppressed when state already records the same tier.

    A project expiring in 7 days (red tier) with prior state alerted_tier='red'
    must NOT emit a new alert.  Patches fieldkit.watch.contract_expiry.date
    with a real date subclass so date.today() returns a fixed value and
    isinstance() checks remain valid.
    """
    fixed_today = datetime.date(2026, 6, 10)
    # sf_contract_end as ISO string — parsed via date.fromisoformat in source.
    # 7 days from fixed_today → red tier (≤14).
    contract_end_str = "2026-06-17"

    fm = {
        "sf_contract_end": contract_end_str,
        "sf_stage": "active",
        "sf_opportunity": "OPP-SUP",
    }
    _make_project_file(tmp_path, "acme", "hcs-drawdown", fm)

    # State already records red tier for this project → should suppress
    # implementation note: state key includes threshold suffix (default 14-30-60)
    state = {"pursuit-sf-close-date/v1/acme/hcs-drawdown/red/14-30-60": {"alerted_tier": "red"}}

    datetime_proxy = _make_ce_date_proxy(fixed_today)
    with (
        patch("fieldkit.watch.contract_expiry.datetime", new=datetime_proxy),
        patch.object(ce, "append_alert") as mock_alert,
    ):
        rc, _ = _run(tmp_path, state=state)

    assert rc == 0
    mock_alert.assert_not_called(), ("Alert must be suppressed when the same tier was already alerted in state")


def test_emits_alert_for_expiring_contract(tmp_path: Path) -> None:
    """Task 4.6b: alert fires for a contract expiring within the red threshold.

    A project expiring in 7 days (red tier) with no prior state must emit
    exactly one alert.  Patches fieldkit.watch.contract_expiry.date with a
    real date subclass so date.today() returns a fixed value and isinstance()
    checks remain valid.
    """
    fixed_today = datetime.date(2026, 6, 10)
    # 7 days from fixed_today → red tier (≤14)
    contract_end_str = "2026-06-17"

    fm = {
        "sf_contract_end": contract_end_str,
        "sf_stage": "active",
        "sf_opportunity": "OPP-EMIT",
    }
    _make_project_file(tmp_path, "acme", "hcs-drawdown", fm)

    datetime_proxy = _make_ce_date_proxy(fixed_today)
    with (
        patch("fieldkit.watch.contract_expiry.datetime", new=datetime_proxy),
        patch.object(ce, "append_alert") as mock_alert,
    ):
        rc, _ = _run(tmp_path, state={})

    assert rc == 0
    mock_alert.assert_called_once(), "Alert must fire for a contract in the red tier with no prior state"
    result = mock_alert.call_args[0][0]
    assert result["tier"] == "red"
    assert result["account"] == "acme"
    assert result["pursuit"] == "hcs-drawdown"


# ---------------------------------------------------------------------------
# Additional branch coverage for _run_contract_expiry_inner (cc=27)
# ---------------------------------------------------------------------------


# ── TestRunContractExpiryInnerBranches (flattened) ─────────────────────────────────────────────


def test_classify_tier_boundary_minus_180() -> None:
    """_classify_tier(-180) is the last expired day (inclusive)."""
    assert ce._classify_tier(-180) == "expired"


def test_classify_tier_beyond_180_returns_none() -> None:
    """_classify_tier(-181) is beyond 180 days overdue → None (suppressed)."""
    assert ce._classify_tier(-181) is None


def test_load_state_corrupt_returns_empty(tmp_path: Path) -> None:
    """Lines 127-135: corrupt state file returns {}."""
    state_file = tmp_path / "state.json"
    state_file.write_text("not valid json {{{", encoding="utf-8")
    with patch.object(ce, "_state_file", return_value=state_file):
        result = ce._load_state()
    assert result == {}


def test_load_state_missing_returns_empty(tmp_path: Path) -> None:
    """_load_state() returns {} when file doesn't exist."""
    state_file = tmp_path / "nonexistent.json"
    with patch.object(ce, "_state_file", return_value=state_file):
        result = ce._load_state()
    assert result == {}


def test_save_state_oserror_propagates(tmp_path: Path) -> None:
    """Lines 139-147: _save_state raises OSError when write fails."""
    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()
    state_file = watchers_dir / "state.json"
    tmp_file = watchers_dir / "state.json.tmp"

    with (
        patch.object(ce, "_state_file", return_value=state_file),
        patch.object(ce, "get_watchers_dir", return_value=watchers_dir),
        # Patch Path.open on the tmp file to raise OSError
        patch.object(tmp_file.__class__, "open", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        ce._save_state({"key": "value"}, previous_state={})


def test_pursuit_read_error_is_fatal(tmp_path: Path) -> None:
    """An unreadable pursuit produces a nonzero watcher outcome."""
    # Create the accounts dir so the watcher proceeds past the early check
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    pursuit_dir = accounts_dir / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "bad-pursuit.md"
    pursuit_file.write_text("placeholder", encoding="utf-8")

    with (
        patch("fieldkit.watch.contract_expiry.get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state", return_value=None),
        patch.object(ce, "write_run_status", return_value=None),
        # Simulate read failure
        patch("fieldkit.watch.contract_expiry.Path.read_text", side_effect=OSError("permission denied")),
        patch.object(ce, "append_alert") as mock_alert,
    ):
        rc = ce._run_contract_expiry_inner(account_filter=None, dry_run=False)

    assert rc == 1
    mock_alert.assert_not_called()


def test_unparseable_contract_end_is_skipped(tmp_path: Path) -> None:
    """Lines 341-344: invalid sf_contract_end string → skipped."""
    fm = {
        "sf_contract_end": "not-a-date",
        "sf_stage": "active",
    }
    _make_project_file(tmp_path, "acme", "bad-date-proj", fm)

    with patch.object(ce, "append_alert") as mock_alert:
        rc, _ = _run(tmp_path)

    assert rc == 1
    mock_alert.assert_not_called()


def test_state_write_failure_is_fatal(tmp_path: Path) -> None:
    """A lost suppression state makes the run fatal."""
    _make_project_file(tmp_path, "acme", "hcs-drawdown", _FM_RED)

    with (
        patch("fieldkit.watch.contract_expiry.get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state", side_effect=OSError("disk full")),
        patch.object(ce, "write_run_status", return_value=None),
        patch.object(ce, "append_alert"),
    ):
        rc = ce._run_contract_expiry_inner(account_filter=None, dry_run=False)

    assert rc == 1


def test_frontmatter_none_result_is_skipped(tmp_path: Path) -> None:
    """Malformed pursuit frontmatter is skipped without emitting an alert."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "no-fm-proj.md").write_text("# No frontmatter\n", encoding="utf-8")
    with patch.object(ce, "append_alert") as mock_alert:
        rc, _ = _run(tmp_path)

    assert rc == 1
    mock_alert.assert_not_called()


def test_orange_tier_fires_alert(tmp_path: Path) -> None:
    """Orange tier (15-30 days) fires an alert."""
    fm = {
        "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=20)).isoformat(),
        "sf_stage": "active",
        "sf_opportunity": "OPP-ORG",
    }
    _make_project_file(tmp_path, "acme", "orange-proj", fm)
    with patch.object(ce, "append_alert") as mock_alert:
        rc, _ = _run(tmp_path)
    assert rc == 0
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0]["tier"] == "orange"


def test_sf_opportunity_id_fallback(tmp_path: Path) -> None:
    """sf_opportunity_id is used when sf_opportunity is absent."""
    fm = {
        "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=7)).isoformat(),
        "sf_stage": "active",
        "sf_opportunity_id": "OPP-ID-001",
    }
    _make_project_file(tmp_path, "acme", "opp-id-proj", fm)
    with patch.object(ce, "append_alert") as mock_alert:
        rc, _ = _run(tmp_path)
    assert rc == 0
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["sf_opportunity"] == "OPP-ID-001"


def test_closed_stage_skipped(tmp_path: Path) -> None:
    """'closed' (lowercase) stage is skipped like 'Closed'."""
    fm = {
        "sf_contract_end": (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=7)).isoformat(),
        "sf_stage": "closed",
    }
    _make_project_file(tmp_path, "acme", "closed-proj", fm)
    with patch.object(ce, "append_alert") as mock_alert:
        rc, _ = _run(tmp_path)
    assert rc == 0
    mock_alert.assert_not_called()
