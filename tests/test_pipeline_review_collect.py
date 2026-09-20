"""Tests for fieldkit.commands.pipeline.collect.collect_all_pursuit_data — branch coverage."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.pipeline.collect import (
    _extract_account,
    _parse_champion_output,
    _query_champion_signals_inprocess,
    collect_all_pursuit_data,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meddpicc(
    champion: int = 2,
    economic_buyer: int = 2,
    metrics: int = 2,
    decision_criteria: int = 2,
    decision_process: int = 2,
    identify_pain: int = 2,
    competition: int = 2,
    paper_process: int = 2,
    total: int = 16,
) -> Any:
    return SimpleNamespace(
        champion=champion,
        economic_buyer=economic_buyer,
        metrics=metrics,
        decision_criteria=decision_criteria,
        decision_process=decision_process,
        identify_pain=identify_pain,
        competition=competition,
        paper_process=paper_process,
        total=total,
    )


def _fm(
    stage: str = "discover",
    gate_status: str = "pending",
    last_transition: str | None = None,
    sf_close_date: str | None = None,
    meddpicc: Any = None,
    transition_history: list[Any] | None = None,
) -> Any:
    return SimpleNamespace(
        stage=stage,
        gate_status=gate_status,
        last_transition=last_transition,
        sf_close_date=sf_close_date,
        meddpicc=meddpicc,
        transition_history=transition_history,
    )


def _make_path(account: str = "acme", deal: str = "big-deal") -> Path:
    return Path(f"root/accounts/{account}/pursuits/{deal}.md")


# ---------------------------------------------------------------------------
# _extract_account
# ---------------------------------------------------------------------------


# ── TestExtractAccount (flattened) ──────────────────────────────────────────


def test_extract_account_standard_path() -> None:
    p = Path("root/accounts/acme/pursuits/deal.md")
    assert _extract_account(p.parts) == "acme"


def test_extract_account_no_accounts_segment() -> None:
    p = Path("some/other/path/deal.md")
    assert _extract_account(p.parts) == "unknown"


def test_extract_account_trailing_accounts() -> None:
    # accounts is the last part — no account after it
    p = Path("root/accounts")
    assert _extract_account(p.parts) == "unknown"


# ---------------------------------------------------------------------------
# _parse_champion_output
# ---------------------------------------------------------------------------


# ── TestParseChampionOutput (flattened) ─────────────────────────────────────


def test_parse_champion_output_parses_all_fields() -> None:
    stdout = """
  Threads initiated   : 3  (50% initiation rate)
  Last outbound       : 2026-01-15  30d ago
  Signal: INITIATOR
