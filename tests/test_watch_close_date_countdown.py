"""Tests for fieldkit/watch/close_date_countdown.py.

Covers:
- _classify_tier() buckets red/yellow/green/overdue/skip correctly
- closed-won / closed-lost pursuits skipped
- pursuits with no sf_close_date skipped
- sf_close_date as ISO string parses correctly
- sf_close_date as datetime.date object works
- should_suppress_countdown() same tier is suppressed on second run
- should_suppress_countdown() tier change (yellow→red) is not suppressed
- MEDDPICC gap detection (champion=0 and economic_buyer=0)
- dry-run flag prevents file writes
- overdue pursuit (≤0 days) emits alert with 🔴 tier
"""

import datetime
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.models import PursuitFrontmatter
from fieldkit.watch import close_date_countdown as cdc

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TODAY = datetime.date(2026, 6, 6)


def _fm(
    *,
    stage: str = "discovery",
    sf_close_date: Any = None,
    sf_next_steps: str = "",
    meddpicc: Any = None,
) -> SimpleNamespace:
    """Return a minimal pursuit frontmatter namespace."""
    return SimpleNamespace(
        stage=stage,
        sf_close_date=sf_close_date,
        sf_next_steps=sf_next_steps,
        meddpicc=meddpicc,
    )


def _meddpicc(
    champion: int = 2,
    economic_buyer: int = 2,
    paper_process: int = 2,
    total: int = 16,
) -> SimpleNamespace:
    return SimpleNamespace(
        champion=champion,
        economic_buyer=economic_buyer,
        paper_process=paper_process,
        total=total,
    )


# ---------------------------------------------------------------------------
# _classify_tier — unit tests
# ---------------------------------------------------------------------------


# ── TestClassifyTier (flattened) ─────────────────────────────────────────────

_KWARGS_classify_tier: dict[str, int] = {"threshold_red": 14, "threshold_yellow": 30, "threshold_green": 60}


def test_classify_tier_red_boundary() -> None:
    assert cdc._classify_tier(14, **_KWARGS_classify_tier) == "red"


def test_classify_tier_red_overdue() -> None:
    # overdue (negative days) is still red
    assert cdc._classify_tier(-5, **_KWARGS_classify_tier) == "red"


def test_classify_tier_yellow_low() -> None:
    assert cdc._classify_tier(15, **_KWARGS_classify_tier) == "yellow"


def test_classify_tier_yellow_boundary() -> None:
    assert cdc._classify_tier(30, **_KWARGS_classify_tier) == "yellow"


def test_classify_tier_green_low() -> None:
    assert cdc._classify_tier(31, **_KWARGS_classify_tier) == "green"


def test_classify_tier_green_boundary() -> None:
    assert cdc._classify_tier(60, **_KWARGS_classify_tier) == "green"


def test_classify_tier_skip_beyond_green() -> None:
    assert cdc._classify_tier(61, **_KWARGS_classify_tier) is None


def test_classify_tier_skip_far_future() -> None:
    assert cdc._classify_tier(200, **_KWARGS_classify_tier) is None


# ---------------------------------------------------------------------------
# should_suppress_countdown
# ---------------------------------------------------------------------------


# ── TestSuppression (flattened) ─────────────────────────────────────────────


def test_first_time_not_suppressed() -> None:
    assert cdc.should_suppress_countdown("acme/deal", "red", {}) is False


def test_same_tier_suppressed() -> None:
    state = {"acme/deal": {"alerted_tier": "red"}}
    assert cdc.should_suppress_countdown("acme/deal", "red", state) is True


def test_tier_change_not_suppressed_simple() -> None:
    state = {"acme/deal": {"alerted_tier": "yellow"}}
    assert cdc.should_suppress_countdown("acme/deal", "red", state) is False


def test_different_pursuit_not_suppressed() -> None:
    state = {"acme/other": {"alerted_tier": "red"}}
    assert cdc.should_suppress_countdown("acme/deal", "red", state) is False


# ---------------------------------------------------------------------------
# append_countdown_alert — dry_run prevents writes
# ---------------------------------------------------------------------------


# ── TestAppendCountdownAlert (flattened) ─────────────────────────────────────────────


def _result_append_countdown_alert(tier: str = "red", days: int = 5) -> dict[str, Any]:
    return {
        "account": "acme",
        "pursuit": "big-deal",
        "tier": tier,
        "days_to_close": days,
        "close_date": "2026-06-11",
        "native_qualification": "unavailable (live ClosePlan fetch required)",
        "champion": "Jane Doe",
        "next_steps": "Demo next week",
        "rel_path": "accounts/acme/pursuits/big-deal.md",
    }


def test_append_countdown_alert_dry_run_no_writes(tmp_path: Path) -> None:
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(cdc, "_alerts_file", return_value=alerts_file),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path),
    ):
        cdc.append_countdown_alert(_result_append_countdown_alert(), dry_run=True)
    assert not alerts_file.exists()


def test_append_countdown_alert_wet_run_writes_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(cdc, "_alerts_file", return_value=alerts_file),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path),
    ):
        cdc.append_countdown_alert(_result_append_countdown_alert(), dry_run=False)
    assert alerts_file.exists()
    content = alerts_file.read_text()
    assert "acme/big-deal" in content
    assert "🔴" in content


def test_append_countdown_alert_overdue_label_present(tmp_path: Path) -> None:
    alerts_file = tmp_path / "alerts.md"
    with (
        patch.object(cdc, "_alerts_file", return_value=alerts_file),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path),
    ):
        cdc.append_countdown_alert(_result_append_countdown_alert(days=-3), dry_run=False)
    content = alerts_file.read_text()
    assert "OVERDUE" in content


# ---------------------------------------------------------------------------
# _run_countdown — integration over mocked pursuits
# ---------------------------------------------------------------------------

_PURSUIT_PATH = Path("accounts/acme/pursuits/big-deal.md")


def _make_run_patches(
    pursuits: list[Path],
    fm_map: dict[Path, Any],
    watchers_dir: Path,
    state: dict[str, Any] | None = None,
) -> list[Any]:
    """Return a list of context managers that patch _run_countdown dependencies."""
    return [
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=watchers_dir.parent),
        patch.object(cdc, "get_watchers_dir", return_value=watchers_dir),
        patch.object(cdc, "_alerts_file", return_value=watchers_dir / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=watchers_dir / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter(pursuits)),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm_map[p], "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value=state or {}),
        patch.object(cdc, "_save_state", return_value=None),
    ]


