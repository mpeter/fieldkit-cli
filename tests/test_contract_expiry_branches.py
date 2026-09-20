"""Branch-coverage tests for fieldkit.watch.contract_expiry._run_contract_expiry_inner."""

import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.watch import contract_expiry as ce

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _md_content(
    sf_contract_end: str | None = None,
    sf_stage: str | None = None,
    sf_opportunity: str = "",
) -> str:
    lines = ["---", "stage: closed-lost" if sf_stage in {"Completed", "Closed"} else "stage: qualify"]
    if sf_contract_end:
        lines.append(f"sf_close_date: {sf_contract_end}")
    if sf_opportunity:
        lines.append(f"sf_opportunity_id: {sf_opportunity}")
    lines += ["---", "", "# Pursuit"]
    return "\n".join(lines)


def _make_project(
    tmp_path: Path,
    account: str = "acme",
    project: str = "proj",
    sf_contract_end: str | None = None,
    sf_stage: str | None = None,
    sf_opportunity: str = "",
) -> Path:
    project_dir = tmp_path / "accounts" / account / "pursuits"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{project}.md"
    path.write_text(
        _md_content(sf_contract_end=sf_contract_end, sf_stage=sf_stage, sf_opportunity=sf_opportunity),
        encoding="utf-8",
    )
    return path


_KWARGS_BASE: dict[str, Any] = {"account_filter": None, "dry_run": False}


def _run(tmp_path: Path, **kwargs: Any) -> int:
    full_kwargs = {**_KWARGS_BASE, **kwargs}
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
    ):
        return ce._run_contract_expiry_inner(**full_kwargs)


# ---------------------------------------------------------------------------
# No accounts directory
# ---------------------------------------------------------------------------


# ── TestNoAccountsDir (flattened) ───────────────────────────────────────────


def testget_watchers_dir_no_accounts_dir_returns_1(tmp_path: Path) -> None:
    rc = _run(tmp_path)
    assert rc == 1


# ---------------------------------------------------------------------------
# No project files
# ---------------------------------------------------------------------------


# ── TestNoProjectFiles (flattened) ──────────────────────────────────────────


def test_process_project_path_no_projects_returns_0(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    rc = _run(tmp_path)
    assert rc == 0


# ---------------------------------------------------------------------------
# Dot-directory exclusion (historic regression)
# ---------------------------------------------------------------------------


# ── TestDotDirectoryExclusion (flattened) ───────────────────────────────────


def test_dot_directory_exclusion_dot_archive_excluded(tmp_path: Path) -> None:
    # Place a project under .archive/ — should be skipped
    dot_dir = tmp_path / "accounts" / ".archive" / "pursuits"
    dot_dir.mkdir(parents=True)
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=10)).isoformat()
    (dot_dir / "proj.md").write_text(_md_content(sf_contract_end=close), encoding="utf-8")

    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert") as mock_alert,
    ):
        ce._run_contract_expiry_inner(**_KWARGS_BASE)
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Completed/closed stages skipped
# ---------------------------------------------------------------------------


# ── TestCompletedStageSkipped (flattened) ───────────────────────────────────


def test_completed_stage_skipped_completed_stage_no_alert(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=10)).isoformat()
    _make_project(tmp_path, sf_contract_end=close, sf_stage="Completed")

    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert") as mock_alert,
    ):
        ce._run_contract_expiry_inner(**_KWARGS_BASE)
    mock_alert.assert_not_called()


def test_completed_stage_skipped_closed_stage_no_alert(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=5)).isoformat()
    _make_project(tmp_path, sf_contract_end=close, sf_stage="Closed")

    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert") as mock_alert,
    ):
        ce._run_contract_expiry_inner(**_KWARGS_BASE)
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Missing sf_contract_end skipped
# ---------------------------------------------------------------------------


# ── TestMissingContractEnd (flattened) ──────────────────────────────────────


