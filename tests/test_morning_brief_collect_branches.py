"""Tests for morning_brief/collect.py — covering uncovered branches.

Targets:
- collect_pursuit_alerts: account_filter, gate_status=fail, stuck>14d, closed stages
- collect_pipeline_pulse: M/D/YYYY date, no close date, account_filter, MEDDPICC sort
- _champion_signal_block: closed pursuit, no champion MEDDPICC, signal filtering
- collect_decay_signals: no accounts.yaml, invalid acct_data, account_filter
"""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.commands.brief.collect as collect_mod
from fieldkit.commands.brief.collect import (
    _champion_signal_block,
    collect_decay_signals,
    collect_pipeline_pulse,
    collect_pursuit_alerts,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pursuit(
    path: Path,
    stage: str = "propose",
    extra_frontmatter: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstage: {stage}\n{extra_frontmatter}---\n# Pursuit\n",
        encoding="utf-8",
    )


def _pursuits_dir(data_root: Path, account: str = "acme-corp") -> Path:
    d = data_root / "accounts" / account / "pursuits"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ===========================================================================
# collect_pursuit_alerts — uncovered branches
# ===========================================================================


# ── TestCollectPursuitAlertsGateStatus (flattened) ──────────────────────────


def test_collect_pursuit_alerts_gate_fail_alert_surfaced(tmp_path: Path) -> None:
    """A stored local failure does not become a current qualification alert."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(
        pursuits / "gated-deal.md",
        stage="propose",
        extra_frontmatter="gate_status: fail\n",
    )

    result = collect_pursuit_alerts(tmp_path)

    assert "Gate status: FAIL" not in result
    assert "Native qualification" in result
    assert "gated-deal" in result


def test_collect_pursuit_alerts_gate_pass_no_alert(tmp_path: Path) -> None:
    """A pursuit with gate_status != fail does not produce a gate alert."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(
        pursuits / "open-deal.md",
        stage="propose",
        extra_frontmatter="gate_status: pass\n",
    )

    result = collect_pursuit_alerts(tmp_path)

    # No gate alert, but stall may surface (no last_transition)
    assert "Gate status: FAIL" not in result


# ── TestCollectPursuitAlertsAccountFilter (flattened) ───────────────────────