# ── TestRunCountdown (flattened) ─────────────────────────────────────────────

_KWARGS_run_countdown: dict[str, Any] = {
    "threshold_red": 14,
    "threshold_yellow": 30,
    "threshold_green": 60,
    "account_filter": None,
    "dry_run": False,
}


def _run_run_countdown(pursuits: list[Path], fm_map: dict[Path, Any], tmp_path: Path, **kwargs: Any) -> int:
    patchers = _make_run_patches(pursuits, fm_map, tmp_path / "watchers", **kwargs)
    # Enter all context managers
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        return cdc._run_countdown(**_KWARGS_run_countdown)
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)


def test_run_countdown_skip_closed_pursuit(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "acme" / "pursuits" / "closed-deal.md"
    fm = _fm(stage="closed-won", sf_close_date="2026-06-10")
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_not_called()


def test_run_countdown_skip_missing_close_date(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "acme" / "pursuits" / "no-date.md"
    fm = _fm(stage="discovery", sf_close_date=None)
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_not_called()


def test_run_countdown_date_coercion_string(tmp_path: Path) -> None:
    """sf_close_date as ISO string triggers an alert for a near-term date."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()  # red tier
    path = tmp_path / "accounts" / "acme" / "pursuits" / "string-date.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["tier"] == "red"


def test_run_countdown_date_coercion_date_object(tmp_path: Path) -> None:
    """sf_close_date as datetime.date object is handled without conversion error."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = today + datetime.timedelta(days=7)  # already a date object
    path = tmp_path / "accounts" / "acme" / "pursuits" / "date-obj.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["tier"] == "red"


def test_run_countdown_meddpicc_gap_detection(tmp_path: Path) -> None:
    """Historical values do not infer current qualification gaps."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "gap-deal.md"
    fm = _fm(
        stage="discovery",
        sf_close_date=close,
        meddpicc=_meddpicc(champion=0, economic_buyer=0, total=8),
    )
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["native_qualification"] == "unavailable (no Salesforce opportunity link)"
    assert "critical_gaps" not in result
    assert "meddpicc_score" not in result


def test_run_countdown_suppression_same_tier(tmp_path: Path) -> None:
    """Alert is suppressed when pursuit is still in the same tier."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()  # red
    path = tmp_path / "accounts" / "acme" / "pursuits" / "repeat-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())
    prior_state = {"acme/repeat-deal": {"alerted_tier": "red"}}
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value=prior_state),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_not_called()


def test_run_countdown_suppression_tier_change(tmp_path: Path) -> None:
    """Alert fires when tier changes from yellow → red."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()  # red
    path = tmp_path / "accounts" / "acme" / "pursuits" / "escalating-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())
    # Prior state had yellow; now it's red → should NOT suppress
    prior_state = {"acme/escalating-deal": {"alerted_tier": "yellow"}}
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value=prior_state),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0]["tier"] == "red"


def test_run_countdown_dry_run_no_writes(tmp_path: Path) -> None:
    """dry_run=True passes dry_run=True to append_countdown_alert."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "dry-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter=None,
            dry_run=True,
        )
    mock_alert.assert_called_once()
    # dry_run arg must be True
    assert mock_alert.call_args[0][1] is True


def test_run_countdown_overdue_alert(tmp_path: Path) -> None:
    """Overdue pursuit (days ≤ 0) is classified as red and alert fires."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today - datetime.timedelta(days=3)).isoformat()  # 3 days overdue
    path = tmp_path / "accounts" / "acme" / "pursuits" / "overdue-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["tier"] == "red"
    assert result["days_to_close"] < 0


def test_run_countdown_meddpicc_none_fields_flagged_as_gaps(tmp_path: Path) -> None:
    """Missing historical values remain irrelevant to current native status."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "none-meddpicc.md"
    meddpicc = SimpleNamespace(champion=None, economic_buyer=None, total=0)
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=meddpicc)
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["native_qualification"] == "unavailable (no Salesforce opportunity link)"
    assert "critical_gaps" not in result


def test_native_qualification_status_requires_live_fetch_for_linked_pursuit() -> None:
    from fieldkit.pursuit.qualification import native_qualification_status

    fm = PursuitFrontmatter.model_validate({"stage": Stage.PROPOSE, "sf_opportunity_id": "006example"})

    result = native_qualification_status(fm.sf_opportunity_id)

    assert result == "pending (live ClosePlan fetch required)"


def test_run_countdown_red_tier_reports_unconfirmed_buyer_and_missing_paper_process(tmp_path: Path) -> None:
    """Configured tiers remain independent from historical qualification values."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=23)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "near-close-deal.md"
    fm = _fm(
        stage="propose",
        sf_close_date=close,
        meddpicc=_meddpicc(champion=2, economic_buyer=1, paper_process=0, total=6),
    )
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Example Stakeholder"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(
            threshold_red=30,
            threshold_yellow=45,
            threshold_green=60,
            account_filter=None,
            dry_run=True,
        )

    mock_alert.assert_called_once()
    result = mock_alert.call_args.args[0]
    assert result["tier"] == "red"
    assert result["native_qualification"] == "unavailable (no Salesforce opportunity link)"


def test_run_countdown_next_steps_body_fallback(tmp_path: Path) -> None:
    """historic regression: next_steps falls back to body text when sf_next_steps is empty."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "body-steps.md"
    fm = _fm(stage="discovery", sf_close_date=close, sf_next_steps="", meddpicc=_meddpicc())
    body = "Some content\nNext steps: Follow up with CTO on architecture review\nMore text"
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, body, datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["next_steps"] == "Follow up with CTO on architecture review"
    assert result["next_steps"] != "(none)"