def test_parse_contract_end_date_no_contract_end_skipped(tmp_path: Path) -> None:
    _make_project(tmp_path, sf_contract_end=None)

    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert") as mock_alert,
    ):
        ce._run_contract_expiry_inner(**_KWARGS_BASE)
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------


# ── TestTierClassification (flattened) ──────────────────────────────────────


def _tier_urgency_run_with_alert_capture(tmp_path: Path, days_delta: int) -> Any:
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=days_delta)).isoformat()
    _make_project(tmp_path, sf_contract_end=close)
    alerts: list[Any] = []
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert", side_effect=lambda r, dry_run: alerts.append(r)),
    ):
        ce._run_contract_expiry_inner(**_KWARGS_BASE)
    return alerts


def test_tier_urgency_red_tier_at_14_days(tmp_path: Path) -> None:
    alerts = _tier_urgency_run_with_alert_capture(tmp_path, 14)
    assert len(alerts) == 1
    assert alerts[0]["tier"] == "red"


def test_tier_urgency_orange_tier_at_25_days(tmp_path: Path) -> None:
    alerts = _tier_urgency_run_with_alert_capture(tmp_path, 25)
    assert len(alerts) == 1
    assert alerts[0]["tier"] == "orange"


def test_tier_urgency_yellow_tier_at_50_days(tmp_path: Path) -> None:
    alerts = _tier_urgency_run_with_alert_capture(tmp_path, 50)
    assert len(alerts) == 1
    assert alerts[0]["tier"] == "yellow"


def test_tier_urgency_no_alert_beyond_60_days(tmp_path: Path) -> None:
    alerts = _tier_urgency_run_with_alert_capture(tmp_path, 61)
    assert len(alerts) == 0


def test_tier_urgency_expired_negative_days(tmp_path: Path) -> None:
    alerts = _tier_urgency_run_with_alert_capture(tmp_path, -5)
    assert len(alerts) == 1
    assert alerts[0]["tier"] == "expired"


def test_tier_urgency_very_old_expired_suppressed(tmp_path: Path) -> None:
    # > 180 days overdue → classify_tier returns None → no alert
    alerts = _tier_urgency_run_with_alert_capture(tmp_path, -200)
    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# Account filter
# ---------------------------------------------------------------------------


# ── TestAccountFilter (flattened) ───────────────────────────────────────────


def test_account_filter_filter_limits_to_matching_account(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=10)).isoformat()
    _make_project(tmp_path, account="acme", sf_contract_end=close)
    _make_project(tmp_path, account="globalpay", sf_contract_end=close, project="gp-proj")

    alerts: list[Any] = []
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert", side_effect=lambda r, dry_run: alerts.append(r)),
    ):
        ce._run_contract_expiry_inner(account_filter="acme", dry_run=False)

    assert len(alerts) == 1
    assert alerts[0]["account"] == "acme"


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


# ── TestSuppression (flattened) ─────────────────────────────────────────────


def test_suppression_same_tier_suppressed(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=10)).isoformat()
    _make_project(tmp_path, sf_contract_end=close)
    # Red tier already alerted — implementation note: state key includes threshold suffix (default 14-30-60)
    prior_state = {"pursuit-sf-close-date/v1/acme/proj/red/14-30-60": {"alerted_tier": "red"}}

    alerts: list[Any] = []
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value=prior_state),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert", side_effect=lambda r, dry_run: alerts.append(r)),
    ):
        ce._run_contract_expiry_inner(**_KWARGS_BASE)
    assert len(alerts) == 0


