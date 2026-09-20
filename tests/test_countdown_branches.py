"""Branch-coverage tests for fieldkit.watch.close_date_countdown._run_countdown_inner
targeting the branches not yet hit by test_watch_close_date_countdown.py."""

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.watch import close_date_countdown as cdc

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers (mirrors test_watch_close_date_countdown.py conventions)
# ---------------------------------------------------------------------------


def _fm(
    stage: str = "discover",
    sf_close_date: Any = None,
    sf_next_steps: str = "",
    meddpicc: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        stage=stage,
        sf_close_date=sf_close_date,
        sf_next_steps=sf_next_steps,
        meddpicc=meddpicc,
    )


def _meddpicc(champion: int = 2, economic_buyer: int = 2, total: int = 16) -> SimpleNamespace:
    return SimpleNamespace(champion=champion, economic_buyer=economic_buyer, total=total)


_KWARGS: dict[str, Any] = {
    "threshold_red": 14,
    "threshold_yellow": 30,
    "threshold_green": 60,
    "account_filter": None,
    "dry_run": False,
}


def _patch_run(
    pursuits: list[Path],
    fm_map: dict[Path, Any],
    tmp_path: Path,
    *,
    state: dict[str, Any] | None = None,
    accounts_cfg: dict[str, Any] | None = None,
) -> list[Any]:
    return [
        patch.object(cdc, "get_accounts_config", return_value={"accounts": accounts_cfg or {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter(pursuits)),
        patch.object(cdc, "load_pursuit", side_effect=lambda p: (fm_map[p], "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value="Jane Doe"),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value=state or {}),
        patch.object(cdc, "_save_state"),
    ]


# ---------------------------------------------------------------------------
# Empty accounts config → exit 1
# ---------------------------------------------------------------------------


# ── TestEmptyAccounts (flattened) ───────────────────────────────────────────


def test_empty_accounts_empty_accounts_returns_1(tmp_path: Path) -> None:
    with patch.object(cdc, "get_accounts_config", return_value={"accounts": {}}):
        rc = cdc._run_countdown_inner(**_KWARGS)
    assert rc == 1


def test_empty_accounts_accounts_not_dict_returns_1(tmp_path: Path) -> None:
    with patch.object(cdc, "get_accounts_config", return_value={"accounts": "not-a-dict"}):
        rc = cdc._run_countdown_inner(**_KWARGS)
    assert rc == 1


# ---------------------------------------------------------------------------
# Account filter: unknown account → exit 1
# ---------------------------------------------------------------------------


# ── TestAccountFilterUnknown (flattened) ────────────────────────────────────


def test_account_filter_unknown_unknown_account_filter_returns_1(tmp_path: Path) -> None:
    with patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}):
        rc = cdc._run_countdown_inner(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter="nonexistent",
            dry_run=False,
        )
    assert rc == 1


def test_account_filter_unknown_known_account_filter_runs(tmp_path: Path) -> None:
    patchers = _patch_run([], {}, tmp_path)
    for p in patchers:
        p.__enter__()
    try:
        rc = cdc._run_countdown_inner(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter="acme",
            dry_run=False,
        )
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)
    assert rc == 0


# ---------------------------------------------------------------------------
# Pursuit with no pursuits_idx in path
# ---------------------------------------------------------------------------


# ── TestBadPursuitPath (flattened) ──────────────────────────────────────────


def test_bad_pursuit_path_makes_an_otherwise_empty_run_fatal(tmp_path: Path) -> None:
    """A malformed discovered path is a processing failure, not a benign skip."""
    path = tmp_path / "some" / "deal.md"
    fm = _fm(stage="discover")
    patchers = _patch_run([path], {path: fm}, tmp_path)
    for p in patchers:
        p.__enter__()
    try:
        rc = cdc._run_countdown_inner(**_KWARGS)
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)
    assert rc == 1


# ---------------------------------------------------------------------------
# Account filter excludes non-matching accounts
# ---------------------------------------------------------------------------


# ── TestAccountFilterExcludesOthers (flattened) ─────────────────────────────


def test_account_filter_excludes_others_non_matching_account_not_alerted(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "globalpay" / "pursuits" / "deal.md"
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=7)).isoformat()
    fm = _fm(stage="discover", sf_close_date=close, meddpicc=_meddpicc())

    alerted: list[Any] = []
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}, "globalpay": {}}}),
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
        patch.object(cdc, "append_countdown_alert", side_effect=lambda r, dr: alerted.append(r)),
    ):
        cdc._run_countdown_inner(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter="acme",  # only acme, not globalpay
            dry_run=False,
        )
    assert len(alerted) == 0