def test_run_countdown_next_steps_body_fallback_truncated(tmp_path: Path) -> None:
    """historic regression: body-extracted next_steps is truncated at 200 chars."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "long-steps.md"
    fm = _fm(stage="discovery", sf_close_date=close, sf_next_steps="", meddpicc=_meddpicc())
    long_text = "x" * 300
    body = f"Next steps: {long_text}"
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, body, datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert len(result["next_steps"]) <= 200


def test_run_countdown_none_close_date_logs_debug_not_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """historic regression: a pursuit with sf_close_date=None logs at DEBUG level, not WARNING."""
    path = tmp_path / "accounts" / "acme" / "pursuits" / "no-date-log.md"
    fm = _fm(stage="discovery", sf_close_date=None)
    import logging

    with (
        caplog.at_level(logging.DEBUG, logger="fieldkit.watch"),
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "watcher_logging", return_value=nullcontext()),
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)

    # The "No sf_close_date" message must appear only at DEBUG level
    close_date_msgs = [
        (r.levelname, r.message)
        for r in caplog.records
        if "sf_close_date" in r.message or "no sf_close_date" in r.message.lower()
    ]
    assert close_date_msgs, "Expected a log message about missing sf_close_date"
    for levelname, _ in close_date_msgs:
        assert levelname == "DEBUG", f"Expected DEBUG, got {levelname} for sf_close_date message"


def test_run_countdown_empty_string_close_date_logs_debug_not_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """historic regression: a pursuit with sf_close_date="" logs at DEBUG level, not WARNING."""
    path = tmp_path / "accounts" / "acme" / "pursuits" / "empty-date-log.md"
    fm = _fm(stage="discovery", sf_close_date="")
    import logging

    with (
        caplog.at_level(logging.DEBUG, logger="fieldkit.watch"),
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "watcher_logging", return_value=nullcontext()),
    ):
        cdc._run_countdown(**_KWARGS_run_countdown)

    # The "No sf_close_date" message must appear only at DEBUG level
    close_date_msgs = [
        (r.levelname, r.message)
        for r in caplog.records
        if "sf_close_date" in r.message or "no sf_close_date" in r.message.lower()
    ]
    assert close_date_msgs, "Expected a log message about missing sf_close_date"
    for levelname, _ in close_date_msgs:
        assert levelname == "DEBUG", f"Expected DEBUG, got {levelname} for sf_close_date message"


# ---------------------------------------------------------------------------
# historic regression: within-run duplicate suppression
# ---------------------------------------------------------------------------


# ── TestBug175WithinRunDedup (flattened) ─────────────────────────────────────────────

_KWARGS_bug175_within_run_dedup: dict[str, Any] = {
    "threshold_red": 14,
    "threshold_yellow": 30,
    "threshold_green": 60,
    "account_filter": None,
    "dry_run": False,
}


def test_duplicate_path_alerts_only_once(tmp_path: Path) -> None:
    """When the same pursuit path appears twice in iterate_pursuits output,
    only one alert block should be written (second occurrence is suppressed
    because updated_state already records alerted_tier from the first pass)."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()  # red tier
    path = tmp_path / "accounts" / "acme" / "pursuits" / "dup-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())

    # Provide the same path twice — simulates symlink / duplicate iteration
    duplicate_paths = [path, path]
    fm_map = {path: fm}

    saved_states: list[dict[str, Any]] = []

    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter(duplicate_paths)),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm_map[p], "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", side_effect=lambda state, **_: saved_states.append(state)),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        rc = cdc._run_countdown(**_KWARGS_bug175_within_run_dedup)

    assert rc == 0
    # Only one alert should have been written despite two path occurrences
    assert mock_alert.call_count == 1, (
        f"Expected 1 alert call, got {mock_alert.call_count} — "
        "historic regression: suppression must use updated_state, not original state"
    )
    # The saved state should record alerted_tier for the pursuit
    assert saved_states, "State should have been saved"
    final_state = saved_states[-1]
    assert "acme/dup-deal" in final_state
    assert final_state["acme/dup-deal"]["alerted_tier"] == "red"


# ---------------------------------------------------------------------------
# historic regression: stale state pruning
# ---------------------------------------------------------------------------


# ── TestBug187StatePruning (flattened) ─────────────────────────────────────────────

_KWARGS_bug187_state_pruning: dict[str, Any] = {
    "threshold_red": 14,
    "threshold_yellow": 30,
    "threshold_green": 60,
    "account_filter": None,
    "dry_run": False,
}


def test_stale_key_not_in_saved_state(tmp_path: Path) -> None:
    """A state entry for a deleted pursuit must not appear in the saved state
    after _run_countdown completes."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()
    live_path = tmp_path / "accounts" / "acme" / "pursuits" / "live-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())

    # Stale key: "acme/deleted-deal" — no corresponding file in iterate_pursuits
    stale_state: dict[str, Any] = {
        "acme/deleted-deal": {
            "alerted_tier": "yellow",
            "checked_at": "2026-01-01T00:00:00Z",
        }
    }

    saved_states: list[dict[str, Any]] = []

    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([live_path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm, "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value=stale_state),
        patch.object(cdc, "_save_state", side_effect=lambda state, **_: saved_states.append(state)),
        patch.object(cdc, "append_countdown_alert"),
    ):
        rc = cdc._run_countdown(**_KWARGS_bug187_state_pruning)

    assert rc == 0
    assert saved_states, "State should have been saved"
    final_state = saved_states[-1]
    assert "acme/deleted-deal" not in final_state, (
        "historic regression: stale key for deleted pursuit must be pruned from saved state"
    )
    # The live pursuit should still be present
    assert "acme/live-deal" in final_state


def test_live_key_preserved_in_saved_state(tmp_path: Path) -> None:
    """A state entry for a pursuit that still exists on disk must be kept."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "existing-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())

    # Pre-existing state for the same pursuit (same tier → will be suppressed)
    prior_state: dict[str, Any] = {
        "acme/existing-deal": {
            "alerted_tier": "red",
            "checked_at": "2026-06-01T00:00:00Z",
        }
    }

    saved_states: list[dict[str, Any]] = []

    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm, "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value=prior_state),
        patch.object(cdc, "_save_state", side_effect=lambda state, **_: saved_states.append(state)),
        patch.object(cdc, "append_countdown_alert"),
    ):
        rc = cdc._run_countdown(**_KWARGS_bug187_state_pruning)

    assert rc == 0
    assert saved_states, "State should have been saved"
    final_state = saved_states[-1]
    assert "acme/existing-deal" in final_state, "Live pursuit key must be preserved in state"