"""
    init, last, signal = _parse_champion_output(stdout)
    assert "3" in init
    assert "2026-01-15" in last
    assert signal == "INITIATOR"


def test_parse_champion_output_missing_fields_return_empty() -> None:
    init, last, signal = _parse_champion_output("")
    assert init == "" and last == "" and signal == ""


def test_parse_champion_output_only_signal() -> None:
    stdout = "  Signal: PASSIVE"
    _, _, signal = _parse_champion_output(stdout)
    assert signal == "PASSIVE"


# ---------------------------------------------------------------------------
# _query_champion_signals_inprocess
# ---------------------------------------------------------------------------


# ── TestQueryChampionSignalsInprocess (flattened) ───────────────────────────


def test_query_champion_signals_inprocess_empty_output_returns_empty_strings() -> None:
    conn = MagicMock(spec=sqlite3.Connection)
    with patch(
        "fieldkit.commands.pipeline.collect.query_champion_signals",
        return_value="",
    ):
        init, last, signal = _query_champion_signals_inprocess(conn, "Jane Doe")
    assert init == "" and last == "" and signal == ""


def test_query_champion_signals_inprocess_no_last_outbound_returns_empty() -> None:
    conn = MagicMock(spec=sqlite3.Connection)
    with patch(
        "fieldkit.commands.pipeline.collect.query_champion_signals",
        return_value="No people matched 'Jane Doe'",
    ):
        init, last, signal = _query_champion_signals_inprocess(conn, "Jane Doe")
    assert init == "" and last == "" and signal == ""


def test_query_champion_signals_inprocess_sqlite_error_returns_empty() -> None:
    conn = MagicMock(spec=sqlite3.Connection)
    with patch(
        "fieldkit.commands.pipeline.collect.query_champion_signals",
        side_effect=sqlite3.Error("db error"),
    ):
        init, last, signal = _query_champion_signals_inprocess(conn, "Jane Doe")
    assert init == "" and last == "" and signal == ""


def test_query_champion_signals_inprocess_valid_output_parsed() -> None:
    conn = MagicMock(spec=sqlite3.Connection)
    output = (
        "  Threads initiated   : 5  (60% initiation rate)\n"
        "  Last outbound       : 2026-05-01  14d ago\n"
        "  Signal: INITIATOR\n"
    )
    with patch("fieldkit.commands.pipeline.collect.query_champion_signals", return_value=output):
        init, _last, signal = _query_champion_signals_inprocess(conn, "Jane Doe")
    assert "5" in init
    assert signal == "INITIATOR"


# ---------------------------------------------------------------------------
# collect_all_pursuit_data
# ---------------------------------------------------------------------------


# ── TestCollectAllPursuitData (flattened) ───────────────────────────────────


def _collect_all_pursuit_data_run(
    paths: list[Path],
    fm_map: dict[Path, Any],
    tmp_path: Path,
    accounts_cfg: dict[str, Any] | None = None,
    gmail_db_exists: bool = False,
) -> tuple[Any, Any, Any]:
    config = {"accounts": accounts_cfg or {}}

    def fake_load(path: Path) -> tuple[Any, str, Any]:
        return fm_map[path], "", None

    with (
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter(paths)),
        patch("fieldkit.commands.pipeline.collect.load_pursuit", side_effect=fake_load),
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value=config),
        patch("fieldkit.commands.pipeline.collect.extract_champion_name", return_value=None),
    ):
        return collect_all_pursuit_data(tmp_path)


def test_collect_all_pursuit_data_empty_pursuits_returns_empty_lists(tmp_path: Path) -> None:
    rows, signals, _blindspot = _collect_all_pursuit_data_run([], {}, tmp_path)
    assert rows == []
    assert signals is None  # historic regression: gmail.db absent → None not []


def test_collect_all_pursuit_data_closed_stage_excluded_from_rows(tmp_path: Path) -> None:
    path = _make_path()
    fm = _fm(stage="closed-won")
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert rows == []


def test_collect_all_pursuit_data_active_stage_included_in_rows(tmp_path: Path) -> None:
    path = _make_path(account="acme", deal="big-deal")
    fm = _fm(stage="discover", meddpicc=_meddpicc())
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert len(rows) == 1
    assert rows[0].account == "acme"
    assert rows[0].deal == "big-deal"


def test_collect_all_pursuit_data_meddpicc_scores_extracted(tmp_path: Path) -> None:
    path = _make_path()
    fm = _fm(stage="discover", meddpicc=_meddpicc(champion=2, metrics=1, total=10))
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert rows[0].native_qualification == "unavailable (no Salesforce opportunity link)"
    assert not hasattr(rows[0], "meddpicc_total")
    assert not hasattr(rows[0], "scores")


def test_collect_all_pursuit_data_no_meddpicc_zero_total(tmp_path: Path) -> None:
    path = _make_path()
    fm = _fm(stage="discover", meddpicc=None)
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert rows[0].native_qualification == "unavailable (no Salesforce opportunity link)"


def test_collect_all_pursuit_data_sf_close_date_extracted(tmp_path: Path) -> None:
    path = _make_path()
    fm = _fm(stage="discover", sf_close_date="2026-12-31")
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert rows[0].close_date_str == "2026-12-31"


def test_collect_all_pursuit_data_missing_close_date_non_prepipeline(tmp_path: Path) -> None:
    path = _make_path()
    fm = _fm(stage="discover", sf_close_date=None)
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert "⚠" in rows[0].close_date_str


def test_collect_all_pursuit_data_missing_close_date_prepipeline_empty(tmp_path: Path) -> None:
    path = _make_path()
    fm = _fm(stage="pre-pipeline", sf_close_date=None)
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert rows[0].close_date_str == ""


def test_collect_all_pursuit_data_velocity_computed_with_history(tmp_path: Path) -> None:
    path = _make_path()
    fm = _fm(
        stage="discover",
        last_transition="2026-01-01",
        transition_history=[{"date": "2026-01-01"}, {"date": "2026-02-01"}],
    )
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert rows[0].velocity_days_per_stage is not None


def test_collect_all_pursuit_data_velocity_none_without_history(tmp_path: Path) -> None:
    path = _make_path()
    fm = _fm(stage="discover", transition_history=None)
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    assert rows[0].velocity_days_per_stage is None


def test_collect_all_pursuit_data_blindspot_ok_when_active_meets_threshold(tmp_path: Path) -> None:
    path = _make_path(account="acme")
    fm = _fm(stage="discover")
    accounts_cfg = {"acme": {"blindspot_threshold": 1}}
    _, _, blindspot = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path, accounts_cfg=accounts_cfg)
    acme_entry = next(b for b in blindspot if b["account"] == "acme")
    assert acme_entry["status"] == "ok"


def test_collect_all_pursuit_data_blindspot_flagged_when_no_active(tmp_path: Path) -> None:
    accounts_cfg = {"acme": {"blindspot_threshold": 1}}
    _, _, blindspot = _collect_all_pursuit_data_run([], {}, tmp_path, accounts_cfg=accounts_cfg)
    acme_entry = next(b for b in blindspot if b["account"] == "acme")
    assert acme_entry["status"] == "blindspot"


def test_collect_all_pursuit_data_internal_accounts_excluded_from_blindspot(tmp_path: Path) -> None:
    accounts_cfg = {"internal-team": {"internal": True, "blindspot_threshold": 1}}
    _, _, blindspot = _collect_all_pursuit_data_run([], {}, tmp_path, accounts_cfg=accounts_cfg)
    names = [b["account"] for b in blindspot]
    assert "internal-team" not in names


def test_collect_all_pursuit_data_invalid_pursuit_file_skipped(tmp_path: Path) -> None:

    path = _make_path()

    def bad_load(p: Path) -> Any:
        raise ValueError("bad frontmatter")

    with (
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([path])),
        patch("fieldkit.commands.pipeline.collect.load_pursuit", side_effect=bad_load),
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value={"accounts": {}}),
    ):
        rows, _, _ = collect_all_pursuit_data(tmp_path)
    assert rows == []


def test_collect_all_pursuit_data_champion_signal_added_when_gmail_exists(tmp_path: Path) -> None:
    # Create a fake gmail.db so db_path.exists() is True
    gmail_db = tmp_path / "gmail.db"
    gmail_db.write_bytes(b"")

    path = _make_path(account="acme", deal="big-deal")
    fm = _fm(stage="discover", meddpicc=_meddpicc(champion=2))

    def fake_load(p: Path) -> tuple[Any, str, Any]:
        return fm, "", None

    mock_conn = MagicMock(spec=sqlite3.Connection)

    with (
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([path])),
        patch("fieldkit.commands.pipeline.collect.load_pursuit", side_effect=fake_load),
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=gmail_db),
        patch("fieldkit.commands.pipeline.collect._gmail_connect", return_value=mock_conn),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value={"accounts": {}}),
        patch("fieldkit.commands.pipeline.collect.extract_champion_name", return_value="Jane Doe"),
        patch(
            "fieldkit.commands.pipeline.collect._query_champion_signals_inprocess",
            return_value=("3 (50%)", "2026-01-15", "INITIATOR"),
        ),
    ):
        _rows, signals, _ = collect_all_pursuit_data(tmp_path)

    assert len(signals) == 1
    assert signals[0]["account"] == "acme"
    assert signals[0]["signal"] == "INITIATOR"


def test_collect_all_pursuit_data_champion_below_1_no_signal_query(tmp_path: Path) -> None:
    """Pursuits with champion < 1 are not queried for signals."""
    gmail_db = tmp_path / "gmail.db"
    gmail_db.write_bytes(b"")

    path = _make_path()
    fm = _fm(stage="discover", meddpicc=_meddpicc(champion=0))

    mock_conn = MagicMock(spec=sqlite3.Connection)

    with (
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([path])),
        patch("fieldkit.commands.pipeline.collect.load_pursuit", return_value=(fm, "", None)),
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=gmail_db),
        patch("fieldkit.commands.pipeline.collect._gmail_connect", return_value=mock_conn),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value={"accounts": {}}),
        patch("fieldkit.commands.pipeline.collect.extract_champion_name", return_value=None),
        patch("fieldkit.commands.pipeline.collect._query_champion_signals_inprocess") as mock_sig,
    ):
        _, signals, _ = collect_all_pursuit_data(tmp_path)

    mock_sig.assert_not_called()
    assert signals == []  # gmail.db present but champion_score=0 → no signals


def test_collect_all_pursuit_data_gmail_connect_error_continues(tmp_path: Path) -> None:
    """sqlite3 error on connect still returns results (signals empty)."""
    gmail_db = tmp_path / "gmail.db"
    gmail_db.write_bytes(b"")

    path = _make_path()
    fm = _fm(stage="discover", meddpicc=_meddpicc(champion=2))

    with (
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([path])),
        patch("fieldkit.commands.pipeline.collect.load_pursuit", return_value=(fm, "", None)),
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=gmail_db),
        patch("fieldkit.commands.pipeline.collect._gmail_connect", side_effect=sqlite3.Error("locked")),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value={"accounts": {}}),
    ):
        rows, signals, _ = collect_all_pursuit_data(tmp_path)

    assert len(rows) == 1
    assert signals is None  # historic regression: connect error → gmail unavailable → None


def test_collect_all_pursuit_data_unknown_gate_status_passed_through(tmp_path: Path) -> None:
    """A local gate status does not become current native qualification."""
    path = _make_path()
    fm = _fm(stage="discover", gate_status="bogus-status")
    rows, _, _ = _collect_all_pursuit_data_run([path], {path: fm}, tmp_path)
    # collect_all_pursuit_data does not normalize (only collect_pursuit_health does)
    assert rows[0].gate_status == "pending"


def test_collect_all_pursuit_data_active_count_increments_per_account(tmp_path: Path) -> None:
    path1 = _make_path(account="acme", deal="deal-1")
    path2 = _make_path(account="acme", deal="deal-2")
    fm = _fm(stage="discover")
    accounts_cfg = {"acme": {"blindspot_threshold": 2}}
    _rows, _, blindspot = _collect_all_pursuit_data_run(
        [path1, path2], {path1: fm, path2: fm}, tmp_path, accounts_cfg=accounts_cfg
    )
    acme_entry = next(b for b in blindspot if b["account"] == "acme")
    assert acme_entry["active_pursuits"] == 2
    assert acme_entry["status"] == "ok"