# ---------------------------------------------------------------------------
# Load failure skips the pursuit
# ---------------------------------------------------------------------------


# ── TestLoadFailureSkipped (flattened) ──────────────────────────────────────


def test_load_failure_makes_an_otherwise_empty_run_fatal(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "acme" / "pursuits" / "bad.md"

    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", side_effect=ValueError("bad yaml")),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert") as mock_alert,
    ):
        rc = cdc._run_countdown_inner(**_KWARGS)
    assert rc == 1
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Next-steps: ## Next Steps section heading fallback (implementation change)
# ---------------------------------------------------------------------------


# ── TestNextStepsSectionFallback (flattened) ────────────────────────────────


def test_next_steps_section_fallback_next_steps_section_heading_used(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "section-steps.md"
    fm = _fm(stage="discover", sf_close_date=close, sf_next_steps="", meddpicc=_meddpicc())
    body = "Some content\n\n## Next Steps\n\nSchedule architecture review with CTO\n\n## Other Section\n"

    captured: list[dict[str, Any]] = []

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
        patch.object(cdc, "append_countdown_alert", side_effect=lambda r, dr: captured.append(r)),
    ):
        cdc._run_countdown_inner(**_KWARGS)

    assert len(captured) == 1
    assert "architecture review" in captured[0]["next_steps"]


def test_next_steps_section_fallback_next_steps_none_when_no_body_content(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "no-steps.md"
    fm = _fm(stage="discover", sf_close_date=close, sf_next_steps="", meddpicc=_meddpicc())
    body = "Just a heading but no next steps content."

    captured: list[dict[str, Any]] = []

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
        patch.object(cdc, "append_countdown_alert", side_effect=lambda r, dr: captured.append(r)),
    ):
        cdc._run_countdown_inner(**_KWARGS)

    assert len(captured) == 1
    assert captured[0]["next_steps"] == "(none)"


# ---------------------------------------------------------------------------
# State save failure
# ---------------------------------------------------------------------------


# ── TestStateSaveFailure (flattened) ────────────────────────────────────────


def test_state_save_failure_is_fatal(tmp_path: Path) -> None:
    patchers = _patch_run([], {}, tmp_path)
    for p in patchers:
        p.__enter__()
    try:
        with patch.object(cdc, "_save_state", side_effect=OSError("disk full")):
            rc = cdc._run_countdown_inner(**_KWARGS)
    finally:
        for p in reversed(patchers):
            p.__exit__(None, None, None)
    assert rc == 1


# ---------------------------------------------------------------------------
# Out-of-range dates not alerted
# ---------------------------------------------------------------------------


# ── TestOutOfRangeDates (flattened) ─────────────────────────────────────────


def test_out_of_range_dates_far_future_date_not_alerted(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=200)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "far-future.md"
    fm = _fm(stage="discover", sf_close_date=close, meddpicc=_meddpicc())

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
        rc = cdc._run_countdown_inner(**_KWARGS)
    assert rc == 0
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# No MEDDPICC block (meddpicc is None)
# ---------------------------------------------------------------------------


# ── TestNoMeddpicc (flattened) ──────────────────────────────────────────────


def test_no_meddpicc_no_meddpicc_zero_score(tmp_path: Path) -> None:
    today = datetime.date.today()
    close = (today + datetime.timedelta(days=7)).isoformat()
    path = tmp_path / "accounts" / "acme" / "pursuits" / "no-meddpicc.md"
    fm = _fm(stage="discover", sf_close_date=close, meddpicc=None)

    captured: list[dict[str, Any]] = []
    with (
        patch.object(cdc, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.watch.close_date_countdown.get_fieldkit_home", return_value=tmp_path),
        patch.object(cdc, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(cdc, "_alerts_file", return_value=tmp_path / "watchers" / "alerts.md"),
        patch.object(cdc, "_state_file", return_value=tmp_path / "watchers" / "state.json"),
        patch.object(cdc, "iterate_pursuits", return_value=iter([path])),
        patch.object(cdc, "load_pursuit", return_value=(fm, "", datetime.datetime(2026, 6, 6))),
        patch.object(cdc, "extract_champion_name", return_value=None),
        patch.object(cdc, "write_run_status"),
        patch.object(cdc, "_load_state", return_value={}),
        patch.object(cdc, "_save_state"),
        patch.object(cdc, "append_countdown_alert", side_effect=lambda r, dr: captured.append(r)),
    ):
        cdc._run_countdown_inner(**_KWARGS)

    assert len(captured) == 1
    assert captured[0]["native_qualification"] == "unavailable (no Salesforce opportunity link)"
    assert "critical_gaps" not in captured[0]