def test_no_extra_write_when_nothing_pruned(tmp_path: Path) -> None:
    """When all state entries correspond to live pursuits, _save_state must be
    called exactly once (the normal end-of-run save) — not twice.

    historic regression fix: the prune step must not trigger a second _save_state call when
    there are no stale keys to remove.  A spurious rewrite would mean every run
    dirtied the state file even when nothing changed.
    """
    today = datetime.datetime.now(tz=datetime.UTC).date()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "current-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close, meddpicc=_meddpicc())

    # State whose only key matches the live pursuit — nothing to prune.
    prior_state: dict[str, Any] = {
        "acme/current-deal": {
            "alerted_tier": "red",
            "checked_at": "2026-06-01T00:00:00Z",
        }
    }

    saved_states: list[dict[str, Any]] = []

    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm, "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value=prior_state),
        patch.object(cdc, "_save_state", side_effect=lambda state, **_: saved_states.append(state)),
        patch.object(cdc, "append_countdown_alert"),
    ):
        rc = cdc._run_countdown(**_KWARGS_bug187_state_pruning)

    assert rc == 0
    assert len(saved_states) == 1, (
        f"historic regression: _save_state must be called exactly once when nothing is pruned; got {len(saved_states)} call(s)"
    )
    # The live pursuit key must be present in the single saved state.
    # Note: the watcher legitimately updates the state entry with current scan
    # results (tier, checked_at, etc.) — so value equality is NOT asserted here.
    # What we verify is: exactly one save (no spurious prune-triggered rewrite).
    final_state = saved_states[0]
    assert "acme/current-deal" in final_state, "Live pursuit key must survive the no-prune path"


# ---------------------------------------------------------------------------
# historic regression: datetime.datetime coercion for sf_close_date
# ---------------------------------------------------------------------------


# ── TestBug189DatetimeCoercion (flattened) ─────────────────────────────────────────────

_KWARGS_bug189_datetime_coercion: dict[str, Any] = {
    "threshold_red": 14,
    "threshold_yellow": 30,
    "threshold_green": 60,
    "account_filter": None,
    "dry_run": False,
}


def test_datetime_object_no_warning_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """sf_close_date as datetime.datetime must not produce a WARNING log entry.
    The coercion to date must happen silently before the isinstance check."""
    import logging

    today = datetime.datetime.now(tz=datetime.UTC).date()
    # Provide a datetime.datetime (as yaml.safe_load would return)
    close_dt = datetime.datetime(today.year, today.month, today.day) + datetime.timedelta(days=7)
    path = tmp_path / "accounts" / "acme" / "pursuits" / "dt-coerce.md"
    fm = _fm(stage="discovery", sf_close_date=close_dt, meddpicc=_meddpicc())

    with (
        caplog.at_level(logging.WARNING, logger="fieldkit.watch"),
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm, "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        rc = cdc._run_countdown(**_KWARGS_bug189_datetime_coercion)

    assert rc == 0
    # No WARNING about unparseable sf_close_date
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING and "sf_close_date" in r.message]
    assert not warning_msgs, (
        f"historic regression: datetime.datetime sf_close_date must not produce a WARNING; got: {warning_msgs}"
    )
    # The alert must still fire (date is in red tier)
    mock_alert.assert_called_once()
    assert mock_alert.call_args[0][0]["tier"] == "red"


def test_datetime_object_parsed_as_correct_date(tmp_path: Path) -> None:
    """sf_close_date as datetime.datetime is coerced to the correct date value."""
    today = datetime.datetime.now(tz=datetime.UTC).date()
    target_date = today + datetime.timedelta(days=7)
    close_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
    path = tmp_path / "accounts" / "acme" / "pursuits" / "dt-date.md"
    fm = _fm(stage="discovery", sf_close_date=close_dt, meddpicc=_meddpicc())

    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm, "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        rc = cdc._run_countdown(**_KWARGS_bug189_datetime_coercion)

    assert rc == 0
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    # close_date in result should be the ISO date string (not datetime string)
    assert result["close_date"] == target_date.isoformat(), (
        f"historic regression: close_date should be {target_date.isoformat()!r}, got {result['close_date']!r}"
    )
    assert result["tier"] == "red"


# ---------------------------------------------------------------------------
# Task 1 — alert dedup: append_countdown_alert skips duplicates for same date+slug
# ---------------------------------------------------------------------------


# ── TestAppendCountdownAlertDedup (flattened) ─────────────────────────────────────────────


def _result_append_countdown_alert_dedup(
    account: str = "acme", pursuit: str = "big-deal", tier: str = "red", days: int = 5
) -> dict[str, Any]:
    return {
        "account": account,
        "pursuit": pursuit,
        "tier": tier,
        "days_to_close": days,
        "close_date": "2026-06-15",
        "native_qualification": "unavailable (live ClosePlan fetch required)",
        "champion": "Jane Doe",
        "next_steps": "Demo next week",
        "rel_path": f"accounts/{account}/pursuits/{pursuit}.md",
    }


def test_first_write_is_appended(tmp_path: Path) -> None:
    """First alert for a date+slug is written to the alert file."""
    alerts_file = tmp_path / "close-date-countdown-alerts.md"
    alerts_file.write_text("# Close-Date Countdown Alerts\n\nAutomated alerts.\n", encoding="utf-8")

    with (
        patch.object(cdc, "_alerts_file", return_value=alerts_file),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path),
    ):
        original_size = alerts_file.stat().st_size
        cdc.append_countdown_alert(_result_append_countdown_alert_dedup(), dry_run=False)

    assert alerts_file.stat().st_size > original_size, "Alert file must grow on the first write"


def test_duplicate_alert_is_not_written(tmp_path: Path) -> None:
    """A second call with the same date+slug must not grow the alert file."""
    today_str = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    account = "acme"
    pursuit = "big-deal"
    alerts_file = tmp_path / "close-date-countdown-alerts.md"
    # Pre-populate with a heading matching what append_countdown_alert would write
    alerts_file.write_text(
        "# Close-Date Countdown Alerts\n\n"
        f"## {today_str} — {account}/{pursuit} — closes in 5 day(s) 🔴\n\n"
        f"- **Pursuit:** [{pursuit}](accounts/{account}/pursuits/{pursuit}.md)\n",
        encoding="utf-8",
    )

    with (
        patch.object(cdc, "_alerts_file", return_value=alerts_file),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path),
    ):
        size_before = alerts_file.stat().st_size
        cdc.append_countdown_alert(
            _result_append_countdown_alert_dedup(account=account, pursuit=pursuit), dry_run=False
        )
        size_after = alerts_file.stat().st_size

    assert size_after == size_before, "Alert file must NOT grow when the heading already exists (dedup check)"