def test_collect_pursuit_alerts_account_filter_excludes_other_accounts(tmp_path: Path) -> None:
    """account_filter='acme-corp' excludes pursuits from other accounts."""
    _pursuits_dir(tmp_path, "acme-corp")
    _pursuits_dir(tmp_path, "globalpay")

    _write_pursuit(tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal-a.md", stage="propose")
    _write_pursuit(tmp_path / "accounts" / "globalpay" / "pursuits" / "deal-b.md", stage="propose")

    result = collect_pursuit_alerts(tmp_path, account_filter="acme-corp")

    assert "acme-corp" in result or result == "No active pursuits flagged today."
    assert "globalpay" not in result


def test_collect_pursuit_alerts_account_filter_case_insensitive(tmp_path: Path) -> None:
    """account_filter is case-insensitive."""
    _pursuits_dir(tmp_path, "acme-corp")
    _write_pursuit(tmp_path / "accounts" / "acme-corp" / "pursuits" / "deal.md", stage="propose")

    result_lower = collect_pursuit_alerts(tmp_path, account_filter="acme-corp")
    result_upper = collect_pursuit_alerts(tmp_path, account_filter="ACME-CORP")

    assert result_lower == result_upper


# ── TestCollectPursuitAlertsClosedStages (flattened) ────────────────────────


def test_collect_pursuit_alerts_closed_pursuit_not_surfaced(tmp_path: Path) -> None:
    """Pursuits in closed stages are not included in alerts."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(pursuits / "closed-deal.md", stage="closed-won")

    result = collect_pursuit_alerts(tmp_path)

    assert result == "No active pursuits flagged today."


def test_collect_pursuit_alerts_stuck_pursuit_with_last_transition(tmp_path: Path) -> None:
    """A pursuit stuck >14 days surfaces the stall alert."""
    pursuits = _pursuits_dir(tmp_path)
    # Use a very old last_transition date
    _write_pursuit(
        pursuits / "stuck-deal.md",
        stage="propose",
        extra_frontmatter="last_transition: 2020-01-01\n",
    )

    result = collect_pursuit_alerts(tmp_path)

    assert "⏱" in result
    assert "stuck-deal" in result


# ===========================================================================
# collect_pipeline_pulse — uncovered branches
# ===========================================================================


# ── TestCollectPipelinePulseDateFormats (flattened) ─────────────────────────


def test_collect_pipeline_pulse_slash_date_format_parsed_correctly(tmp_path: Path) -> None:
    """M/D/YYYY date format is parsed and used for RED classification."""
    pursuits = _pursuits_dir(tmp_path)
    fake_today = date(2026, 6, 14)

    # 5 days from fake_today → RED (6/19/2026 is 5 days from 6/14/2026)
    _write_pursuit(
        pursuits / "slash-date.md",
        stage="propose",
        extra_frontmatter="sf_close_date: 6/19/2026\n",
    )

    # Freeze the symbol collect.py actually reads: datetime.now(tz=UTC).date().
    # Patching `date` here would be inert (historic regression).
    with patch.object(collect_mod, "datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(fake_today.year, fake_today.month, fake_today.day, tzinfo=UTC)
        result = collect_pipeline_pulse(tmp_path)

    assert "🔴" in result
    assert "slash-date" in result


def test_collect_pipeline_pulse_no_close_date_not_red(tmp_path: Path) -> None:
    """Pursuits with no close date are not marked RED."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(pursuits / "no-date.md", stage="propose")

    result = collect_pipeline_pulse(tmp_path)

    assert "🔴" not in result


def test_collect_pipeline_pulse_future_close_date_not_red(tmp_path: Path) -> None:
    """Close date >30 days in future is not marked RED."""
    pursuits = _pursuits_dir(tmp_path)
    fake_today = date(2026, 6, 14)

    _write_pursuit(
        pursuits / "future-deal.md",
        stage="propose",
        extra_frontmatter="sf_close_date: 2026-09-01\n",
    )

    with patch.object(collect_mod, "datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(fake_today.year, fake_today.month, fake_today.day, tzinfo=UTC)
        result = collect_pipeline_pulse(tmp_path)

    assert "🔴" not in result


def test_collect_pipeline_pulse_meddpicc_sort_higher_score_first(tmp_path: Path) -> None:
    """Higher MEDDPICC score appears earlier in the pulse output."""
    pursuits = _pursuits_dir(tmp_path)

    # deal-a: low MEDDPICC
    _write_pursuit(
        pursuits / "deal-low.md",
        stage="propose",
        extra_frontmatter=(
            "meddpicc:\n"
            "  metrics: 0\n"
            "  economic-buyer: 0\n"
            "  decision-criteria: 0\n"
            "  decision-process: 0\n"
            "  identify-pain: 0\n"
            "  champion: 0\n"
            "  competition: 0\n"
            "  paper-process: 0\n"
        ),
    )
    # deal-b: high MEDDPICC
    _write_pursuit(
        pursuits / "deal-high.md",
        stage="propose",
        extra_frontmatter=(
            "meddpicc:\n"
            "  metrics: 3\n"
            "  economic-buyer: 3\n"
            "  decision-criteria: 3\n"
            "  decision-process: 3\n"
            "  identify-pain: 3\n"
            "  champion: 3\n"
            "  competition: 3\n"
            "  paper-process: 3\n"
        ),
    )

    result = collect_pipeline_pulse(tmp_path)

    # deal-high must appear before deal-low (both non-RED, sort by -MEDDPICC)
    high_pos = result.find("deal-high")
    low_pos = result.find("deal-low")
    assert high_pos < low_pos, f"High MEDDPICC must come first; high={high_pos}, low={low_pos}"


# ===========================================================================
# _champion_signal_block — uncovered branches
# ===========================================================================


# ── TestChampionSignalBlock (flattened) ─────────────────────────────────────


def test_champion_signal_block_closed_pursuit_returns_none(tmp_path: Path) -> None:
    """_champion_signal_block returns None for a closed pursuit."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(pursuits / "done-deal.md", stage="closed-won")

    conn = sqlite3.connect(":memory:")
    try:
        result = _champion_signal_block(conn, tmp_path, "acme-corp", pursuits / "done-deal.md")
    finally:
        conn.close()

    assert result is None


def test_champion_signal_block_no_champion_meddpicc_returns_none(tmp_path: Path) -> None:
    """Returns None when MEDDPICC champion=0 (no champion identified)."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(
        pursuits / "no-champ.md",
        stage="propose",
        extra_frontmatter=(
            "meddpicc:\n"
            "  metrics: 1\n"
            "  economic-buyer: 1\n"
            "  decision-criteria: 1\n"
            "  decision-process: 1\n"
            "  identify-pain: 1\n"
            "  champion: 0\n"
            "  competition: 1\n"
            "  paper-process: 1\n"
        ),
    )

    conn = sqlite3.connect(":memory:")
    try:
        result = _champion_signal_block(conn, tmp_path, "acme-corp", pursuits / "no-champ.md")
    finally:
        conn.close()

    assert result is None


def test_champion_signal_block_no_meddpicc_at_all_returns_none(tmp_path: Path) -> None:
    """Returns None when there is no meddpicc block at all."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(pursuits / "no-meddpicc.md", stage="propose")

    conn = sqlite3.connect(":memory:")
    try:
        result = _champion_signal_block(conn, tmp_path, "acme-corp", pursuits / "no-meddpicc.md")
    finally:
        conn.close()

    assert result is None


def test_champion_signal_block_db_error_returns_none(tmp_path: Path) -> None:
    """Returns None when query_champion_signals raises a DB error."""
    pursuits = _pursuits_dir(tmp_path, "acme-corp")
    account_dir = tmp_path / "accounts" / "acme-corp"

    # Write account.md with a champion name
    (account_dir / "account.md").write_text("**Champion:** Jane Doe\n", encoding="utf-8")

    _write_pursuit(
        pursuits / "deal.md",
        stage="propose",
        extra_frontmatter=(
            "meddpicc:\n"
            "  metrics: 1\n"
            "  economic-buyer: 1\n"
            "  decision-criteria: 1\n"
            "  decision-process: 1\n"
            "  identify-pain: 1\n"
            "  champion: 2\n"
            "  competition: 1\n"
            "  paper-process: 1\n"
        ),
    )

    conn = sqlite3.connect(":memory:")
    try:
        with patch.object(
            collect_mod,
            "query_champion_signals",
            side_effect=sqlite3.OperationalError("no such table: threads"),
        ):
            result = _champion_signal_block(conn, tmp_path, "acme-corp", pursuits / "deal.md")
    finally:
        conn.close()

    assert result is None


# ===========================================================================
# collect_decay_signals — uncovered branches
# ===========================================================================


# ── TestCollectDecaySignals (flattened) ─────────────────────────────────────


def test_collect_decay_signals_no_accounts_yaml_returns_sentinel(tmp_path: Path) -> None:
    """collect_decay_signals returns the unavailable sentinel when accounts.yaml is absent."""
    (tmp_path / "accounts").mkdir()

    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "_gmail_connect"),
        patch.object(
            collect_mod,
            "read_accounts_config",
            side_effect=FileNotFoundError("no such file"),
        ),
    ):
        result = collect_decay_signals(tmp_path)

    assert "accounts.yaml not found" in result
    assert "unavailable" in result


def test_collect_decay_signals_account_filter_excludes_other_accounts(tmp_path: Path) -> None:
    """account_filter limits decay reports to the matching account only."""
    (tmp_path / "accounts").mkdir()

    mock_config = {
        "accounts": {
            "acme-corp": {"domain": "acme-corp.com"},
            "globalpay": {"domain": "globalpay.com"},
        }
    }

    called_for: list[str] = []

    def fake_decay_report(conn, account, **kwargs):
        called_for.append(account)

    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "read_accounts_config", return_value=mock_config),
        patch.object(collect_mod, "_gmail_connect", return_value=MagicMock()),
        patch.object(collect_mod, "decay_report", side_effect=fake_decay_report),
    ):
        # Patch conn.close so MagicMock context manager doesn't fail
        collect_decay_signals(tmp_path, account_filter="acme-corp")

    assert called_for == ["acme-corp"] or "acme-corp" in called_for
    assert "globalpay" not in called_for


def test_collect_decay_signals_db_connection_error_returns_sentinel(tmp_path: Path) -> None:
    """Returns the unavailable sentinel when gmail.db connection fails."""
    mock_config = {"accounts": {"acme-corp": {"domain": "acme-corp.com"}}}

    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "read_accounts_config", return_value=mock_config),
        patch.object(
            collect_mod,
            "_gmail_connect",
            side_effect=sqlite3.OperationalError("unable to open database"),
        ),
    ):
        result = collect_decay_signals(tmp_path)

    assert "unavailable" in result


def test_collect_decay_signals_non_dict_account_data_skipped(tmp_path: Path) -> None:
    """Account entries that are not dicts are silently skipped."""
    mock_config = {
        "accounts": {
            "bad-entry": "not-a-dict",
            "acme-corp": {"domain": "acme-corp.com"},
        }
    }

    called_for: list[str] = []

    def fake_decay_report(conn, account, **kwargs):
        called_for.append(account)

    with (
        patch.object(collect_mod, "_gmail_db_exists", return_value=True),
        patch.object(collect_mod, "read_accounts_config", return_value=mock_config),
        patch.object(collect_mod, "_gmail_connect", return_value=MagicMock()),
        patch.object(collect_mod, "decay_report", side_effect=fake_decay_report),
    ):
        collect_decay_signals(tmp_path)

    assert "bad-entry" not in called_for


# ===========================================================================
# collect_pursuit_alerts — MEDDPICC champion/economic_buyer branches
# ===========================================================================


# ── TestCollectPursuitAlertsMeddpicc (flattened) ────────────────────────────


def test_collect_pursuit_alerts_champion_zero_surfaces_alert(tmp_path: Path) -> None:
    """Pursuit with MEDDPICC champion=0 surfaces the champion alert."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(
        pursuits / "no-champion.md",
        stage="propose",
        extra_frontmatter=(
            "meddpicc:\n"
            "  champion: 0\n"
            "  economic-buyer: 2\n"
            "  metrics: 1\n"
            "  identify-pain: 1\n"
            "  decision-criteria: 1\n"
            "  decision-process: 1\n"
            "  paper-process: 1\n"
            "  competition: 1\n"
            "  composite: 8/24\n"
        ),
    )

    result = collect_pursuit_alerts(tmp_path)

    assert "Champion" in result or "champion" in result.lower()