def test_suppression_escalated_tier_fires(tmp_path: Path) -> None:
    today = datetime.date.today()
    # Orange tier (25 days), prior was yellow — should re-fire
    close = (today + datetime.timedelta(days=25)).isoformat()
    _make_project(tmp_path, sf_contract_end=close)
    # implementation note: state key includes threshold suffix (default 14-30-60);
    # prior yellow alert under yellow key — orange key absent → escalation fires
    prior_state = {"acme/proj/yellow/14-30-60": {"alerted_tier": "yellow"}}

    alerts: list[Any] = []
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value=prior_state),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert", side_effect=lambda r, dry_run: alerts.append(r)),
    ):
        ce._run_contract_expiry_inner(**_KWARGS_BASE)
    assert len(alerts) == 1


def test_suppression_expired_bands_key(tmp_path: Path) -> None:
    """historic regression: expired projects use band suffix in state key."""
    today = datetime.date.today()
    close = (today - datetime.timedelta(days=15)).isoformat()  # 15 days overdue → 0-30d band
    _make_project(tmp_path, sf_contract_end=close)

    saved_states: list[dict[str, Any]] = []
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state", side_effect=lambda state, **_: saved_states.append(state)),
        patch.object(ce, "append_alert"),
    ):
        ce._run_contract_expiry_inner(**_KWARGS_BASE)

    assert len(saved_states) > 0
    keys = list(saved_states[-1].keys())
    # Key should contain the band suffix e.g. "acme/proj/expired/0-30d"
    assert any("expired" in k and ("0-30d" in k or "30-90d" in k or "90d+" in k) for k in keys)


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


# ── TestDryRun (flattened) ──────────────────────────────────────────────────


def test_run_contract_expiry_dry_run_does_not_save_state(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=10)).isoformat()
    _make_project(tmp_path, sf_contract_end=close)

    save_calls: list[Any] = []
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state", side_effect=save_calls.append),
        patch.object(ce, "append_alert"),
    ):
        ce._run_contract_expiry_inner(account_filter=None, dry_run=True)

    assert save_calls == []


def test_run_contract_expiry_dry_run_passes_flag_to_append(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=10)).isoformat()
    _make_project(tmp_path, sf_contract_end=close)

    call_args: list[Any] = []
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert", side_effect=lambda r, dry_run: call_args.append(dry_run)),
    ):
        ce._run_contract_expiry_inner(account_filter=None, dry_run=True)

    assert call_args and call_args[0] is True


# ---------------------------------------------------------------------------
# Unparseable date
# ---------------------------------------------------------------------------


# ── TestUnparseableDate (flattened) ─────────────────────────────────────────


def test_parse_contract_end_date_bad_date_skipped(tmp_path: Path) -> None:
    content = "---\nstage: qualify\nsf_close_date: not-a-date\n---\n\n# Pursuit"
    proj_dir = tmp_path / "accounts" / "acme" / "pursuits"
    proj_dir.mkdir(parents=True)
    (proj_dir / "proj.md").write_text(content, encoding="utf-8")

    alerts: list[Any] = []
    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert", side_effect=lambda r, dry_run: alerts.append(r)),
    ):
        rc = ce._run_contract_expiry_inner(**_KWARGS_BASE)
    assert rc == 1
    assert alerts == []


# ---------------------------------------------------------------------------
# No frontmatter skipped
# ---------------------------------------------------------------------------


# ── TestNoFrontmatter (flattened) ───────────────────────────────────────────


def test_parse_project_frontmatter_no_frontmatter_skipped(tmp_path: Path) -> None:
    proj_dir = tmp_path / "accounts" / "acme" / "pursuits"
    proj_dir.mkdir(parents=True)
    (proj_dir / "proj.md").write_text("# No frontmatter here\n", encoding="utf-8")

    with (
        patch.object(ce, "get_fieldkit_home", return_value=tmp_path),
        patch.object(ce, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(ce, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(ce, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(ce, "write_run_status"),
        patch.object(ce, "_load_state", return_value={}),
        patch.object(ce, "_save_state"),
        patch.object(ce, "append_alert") as mock_alert,
    ):
        rc = ce._run_contract_expiry_inner(**_KWARGS_BASE)
    assert rc == 1
    mock_alert.assert_not_called()