def test_different_pursuit_same_date_is_written(tmp_path: Path) -> None:
    """A different pursuit on the same date IS written (no false suppression)."""
    today_str = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    account = "acme"
    pursuit_a = "big-deal"
    pursuit_b = "other-deal"

    alerts_file = tmp_path / "close-date-countdown-alerts.md"
    # Pre-populate only pursuit_a
    alerts_file.write_text(
        "# Close-Date Countdown Alerts\n\n"
        f"## {today_str} — {account}/{pursuit_a} — closes in 5 day(s) 🔴\n\n"
        f"- **Pursuit:** [{pursuit_a}](accounts/{account}/pursuits/{pursuit_a}.md)\n",
        encoding="utf-8",
    )

    with (
        patch.object(cdc, "_alerts_file", return_value=alerts_file),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path),
    ):
        size_before = alerts_file.stat().st_size
        cdc.append_countdown_alert(
            _result_append_countdown_alert_dedup(account=account, pursuit=pursuit_b), dry_run=False
        )
        content = alerts_file.read_text(encoding="utf-8")

    assert alerts_file.stat().st_size > size_before, (
        "A new pursuit slug must be written even when another slug exists for the same date"
    )
    assert pursuit_b in content


# ---------------------------------------------------------------------------
# Tasks 4.1-4.5 -- Branch coverage: suppression, escalation, filters, skips
# ---------------------------------------------------------------------------


def _make_date_proxy(fixed_today: datetime.date) -> type:
    """Return a date proxy class that behaves like datetime.date but returns
    a fixed value from today().

    Using a real subclass of datetime.date preserves isinstance() semantics:
    isinstance(datetime.date(...), proxy_class) is True because proxy_class
    inherits from datetime.date.  This avoids the TypeError that occurs when
    the whole 'date' name is replaced with a MagicMock (which is not a type).
    """

    class _DateProxy(datetime.date):
        @classmethod
        def today(cls) -> datetime.date:  # type: ignore[override]
            return fixed_today

        @classmethod
        def fromisoformat(cls, s: str) -> datetime.date:  # type: ignore[override]
            return datetime.date.fromisoformat(s)

    return _DateProxy


def _make_datetime_proxy(fixed_today: datetime.date) -> type:
    """Return a datetime proxy class that intercepts datetime.now().

    The production code uses ``from datetime import datetime`` and calls
    ``datetime.now(tz=UTC).date()``.  Patching the module-level ``date``
    name only covers ``date.today()``; ``datetime.now()`` bypasses that
    binding entirely.  This proxy patches the ``datetime`` name so
    ``datetime.now(tz=...)`` returns a controlled value whose ``.date()``
    equals *fixed_today*.

    Using a real subclass preserves isinstance() semantics and avoids
    TypeError when the source code uses datetime in type checks.
    """

    class _DatetimeProxy(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.timezone | None = None) -> "datetime.datetime":  # type: ignore[override]
            return datetime.datetime.combine(fixed_today, datetime.time.min, tzinfo=tz)

    return _DatetimeProxy


# ── TestBranchCoverage (flattened) ─────────────────────────────────────────────

_KWARGS_BASE_branch_coverage: dict[str, Any] = {
    "threshold_red": 14,
    "threshold_yellow": 30,
    "threshold_green": 60,
    "dry_run": False,
}


def _make_patches_branch_coverage(
    tmp_path: Path,
    pursuits: list[Path],
    fm_map: dict[Path, Any],
    state: dict[str, Any] | None = None,
) -> list[Any]:
    """Return context managers for _run_countdown_inner dependencies."""
    return [
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}, "globalpay": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter(pursuits)),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm_map[p], "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value=state or {}),
        patch.object(cdc, "_save_state", return_value=None),
    ]


def test_suppresses_when_same_tier_in_state(tmp_path: Path) -> None:
    """Task 4.1: alert is suppressed when state already records the same tier.

    Patches fieldkit.watch.close_date_countdown.date (module-level binding)
    so date.today() returns a fixed date that places the pursuit in the RED
    tier.  Pre-populating state with alerted_tier='red' must suppress the alert.

    Uses a real datetime.date subclass as the proxy so isinstance() checks
    in the source code continue to work correctly.
    """
    # Fixed today: 2026-06-10. Close date 5 days away → red tier.
    fixed_today = datetime.date(2026, 6, 10)
    close_date_str = "2026-06-15"  # 5 days from fixed_today → red

    path = tmp_path / "accounts" / "acme" / "pursuits" / "same-tier-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close_date_str, meddpicc=_meddpicc())
    fm_map = {path: fm}

    # State already records red tier for this pursuit → should suppress
    prior_state = {"acme/same-tier-deal": {"alerted_tier": "red"}}

    date_proxy = _make_date_proxy(fixed_today)
    patchers = _make_patches_branch_coverage(tmp_path, [path], fm_map, state=prior_state)
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        with (
            patch("fieldkit.watch.close_date_countdown.date", new=date_proxy),
            patch.object(cdc, "append_countdown_alert") as mock_alert,
        ):
            rc = cdc._run_countdown(**{**_KWARGS_BASE_branch_coverage, "account_filter": None})
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)

    assert rc == 0
    mock_alert.assert_not_called(), "Alert must be suppressed when same tier is already in state"


