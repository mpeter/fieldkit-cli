"""Regression coverage for Phase B legacy qualification containment."""

import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.commands.brief.collect as brief_collect
import fieldkit.commands.pipeline.collect as pipeline_collect
import fieldkit.watch._pursuit_stall_render as stall_render
import fieldkit.watch._pursuit_stall_scan as stall_scan
import fieldkit.watch.close_date_countdown as countdown
from fieldkit.commands.pipeline.render import _build_narrative_prompt, render_pipeline_table

pytestmark = pytest.mark.unit


def _write_legacy_pursuit(path: Path, *, champion: int, sf_linked: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf_link = 'sf_opportunity_id: "006000000000001AAA"\n' if sf_linked else ""
    path.write_text(
        "---\n"
        "stage: discover\n"
        "gate-status: pass\n"
        "last-transition: 2026-01-01\n"
        f"{sf_link}"
        "meddpicc:\n"
        "  metrics: 3\n"
        "  economic-buyer: 3\n"
        "  decision-criteria: 3\n"
        "  decision-process: 3\n"
        "  identify-pain: 3\n"
        f"  champion: {champion}\n"
        "  competition: 3\n"
        "  paper-process: 3\n"
        "  composite: 24/24\n"
        "---\n\n# Pursuit\n",
        encoding="utf-8",
    )


def test_pipeline_excludes_legacy_scores_and_reports_native_fetch_pending(tmp_path: Path) -> None:
    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md"
    _write_legacy_pursuit(pursuit, champion=3)

    rows = pipeline_collect.collect_pursuit_health(tmp_path)
    table = render_pipeline_table(rows)

    assert rows[0].native_qualification == "pending (live ClosePlan fetch required)"
    assert rows[0].gate_status == "pending"
    assert "Native Qualification" in table
    assert "pending (live ClosePlan fetch required)" in table
    assert "MEDDPICC" not in table
    assert "/24" not in table
    assert "Biggest Gap" not in table


def test_pipeline_champion_signal_uses_derived_identity_not_legacy_score(tmp_path: Path) -> None:
    account_dir = tmp_path / "accounts" / "acme-corp"
    pursuit = account_dir / "pursuits" / "deal.md"
    _write_legacy_pursuit(pursuit, champion=0)
    (account_dir / "account.md").write_text("**Champion:** Jane Doe\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(
            pipeline_collect,
            "_query_champion_signals_inprocess",
            return_value=("2 (50%)", "2026-09-01", "INITIATOR"),
        ) as query:
            signal = pipeline_collect._maybe_collect_champion_signal(pursuit, "acme-corp", "deal", conn)
    finally:
        conn.close()

    assert signal == {
        "account": "acme-corp",
        "deal": "deal",
        "champion": "Jane",
        "initiation": "2 (50%)",
        "last_outbound": "2026-09-01",
        "signal": "INITIATOR",
    }
    query.assert_called_once_with(conn, "Jane")


def test_brief_champion_signal_ignores_historical_champion_zero(tmp_path: Path) -> None:
    account_dir = tmp_path / "accounts" / "acme-corp"
    pursuit = account_dir / "pursuits" / "deal.md"
    _write_legacy_pursuit(pursuit, champion=0)
    (account_dir / "account.md").write_text("**Champion:** Jane Doe\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(
            brief_collect,
            "query_champion_signals",
            return_value="  Threads initiated: 2\n  Last outbound: 2026-09-01\n  Signal: INITIATOR\n",
        ) as query:
            block = brief_collect._champion_signal_block(conn, tmp_path, "acme-corp", pursuit)
    finally:
        conn.close()

    assert block is not None
    assert "champion: Jane" in block
    query.assert_called_once_with(conn, "Jane")


def test_brief_ranking_and_output_ignore_historical_totals(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "acme-corp" / "pursuits"
    _write_legacy_pursuit(pursuits / "a-low.md", champion=0, sf_linked=False)
    _write_legacy_pursuit(pursuits / "z-high.md", champion=3, sf_linked=False)

    pulse = brief_collect.collect_pipeline_pulse(tmp_path)

    assert pulse.index("a-low") < pulse.index("z-high")
    assert "native qualification: unavailable" in pulse
    assert "MEDDPICC" not in pulse
    assert "/24" not in pulse


def test_pipeline_llm_input_contains_only_native_qualification_status() -> None:
    row = pipeline_collect.PursuitRow(
        account="acme-corp",
        deal="deal",
        stage="discover",
        gate_status="pending",
        days_in_stage=5,
        native_qualification="pending (live ClosePlan fetch required)",
    )

    prompt = _build_narrative_prompt([row], [], [], date(2026, 9, 12))

    assert "pending (live ClosePlan fetch required)" in prompt
    assert "MEDDPICC" not in prompt
    assert "/24" not in prompt


def test_countdown_alert_reports_native_unavailable_without_legacy_gaps(tmp_path: Path) -> None:
    result = {
        "account": "acme-corp",
        "pursuit": "deal",
        "tier": "red",
        "days_to_close": 5,
        "native_qualification": "pending (live ClosePlan fetch required)",
        "champion": "Jane Doe",
        "next_steps": "Schedule review",
        "rel_path": "accounts/acme-corp/pursuits/deal.md",
    }
    alerts = tmp_path / "countdown.md"
    with (
        patch.object(countdown, "_alerts_file", return_value=alerts),
        patch.object(countdown, "_ensure_alerts_header"),
        patch.object(countdown, "alert_block_exists", return_value=False),
    ):
        countdown.append_countdown_alert(result, dry_run=False)

    rendered = alerts.read_text(encoding="utf-8")
    assert "Native qualification:** pending (live ClosePlan fetch required)" in rendered
    assert "MEDDPICC" not in rendered
    assert "/24" not in rendered
    assert "Critical gaps" not in rendered


def test_stall_scan_and_alert_do_not_infer_legacy_gate_gap(tmp_path: Path) -> None:
    pursuit = tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md"
    _write_legacy_pursuit(pursuit, champion=0)
    alerts = tmp_path / "stalls.md"

    with patch.object(stall_scan, "get_fieldkit_home", return_value=tmp_path):
        result = stall_scan.scan_pursuit_file(pursuit, threshold_days=14, today=date(2026, 9, 12))

    assert result is not None
    assert result["native_qualification"] == "pending (live ClosePlan fetch required)"
    assert "gate_gap" not in result

    with (
        patch.object(stall_render, "_alerts_file", return_value=alerts),
        patch.object(stall_render, "_ensure_alerts_header"),
        patch.object(stall_render, "alert_block_exists", return_value=False),
    ):
        stall_render.append_stall_alert(result, dry_run=False)

    rendered = alerts.read_text(encoding="utf-8")
    assert "Native qualification:** pending (live ClosePlan fetch required)" in rendered
    assert "Gate gap" not in rendered
    assert "MEDDPICC" not in rendered