def test_collect_pursuit_alerts_economic_buyer_zero_surfaces_alert(tmp_path: Path) -> None:
    """A historical buyer value does not infer a current native gap."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(
        pursuits / "no-buyer.md",
        stage="propose",
        extra_frontmatter=(
            "meddpicc:\n"
            "  champion: 2\n"
            "  economic-buyer: 0\n"
            "  metrics: 1\n"
            "  identify-pain: 1\n"
            "  decision-criteria: 1\n"
            "  decision-process: 1\n"
            "  paper-process: 1\n"
            "  competition: 1\n"
            "  composite: 8/24\n"
        ),
    )

    result = collect_pursuit_alerts(tmp_path)

    assert "Economic Buyer" not in result
    assert "native qualification" in result.lower()


def test_collect_pursuit_alerts_no_last_transition_surfaces_unknown_stall(tmp_path: Path) -> None:
    """Pursuit with no last_transition surfaces the unknown stall alert."""
    pursuits = _pursuits_dir(tmp_path)
    _write_pursuit(
        pursuits / "no-transition.md",
        stage="propose",
    )

    result = collect_pursuit_alerts(tmp_path)

    assert "stall duration unknown" in result or "No last_transition" in result or "⏱" in result


def test_collect_pursuit_alerts_invalid_pursuit_file_skipped(tmp_path: Path) -> None:
    """Invalid pursuit files (YAML error) are skipped without crashing."""
    pursuits = _pursuits_dir(tmp_path)
    bad_file = pursuits / "bad.md"
    bad_file.write_text("---\n: invalid: yaml: :\n---\n# Body\n", encoding="utf-8")

    # Should not raise
    result = collect_pursuit_alerts(tmp_path)
    assert isinstance(result, str)