def test_escalates_tier_when_closer(tmp_path: Path) -> None:
    """Task 4.2: alert fires when pursuit moves from YELLOW to RED tier.

    Prior state has alerted_tier='yellow'.  Patching date so the pursuit
    is now in the RED tier (≤14 days) must trigger a new alert.

    Uses a real datetime.date subclass as the proxy so isinstance() checks
    in the source code continue to work correctly.
    """
    # Fixed today: 2026-06-10. Close date 10 days away → red tier (≤14).
    fixed_today = datetime.date(2026, 6, 10)
    close_date_str = "2026-06-20"  # 10 days from fixed_today → red

    path = tmp_path / "accounts" / "acme" / "pursuits" / "escalating-deal.md"
    fm = _fm(stage="discovery", sf_close_date=close_date_str, meddpicc=_meddpicc())
    fm_map = {path: fm}

    # Prior state had yellow; now it's red → must NOT suppress
    prior_state = {"acme/escalating-deal": {"alerted_tier": "yellow"}}

    date_proxy = _make_date_proxy(fixed_today)
    patchers = _make_patches_branch_coverage(tmp_path, [path], fm_map, state=prior_state)
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        with (
            patch("fieldkit.watch.close_date_countdown.date", new=date_proxy),
            patch.object(cdc, "append_countdown_alert") as mock_alert,
        ):
            rc = cdc._run_countdown(**{**_KWARGS_BASE_branch_coverage, "account_filter": None})
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)

    assert rc == 0
    mock_alert.assert_called_once(), "Alert must fire when tier escalates from yellow to red"
    result = mock_alert.call_args[0][0]
    assert result["tier"] == "red"


def test_skips_non_matching_account_filter(tmp_path: Path) -> None:
    """Task 4.3: pursuit under 'globalpay' is skipped when account_filter='acme'.

    The pursuit is in the red tier but belongs to a different account.
    With account_filter='acme', it must be skipped entirely.
    """
    # Path under 'globalpay', not 'acme'
    path = tmp_path / "accounts" / "globalpay" / "pursuits" / "other-deal.md"
    # Use a real date string; date.today() is not patched here since the
    # account filter check happens before tier classification.
    close_date_str = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    fm = _fm(stage="discovery", sf_close_date=close_date_str, meddpicc=_meddpicc())
    fm_map = {path: fm}

    patchers = _make_patches_branch_coverage(tmp_path, [path], fm_map)
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        with patch.object(cdc, "append_countdown_alert") as mock_alert:
            # Filter to 'acme' only — globalpay pursuit must be skipped
            rc = cdc._run_countdown(**{**_KWARGS_BASE_branch_coverage, "account_filter": "acme"})
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)

    assert rc == 0
    mock_alert.assert_not_called(), "Pursuit from non-matching account must be skipped"


def test_handles_missing_close_date(tmp_path: Path) -> None:
    """Task 4.4: pursuit with sf_close_date=None is skipped without error.

    The watcher must log at DEBUG level and continue without raising.
    No alert must be written.
    """
    path = tmp_path / "accounts" / "acme" / "pursuits" / "no-close-date.md"
    fm = _fm(stage="discovery", sf_close_date=None)
    fm_map = {path: fm}

    patchers = _make_patches_branch_coverage(tmp_path, [path], fm_map)
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        with patch.object(cdc, "append_countdown_alert") as mock_alert:
            rc = cdc._run_countdown(**{**_KWARGS_BASE_branch_coverage, "account_filter": None})
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)

    assert rc == 0
    mock_alert.assert_not_called(), "Pursuit with no sf_close_date must be skipped without error"


def test_handles_closed_stage(tmp_path: Path) -> None:
    """Task 4.5: closed-won pursuit is skipped even when close date is in red tier.

    Terminal stages must be filtered before any tier classification.
    No date patching needed — the stage check happens before date parsing.
    """
    # Use a real near-term date; would be red tier if stage were active.
    close_date_str = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()

    path = tmp_path / "accounts" / "acme" / "pursuits" / "closed-deal.md"
    fm = _fm(stage="closed-won", sf_close_date=close_date_str, meddpicc=_meddpicc())
    fm_map = {path: fm}

    patchers = _make_patches_branch_coverage(tmp_path, [path], fm_map)
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        with patch.object(cdc, "append_countdown_alert") as mock_alert:
            rc = cdc._run_countdown(**{**_KWARGS_BASE_branch_coverage, "account_filter": None})
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)

    assert rc == 0
    mock_alert.assert_not_called(), "Closed-won pursuit must be skipped regardless of close date"


# ---------------------------------------------------------------------------
# Additional branch coverage for _run_countdown_inner (cc=46)
# ---------------------------------------------------------------------------


# ── TestRunCountdownInnerBranches (flattened) ─────────────────────────────────────────────

_KWARGS_BASE_run_countdown_inner_branches: dict[str, Any] = {
    "threshold_red": 14,
    "threshold_yellow": 30,
    "threshold_green": 60,
    "dry_run": False,
}


def _base_patches_run_countdown_inner_branches(
    tmp_path: Path, pursuits: list[Path], fm_map: dict[Path, Any]
) -> list[Any]:
    return [
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter(pursuits)),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm_map[p], "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
    ]


def test_empty_accounts_returns_exit_1(tmp_path: Path) -> None:
    """Lines 264-265: empty accounts dict → return 1 immediately."""
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([])),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
    ):
        rc = cdc._run_countdown_inner(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter=None,
            dry_run=False,
        )
    assert rc == 1


def test_unknown_account_filter_returns_exit_1(tmp_path: Path) -> None:
    """Lines 268-269: account_filter not in accounts → return 1."""
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([])),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
    ):
        rc = cdc._run_countdown_inner(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter="nonexistent-account",
            dry_run=False,
        )
    assert rc == 1


def test_path_without_pursuits_segment_is_skipped(tmp_path: Path) -> None:
    """A run that skips every discovered pursuit records fatal and exits 1."""
    # A path that has no 'pursuits' directory component
    bad_path = tmp_path / "flat-file.md"
    fm = _fm(
        stage="discovery",
        sf_close_date=(datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat(),
    )
    fm_map = {bad_path: fm}

    patchers = _base_patches_run_countdown_inner_branches(tmp_path, [bad_path], fm_map)
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        with patch.object(cdc, "append_countdown_alert") as mock_alert:
            rc = cdc._run_countdown_inner(
                threshold_red=14,
                threshold_yellow=30,
                threshold_green=60,
                account_filter=None,
                dry_run=False,
            )
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)

    assert rc == 1
    mock_alert.assert_not_called()


def test_load_pursuit_exception_is_skipped(tmp_path: Path) -> None:
    """An unreadable sole pursuit makes the completed run fatal."""
    path = tmp_path / "accounts" / "acme" / "pursuits" / "bad-pursuit.md"

    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", side_effect=OSError("disk error")),
        patch.object(cdc, "extract_champion_name", return_value="Jane"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        rc = cdc._run_countdown_inner(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter=None,
            dry_run=False,
        )
    assert rc == 1
    mock_alert.assert_not_called()


def test_unparseable_close_date_is_skipped(tmp_path: Path) -> None:
    """An invalid sole close date makes the completed run fatal."""
    path = tmp_path / "accounts" / "acme" / "pursuits" / "bad-date.md"
    fm = _fm(stage="discovery", sf_close_date="not-a-date-at-all")
    fm_map = {path: fm}

    patchers = _base_patches_run_countdown_inner_branches(tmp_path, [path], fm_map)
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        with patch.object(cdc, "append_countdown_alert") as mock_alert:
            rc = cdc._run_countdown_inner(
                threshold_red=14,
                threshold_yellow=30,
                threshold_green=60,
                account_filter=None,
                dry_run=False,
            )
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)

    assert rc == 1
    mock_alert.assert_not_called()


def test_out_of_range_tier_is_skipped(tmp_path: Path) -> None:
    """Lines 344-350: close date >60 days away → tier=None, pursuit skipped.

    Patches both the ``date`` and ``datetime`` module-level names in the
    production module so that both ``date.today()`` and
    ``datetime.now(tz=UTC).date()`` return the controlled fixed_today value.
    Previously xfail (historic regression) because only ``date`` was patched; fixed by
    adding ``_make_datetime_proxy`` which intercepts ``datetime.now()``.
    """
    fixed_today = datetime.date(2026, 6, 10)
    # 90 days from fixed_today → beyond green threshold (60)
    close_date_str = "2026-09-08"

    path = tmp_path / "accounts" / "acme" / "pursuits" / "far-future.md"
    fm = _fm(stage="discovery", sf_close_date=close_date_str, meddpicc=_meddpicc())
    fm_map = {path: fm}

    date_proxy = _make_date_proxy(fixed_today)
    datetime_proxy = _make_datetime_proxy(fixed_today)
    patchers = _base_patches_run_countdown_inner_branches(tmp_path, [path], fm_map)
    mocks = [p.__enter__() for p in patchers]  # noqa: F841
    try:
        with (
            patch("fieldkit.watch.close_date_countdown.date", new=date_proxy),
            patch("fieldkit.watch.close_date_countdown.datetime", new=datetime_proxy),
            patch.object(cdc, "append_countdown_alert") as mock_alert,
        ):
            rc = cdc._run_countdown_inner(
                threshold_red=14,
                threshold_yellow=30,
                threshold_green=60,
                account_filter=None,
                dry_run=False,
            )
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)

    assert rc == 0
    mock_alert.assert_not_called()


def test_enh134_next_steps_section_heading_fallback(tmp_path: Path) -> None:
    """Lines 383-385: implementation change — next_steps extracted from '## Next Steps' section."""
    fixed_today = datetime.date(2026, 6, 10)
    close_date_str = "2026-06-15"  # 5 days → red tier

    path = tmp_path / "accounts" / "acme" / "pursuits" / "section-steps.md"
    fm = _fm(stage="discovery", sf_close_date=close_date_str, sf_next_steps="", meddpicc=_meddpicc())
    # Body has a ## Next Steps heading with content beneath it
    body = (
        "## Overview\n\nSome content.\n\n## Next Steps\n\nSchedule architecture review with CTO\n\n## Risks\n\nNone\n"
    )
    fm_map = {path: fm}

    date_proxy = _make_date_proxy(fixed_today)
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm_map[p], body, datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
        patch("fieldkit.watch.close_date_countdown.date", new=date_proxy),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        rc = cdc._run_countdown_inner(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter=None,
            dry_run=False,
        )

    assert rc == 0
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    assert result["next_steps"] == "Schedule architecture review with CTO"


def test_enh134_next_steps_section_stops_at_next_heading(tmp_path: Path) -> None:
    """implementation change: scanning stops when a new ## heading is encountered after Next Steps."""
    fixed_today = datetime.date(2026, 6, 10)
    close_date_str = "2026-06-15"  # 5 days → red tier

    path = tmp_path / "accounts" / "acme" / "pursuits" / "section-stop.md"
    fm = _fm(stage="discovery", sf_close_date=close_date_str, sf_next_steps="", meddpicc=_meddpicc())
    # ## Next Steps section followed immediately by another heading (no content)
    body = "## Next Steps\n\n## Risks\n\nSome risk text\n"
    fm_map = {path: fm}

    date_proxy = _make_date_proxy(fixed_today)
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm_map[p], body, datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
        patch("fieldkit.watch.close_date_countdown.date", new=date_proxy),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        rc = cdc._run_countdown_inner(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter=None,
            dry_run=False,
        )

    assert rc == 0
    mock_alert.assert_called_once()
    # No content under Next Steps before next heading → falls back to "(none)"
    result = mock_alert.call_args[0][0]
    assert result["next_steps"] == "(none)"


# ---------------------------------------------------------------------------
# _load_state and _save_state direct coverage (lines 112-133)
# ---------------------------------------------------------------------------


# ── TestLoadSaveStateDirect (flattened) ─────────────────────────────────────────────


def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    """Lines 112-113: state file doesn't exist -> return {}."""
    state_file = tmp_path / "nonexistent.json"
    with patch.object(cdc, "_state_file", return_value=state_file):
        result = cdc._load_state()
    assert result == {}


def test_load_state_valid_file_returns_data(tmp_path: Path) -> None:
    """Lines 114-117: valid JSON file returns the dict."""
    import json as _json

    state_file = tmp_path / "state.json"
    data = {"acme/deal": {"alerted_tier": "red"}}
    state_file.write_text(_json.dumps(data), encoding="utf-8")
    with (
        patch.object(cdc, "_state_file", return_value=state_file),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path),
    ):
        result = cdc._load_state()
    assert result == data


def test_load_state_corrupt_file_returns_empty(tmp_path: Path) -> None:
    """Lines 118-120: corrupt JSON -> return {}."""
    state_file = tmp_path / "state.json"
    state_file.write_text("not valid json {{{", encoding="utf-8")
    with patch.object(cdc, "_state_file", return_value=state_file):
        result = cdc._load_state()
    assert result == {}


def test_load_state_non_dict_json_returns_empty(tmp_path: Path) -> None:
    """Line 117: JSON that is not a dict (e.g. list) -> return {}."""
    state_file = tmp_path / "state.json"
    state_file.write_text("[1, 2, 3]", encoding="utf-8")
    with patch.object(cdc, "_state_file", return_value=state_file):
        result = cdc._load_state()
    assert result == {}


def test_save_state_writes_and_replaces(tmp_path: Path) -> None:
    """Lines 125-130: _save_state writes atomically via tmp file."""
    import json as _json

    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()
    state_file = watchers_dir / "state.json"
    data = {"acme/deal": {"alerted_tier": "red"}}
    with (
        patch.object(cdc, "_state_file", return_value=state_file),
        patch.object(cdc, "get_watchers_dir", return_value=watchers_dir),
    ):
        result = cdc._save_state(data, previous_state={})
    assert result is None
    assert state_file.exists()
    loaded = _json.loads(state_file.read_text(encoding="utf-8"))
    assert loaded == data


def test_save_state_oserror_propagates(tmp_path: Path) -> None:
    """State persistence errors from the shared helper reach the caller."""
    state_file = tmp_path / "state.json"
    with (
        patch.object(cdc, "_state_file", return_value=state_file),
        patch.object(cdc, "merge_state", side_effect=OSError("disk full")),
        pytest.raises(OSError) as exc_info,
    ):
        cdc._save_state({"key": "value"}, previous_state={})
    assert exc_info.type is OSError


# ---------------------------------------------------------------------------
# _run_countdown_inner: rel_path ValueError branch and state-write failure
# ---------------------------------------------------------------------------


# ── TestRunCountdownInnerEdgeBranches (flattened) ─────────────────────────────────────────────

_KWARGS_BASE_run_countdown_inner_edge_branches: dict[str, Any] = {
    "threshold_red": 14,
    "threshold_yellow": 30,
    "threshold_green": 60,
    "dry_run": False,
}


def test_state_write_failure_is_fatal(tmp_path: Path) -> None:
    """A lost suppression state makes an otherwise successful run fatal."""
    fixed_today = datetime.date(2026, 6, 10)
    close_date_str = "2026-06-15"  # 5 days -> red tier
    path = tmp_path / "accounts" / "acme" / "pursuits" / "state-fail.md"
    fm = _fm(stage="discovery", sf_close_date=close_date_str, meddpicc=_meddpicc())
    fm_map = {path: fm}

    date_proxy = _make_date_proxy(fixed_today)
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm_map[p], "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", side_effect=OSError("disk full")),
        patch("fieldkit.watch.close_date_countdown.date", new=date_proxy),
        patch.object(cdc, "append_countdown_alert"),
    ):
        rc = cdc._run_countdown_inner(**{**_KWARGS_BASE_run_countdown_inner_edge_branches, "account_filter": None})

    assert rc == 1


def test_rel_path_value_error_uses_absolute(tmp_path: Path) -> None:
    """Lines 400-401: path.relative_to raises ValueError -> str(path) used instead."""
    import tempfile

    other_dir = Path(tempfile.mkdtemp())
    path = other_dir / "accounts" / "acme" / "pursuits" / "external-deal.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    fixed_today = datetime.date(2026, 6, 10)
    close_date_str = "2026-06-15"  # 5 days -> red tier
    fm = _fm(stage="discovery", sf_close_date=close_date_str, meddpicc=_meddpicc())
    fm_map = {path: fm}

    date_proxy = _make_date_proxy(fixed_today)
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(
            cdc,
            "load_pursuit",
            side_effect=lambda p: (fm_map[p], "", datetime.datetime(2026, 6, 6)),
        ),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status", return_value=None),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state", return_value=None),
        patch("fieldkit.watch.close_date_countdown.date", new=date_proxy),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        rc = cdc._run_countdown_inner(**{**_KWARGS_BASE_run_countdown_inner_edge_branches, "account_filter": None})

    assert rc == 0
    mock_alert.assert_called_once()
    result = mock_alert.call_args[0][0]
    # rel_path should be the absolute path string (not relative)
    assert str(path) in result["rel_path"]


# ---------------------------------------------------------------------------
# historic regression: re-alert after _RE_ALERT_DAYS days in same tier
# ---------------------------------------------------------------------------


# ── TestShouldSuppressCountdownReAlert (flattened) ─────────────────────────────────────────────


def test_same_tier_alerted_today_suppressed() -> None:
    """Same tier, alerted today → suppress."""
    state = {
        "acme/deal": {
            "alerted_tier": "red",
            "last_alerted_date": datetime.datetime.now(tz=datetime.UTC).date().isoformat(),
        }
    }
    assert cdc.should_suppress_countdown("acme/deal", "red", state) is True


def test_same_tier_alerted_8_days_ago_not_suppressed() -> None:
    """Same tier, last_alerted_date 8 days ago → re-alert (not suppressed)."""
    eight_days_ago = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=8)).isoformat()
    state = {
        "acme/deal": {
            "alerted_tier": "red",
            "last_alerted_date": eight_days_ago,
        }
    }
    assert cdc.should_suppress_countdown("acme/deal", "red", state) is False


def test_same_tier_no_last_alerted_date_suppressed() -> None:
    """Legacy state with no last_alerted_date → suppress (safe default)."""
    state = {
        "acme/deal": {
            "alerted_tier": "yellow",
        }
    }
    assert cdc.should_suppress_countdown("acme/deal", "yellow", state) is True


def test_tier_change_not_suppressed() -> None:
    """Tier change yellow→red → always emit (not suppressed)."""
    state = {
        "acme/deal": {
            "alerted_tier": "yellow",
            "last_alerted_date": datetime.datetime.now(tz=datetime.UTC).date().isoformat(),
        }
    }
    assert cdc.should_suppress_countdown("acme/deal", "red", state) is False


def test_no_prior_state_not_suppressed() -> None:
    """No prior state → first-time alert, not suppressed."""
    assert cdc.should_suppress_countdown("acme/deal", "red", {}) is False
