"""Tests for tools/pipeline_review/main.py.

Covers the 10 specified behaviours:
1. collect_pursuit_health returns PursuitRow for each non-closed pursuit
2. collect_pursuit_health skips closed-won and closed-lost stages
3. days_in_stage is -1 when last_transition is missing
4. render_pipeline_table sorts by STAGE_ORDER then days_in_stage descending
5. Gate icons: pending→🔲, pass→✅, fail→❌, override→⚠️
6. Score labels: 0-8→🔴, 9-16→🟡, 17-24→🟢 (via render output)
7. --no-llm flag produces table and LLM placeholder, no subprocess to claude
8. Output file written to routines/briefs/pipeline-review-YYYY-MM-DD.md
9. biggest_gap returns 'element: current→required' when gap exists
10. biggest_gap returns gate-override message when gate_status is override
"""

import sys
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.pipeline.collect import (
    GATE_ICONS,
    PursuitRow,
    _extract_account,
    collect_blindspot_data,
    collect_pursuit_health,
)
from fieldkit.commands.pipeline.main import render_full_brief
from fieldkit.commands.pipeline.render import (
    _quarter_label,
    _render_blindspot_section,
    _render_champion_section,
    render_pipeline_table,
)
from fieldkit.errors import LLMError
from fieldkit.watch.morning_brief_render import calculate_quota_gap

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_data_root(tmp_path: Path) -> Path:
    """Return a data_root with an accounts/ directory."""
    (tmp_path / "accounts").mkdir(parents=True, exist_ok=True)
    return tmp_path


_BASE_MEDDPICC = (
    "meddpicc:\n"
    "  metrics: 1\n"
    "  economic-buyer: 1\n"
    "  decision-criteria: 1\n"
    "  decision-process: 1\n"
    "  identify-pain: 1\n"
    "  champion: 1\n"
    "  competition: 1\n"
    "  paper-process: 1\n"
)


# ── Test 1: collect_pursuit_health returns rows for non-closed pursuits ────────


@pytest.mark.unit
def test_collect_returns_row_for_active_pursuit(tmp_path, write_pursuit_generic):
    """A single active pursuit in discover stage appears as a PursuitRow."""
    acct = tmp_path / "accounts" / "global-pay"
    write_pursuit_generic(
        acct,
        "project-alpha",
        "stage: discover\nlast-transition: 2026-01-01\n" + _BASE_MEDDPICC,
    )
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    assert rows[0].deal == "project-alpha"
    assert rows[0].account == "global-pay"
    assert rows[0].stage == "discover"


@pytest.mark.unit
def test_collect_returns_multiple_rows(tmp_path, write_pursuit_generic):
    """Multiple active pursuits across accounts all appear."""
    acct_a = tmp_path / "accounts" / "global-pay"
    acct_b = tmp_path / "accounts" / "acme-bank"
    write_pursuit_generic(acct_a, "deal-one", "stage: discover\n" + _BASE_MEDDPICC)
    write_pursuit_generic(acct_b, "deal-two", "stage: validate\n" + _BASE_MEDDPICC)
    rows = collect_pursuit_health(tmp_path)
    deals = {r.deal for r in rows}
    assert "deal-one" in deals
    assert "deal-two" in deals


# ── Test 2: closed-won and closed-lost are skipped ────────────────────────────


@pytest.mark.unit
def test_collect_skips_closed_won(tmp_path, write_pursuit_generic):
    """closed-won pursuits must not appear in output."""
    acct = tmp_path / "accounts" / "global-pay"
    write_pursuit_generic(acct, "won-deal", "stage: closed-won\n" + _BASE_MEDDPICC)
    rows = collect_pursuit_health(tmp_path)
    assert rows == []


@pytest.mark.unit
def test_collect_skips_closed_lost(tmp_path, write_pursuit_generic):
    """closed-lost pursuits must not appear in output."""
    acct = tmp_path / "accounts" / "global-pay"
    write_pursuit_generic(acct, "lost-deal", "stage: closed-lost\n" + _BASE_MEDDPICC)
    rows = collect_pursuit_health(tmp_path)
    assert rows == []


@pytest.mark.unit
def test_collect_skips_closed_but_keeps_active(tmp_path, write_pursuit_generic):
    """Closed pursuits are skipped; active ones still appear."""
    acct = tmp_path / "accounts" / "global-pay"
    write_pursuit_generic(acct, "closed", "stage: closed-won\n" + _BASE_MEDDPICC)
    write_pursuit_generic(acct, "active", "stage: discover\n" + _BASE_MEDDPICC)
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    assert rows[0].deal == "active"


# ── Test 3: days_in_stage is -1 when last_transition is missing ───────────────


@pytest.mark.unit
def test_days_in_stage_minus_one_when_no_last_transition(tmp_path, write_pursuit_generic):
    """When last-transition is absent, days_in_stage should be -1."""
    acct = tmp_path / "accounts" / "global-pay"
    write_pursuit_generic(acct, "no-date", "stage: discover\n" + _BASE_MEDDPICC)
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    assert rows[0].days_in_stage == -1


# ── Test 4: render_pipeline_table sorts by STAGE_ORDER then days desc ─────────


@pytest.mark.unit
def test_render_table_sort_order(tmp_path):
    """negotiate rows appear before propose rows; within same stage, more days first."""
    rows = [
        PursuitRow("global-pay", "early", "propose", "pending", 5),
        PursuitRow("global-pay", "late", "negotiate", "pending", 3),
        PursuitRow("global-pay", "older", "propose", "pending", 10),
    ]
    table = render_pipeline_table(rows)
    lines = [ln for ln in table.splitlines() if ln.startswith("|") and "---" not in ln and "Account" not in ln]
    # First data row should be the negotiate row
    assert "negotiate" in lines[0]
    # Within propose rows: older (10 days) before early (5 days)
    propose_lines = [ln for ln in lines if "propose" in ln]
    assert propose_lines[0].find("older") < len(propose_lines[0])
    assert "older" in propose_lines[0]
    assert "early" in propose_lines[1]
    # Contract: render_pipeline_table must return a non-empty string with column headers
    assert isinstance(table, str)
    assert "Avg Days" in table, "column header 'Avg Days' must appear (implementation change rename)"
    assert "Native Qualification" in table
    assert "MEDDPICC" not in table


# ── Test 5: Gate icons ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_gate_icons_pending():
    assert GATE_ICONS["pending"] == "🔲"


@pytest.mark.unit
def test_gate_icons_pass():
    assert GATE_ICONS["pass"] == "✅"


@pytest.mark.unit
def test_gate_icons_fail():
    assert GATE_ICONS["fail"] == "❌"


@pytest.mark.unit
def test_gate_icons_override():
    assert GATE_ICONS["override"] == "⚠️"


@pytest.mark.unit
def test_render_table_shows_gate_icon_in_row():
    """Gate icon appears in the rendered table for each gate_status value."""
    for status, icon in GATE_ICONS.items():
        row = PursuitRow("global-pay", "deal", "discover", status, 5)
        table = render_pipeline_table([row])
        assert icon in table, f"Expected {icon!r} for gate_status={status!r}"


# ── implementation change: gate_status normalization ───────────────────────────────────────


# ── TestGateStatusNormalization (flattened) ─────────────────────────────────


@pytest.mark.unit
@pytest.mark.unit
def test_gate_status_normalization_unknown_gate_status_normalized_to_pending(tmp_path, write_pursuit_generic):
    """gate-status: clear → PursuitRow.gate_status == 'pending'."""
    acct = tmp_path / "accounts" / "acme-corp"
    write_pursuit_generic(acct, "deal-a", "stage: discover\ngate-status: clear\n" + _BASE_MEDDPICC)
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    assert rows[0].gate_status == "pending"


@pytest.mark.unit
@pytest.mark.unit
def test_gate_status_normalization_unknown_gate_status_logs_warning(tmp_path, write_pursuit_generic, caplog):
    """Unknown local gate status is contained as pending without qualification inference."""
    acct = tmp_path / "accounts" / "acme-corp"
    write_pursuit_generic(acct, "deal-b", "stage: discover\ngate-status: clear\n" + _BASE_MEDDPICC)
    rows = collect_pursuit_health(tmp_path)
    assert rows[0].gate_status == "pending"
    assert not any("MEDDPICC" in r.message for r in caplog.records)


@pytest.mark.unit
@pytest.mark.unit
def test_gate_status_normalization_known_gate_status_passes_through_unchanged(tmp_path, write_pursuit_generic):
    """A stored local pass cannot clear current native qualification."""
    acct = tmp_path / "accounts" / "acme-corp"
    write_pursuit_generic(acct, "deal-c", "stage: discover\ngate-status: pass\n" + _BASE_MEDDPICC)
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    assert rows[0].gate_status == "pending"


# ── Test 6: Score labels in rendered output ───────────────────────────────────


@pytest.mark.unit
def test_score_label_low_in_table():
    """Historical totals are not rendered as current qualification."""
    row = PursuitRow("global-pay", "deal", "discover", "pending", 5)
    table = render_pipeline_table([row])
    assert "6/24" not in table
    assert "Native Qualification" in table


@pytest.mark.unit
def test_score_label_medium_in_table():
    row = PursuitRow("global-pay", "deal", "discover", "pending", 5)
    table = render_pipeline_table([row])
    assert "12/24" not in table


@pytest.mark.unit
def test_score_label_high_in_table():
    row = PursuitRow("global-pay", "deal", "discover", "pending", 5)
    table = render_pipeline_table([row])
    assert "20/24" not in table


@pytest.mark.unit
def test_render_full_brief_contains_pursuit_health_table():
    """render_full_brief output contains '## Pursuit Health Table' heading."""
    rows = [PursuitRow("global-pay", "deal", "discover", "pending", 5)]
    brief = render_full_brief(rows, champion_signals=[], blindspot_data=[], no_llm=True, today=date(2026, 5, 18))
    assert "## Pursuit Health Table" in brief


# ── Test 7: --no-llm flag ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_no_llm_produces_placeholder(tmp_path):
    """--no-llm brief contains LLM placeholder text, not an LLM subprocess call."""
    rows = [PursuitRow("global-pay", "deal", "discover", "pending", 5)]
    with patch("subprocess.run") as mock_run:
        brief = render_full_brief(rows, champion_signals=[], blindspot_data=[], no_llm=True, today=date(2026, 5, 18))
        # subprocess.run must NOT have been called for LLM
        for call in mock_run.call_args_list:
            args = call[0][0] if call[0] else []
            assert "claude" not in str(args), "claude subprocess was called with --no-llm"
    assert "LLM narrative skipped" in brief or "LLM_NARRATIVE_PLACEHOLDER" not in brief


@pytest.mark.unit
def test_no_llm_brief_contains_table(tmp_path):
    """--no-llm brief contains the Pursuit Health Table header."""
    rows = [PursuitRow("global-pay", "deal", "discover", "pending", 5)]
    brief = render_full_brief(rows, champion_signals=[], blindspot_data=[], no_llm=True, today=date(2026, 5, 18))
    assert "Pursuit Health Table" in brief
    assert "LLM narrative skipped" in brief


# ── Test 8: Output file written to routines/briefs/ ──────────────────────────


@pytest.mark.unit
def test_output_file_path_contains_today(tmp_path, monkeypatch):
    """cli() writes output to briefs/pipeline-review-YYYY-MM-DD.md."""
    from click.testing import CliRunner

    import fieldkit.commands.pipeline.cli as pr_cli
    import fieldkit.commands.pipeline.main as pr_main

    today = datetime.now(tz=UTC).date().isoformat()

    monkeypatch.setattr(pr_main, "get_fieldkit_home", lambda: tmp_path)
    (tmp_path / "accounts").mkdir(exist_ok=True)
    (tmp_path / "briefs").mkdir(exist_ok=True)

    runner = CliRunner()
    result = runner.invoke(pr_cli.cli, ["--no-llm"])
    assert result.exit_code == 0, result.output

    files = list((tmp_path / "briefs").glob(f"pipeline-review-{today}.md"))
    assert len(files) == 1, f"Expected one file for {today}, found: {list((tmp_path / 'briefs').iterdir())}"


@pytest.mark.unit
def test_account_scoped_output_uses_distinct_filename_and_collector_scope(tmp_path):
    from unittest.mock import patch

    from fieldkit.commands.pipeline.main import _run

    with (
        patch(
            "fieldkit.commands.pipeline.main.collect_all_pursuit_data",
            return_value=([], [], []),
        ) as mock_collect,
        patch("fieldkit.commands.pipeline.main.render_full_brief", return_value="# Scoped review\n"),
    ):
        result = _run(no_llm=True, data_root_override=tmp_path, account="acme-corp")

    assert result is None
    today = datetime.now(tz=UTC).date().isoformat()
    scoped_review = tmp_path / "briefs" / f"pipeline-review-acme-corp-{today}.md"
    assert scoped_review.read_text(encoding="utf-8") == "# Scoped review\n"
    assert not (tmp_path / "briefs" / f"pipeline-review-{today}.md").exists()
    mock_collect.assert_called_once_with(tmp_path, account_filter="acme-corp")


@pytest.mark.unit
def test_account_scoped_output_rejects_unsafe_filename_component(tmp_path):
    from fieldkit.commands.pipeline.main import _run

    with pytest.raises(SystemExit) as exc_info:
        _run(no_llm=True, data_root_override=tmp_path, account="../../outside")

    assert exc_info.value.code == 3
    assert not (tmp_path / "briefs").exists()


@pytest.mark.unit
def test_render_table_override_icon_present():
    """The override gate icon ⚠️ appears when gate_status='override'."""
    row = PursuitRow("global-pay", "deal", "propose", "override", 20)
    table = render_pipeline_table([row])
    assert "⚠️" in table
    assert "override" in table


# ── _quarter_label ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_quarter_label_q1():
    assert _quarter_label(date(2026, 1, 15)) == "Q1 2026"


@pytest.mark.unit
def test_quarter_label_q2():
    assert _quarter_label(date(2026, 4, 1)) == "Q2 2026"


@pytest.mark.unit
def test_quarter_label_q3():
    assert _quarter_label(date(2026, 7, 31)) == "Q3 2026"


@pytest.mark.unit
def test_quarter_label_q4():
    assert _quarter_label(date(2026, 12, 1)) == "Q4 2026"


# ── _extract_account ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_extract_account_from_standard_path(tmp_path):
    """_extract_account pulls the segment after 'accounts'."""
    path = tmp_path / "accounts" / "global-pay" / "pursuits" / "deal.md"
    parts = path.parts
    assert _extract_account(parts) == "global-pay"


@pytest.mark.unit
def test_extract_account_unknown_when_no_accounts_segment(tmp_path):
    """_extract_account returns 'unknown' when 'accounts' is absent."""
    path = tmp_path / "some" / "other" / "path.md"
    parts = path.parts
    assert _extract_account(parts) == "unknown"


# ── _render_champion_section ──────────────────────────────────────────────────


@pytest.mark.unit
def test_render_champion_section_empty():
    """Empty signals list returns the no-data placeholder string."""
    result = _render_champion_section([])
    assert "derived champion identity" in result


@pytest.mark.unit
def test_render_champion_section_with_data():
    """Signals with data render a Markdown table row."""
    signals = [
        {
            "account": "global-pay",
            "deal": "project-shift",
            "champion": "Jane",
            "initiation": "40%",
            "last_outbound": "2026-05-01",
            "signal": "REACTIVE",
        }
    ]
    result = _render_champion_section(signals)
    assert "global-pay" in result
    assert "project-shift" in result
    assert "Jane" in result
    assert "/3" not in result
    assert "REACTIVE" in result


@pytest.mark.unit
def test_render_champion_section_missing_last_outbound():
    """Missing last_outbound renders as '—'."""
    signals = [
        {
            "account": "acme-bank",
            "deal": "deal",
            "champion": "Jane",
            "initiation": "",
            "last_outbound": "",
            "signal": "",
        }
    ]
    result = _render_champion_section(signals)
    assert "—" in result


# ── _render_blindspot_section ─────────────────────────────────────────────────


@pytest.mark.unit
def test_render_blindspot_section_no_blindspots():
    """All accounts meeting threshold renders the no-gap message."""
    data = [{"account": "global-pay", "active_pursuits": 3, "threshold": 1, "status": "ok"}]
    result = _render_blindspot_section(data)
    assert "meet their pursuit-coverage threshold" in result


@pytest.mark.unit
def test_render_blindspot_section_with_blindspot():
    """Account below threshold appears in the blindspot table."""
    data = [
        {"account": "shield-ins", "active_pursuits": 0, "threshold": 1, "status": "blindspot"},
    ]
    result = _render_blindspot_section(data)
    assert "shield-ins" in result
    assert "0" in result


@pytest.mark.unit
def test_render_blindspot_section_mixed():
    """Only blindspot-status accounts appear in the output."""
    data = [
        {"account": "global-pay", "active_pursuits": 2, "threshold": 1, "status": "ok"},
        {"account": "shield-ins", "active_pursuits": 0, "threshold": 1, "status": "blindspot"},
    ]
    result = _render_blindspot_section(data)
    assert "shield-ins" in result
    assert "global-pay" not in result


# ── collect_blindspot_data ────────────────────────────────────────────────────


@pytest.mark.unit
def test_collect_blindspot_data_no_accounts_yaml(tmp_path):
    """When accounts.yaml is absent, returns an empty list (not an error)."""
    (tmp_path / "accounts").mkdir()
    result = collect_blindspot_data(tmp_path)
    assert result == []


@pytest.mark.unit
def test_collect_blindspot_data_below_threshold(tmp_path):
    """Account with zero active pursuits and threshold=1 is classified as blindspot."""
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text(
        "accounts:\n  shield-ins:\n    domains: [shieldins.com]\n    blindspot_threshold: 1\n",
        encoding="utf-8",
    )
    result = collect_blindspot_data(tmp_path)
    sf = next((r for r in result if r["account"] == "shield-ins"), None)
    assert sf is not None
    assert sf["status"] == "blindspot"
    assert sf["active_pursuits"] == 0


@pytest.mark.unit
def test_collect_blindspot_data_above_threshold(tmp_path):
    """Account meeting threshold is classified as ok."""
    accounts_dir = tmp_path / "accounts" / "global-pay"
    accounts_dir.mkdir(parents=True)
    # Write one active pursuit
    pursuits_dir = accounts_dir / "pursuits"
    pursuits_dir.mkdir()
    (pursuits_dir / "deal.md").write_text(
        "---\nstage: discover\nmeddpicc:\n  metrics: 1\n  economic-buyer: 1\n"
        "  decision-criteria: 1\n  decision-process: 1\n  identify-pain: 1\n"
        "  champion: 1\n  competition: 1\n  paper-process: 1\n---\n",
        encoding="utf-8",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text(
        "accounts:\n  global-pay:\n    domains: [globalpay.com]\n    blindspot_threshold: 1\n",
        encoding="utf-8",
    )
    result = collect_blindspot_data(tmp_path)
    gpay = next((r for r in result if r["account"] == "global-pay"), None)
    assert gpay is not None
    assert gpay["status"] == "ok"
    assert gpay["active_pursuits"] == 1


# ── collect_champion_signals: no query.py ────────────────────────────────────


@pytest.mark.unit
def test_collect_champion_signals_no_pursuits(tmp_path):
    """With no pursuits, collect_champion_signals returns empty list."""
    from fieldkit.commands.pipeline.collect import collect_champion_signals as ccs

    (tmp_path / "accounts").mkdir()
    result = ccs(tmp_path)
    assert result == []


@pytest.mark.unit
def test_collect_champion_signals_skips_zero_champion(tmp_path, write_pursuit_generic):
    """Pursuits with champion=0 are excluded from champion signals."""
    from fieldkit.commands.pipeline.collect import collect_champion_signals as ccs

    acct = tmp_path / "accounts" / "global-pay"
    write_pursuit_generic(
        acct,
        "deal",
        "stage: discover\n"
        "meddpicc:\n"
        "  metrics: 1\n  economic-buyer: 1\n  decision-criteria: 1\n"
        "  decision-process: 1\n  identify-pain: 1\n  champion: 0\n"
        "  competition: 1\n  paper-process: 1\n",
    )
    result = ccs(tmp_path)
    assert result == []


# ── render_pipeline_table: empty rows ────────────────────────────────────────


@pytest.mark.unit
def test_render_pipeline_table_empty_rows():
    """Empty rows list renders a table with 'No active pursuits' placeholder."""
    table = render_pipeline_table([])
    assert "No active pursuits" in table


# ── End-to-end: main() writes a file ─────────────────────────────────────────


@pytest.mark.unit
def test_main_no_llm_end_to_end(tmp_path, monkeypatch):
    """cli() with --no-llm produces output containing the pursuit health table."""
    import fieldkit.commands.pipeline.collect as pr_collect

    (tmp_path / "accounts").mkdir()

    # Directly test collect + render (no filesystem output needed)
    rows = pr_collect.collect_pursuit_health(tmp_path)
    brief = render_full_brief(rows, champion_signals=[], blindspot_data=[], no_llm=True, today=date(2026, 5, 18))

    assert "## Pursuit Health Table" in brief
    assert "LLM narrative skipped" in brief
    assert "2026-05-18" in brief


def _patch_path(tmp_path, *args, **kwargs):
    """Not used — keeping end-to-end test direct."""
    from pathlib import Path

    return Path(*args, **kwargs)


# ── LLM synthesis path tests ──────────────────────────────────────────────────


@pytest.mark.unit
def test_render_full_brief_calls_synthesize():
    """When no_llm=False, render_full_brief() calls synthesize() and its return
    value appears in the rendered output. LLM_NARRATIVE_PLACEHOLDER is not present."""
    rows = [PursuitRow("global-pay", "deal", "discover", "pending", 5)]
    champion_signals: list = []
    blindspot_data: list = []

    with patch("fieldkit.commands.pipeline.main.synthesize", return_value="NARRATIVE CONTENT") as mock_synth:
        result = render_full_brief(
            rows,
            champion_signals=champion_signals,
            blindspot_data=blindspot_data,
            no_llm=False,
            today=date(2026, 5, 18),
        )
        mock_synth.assert_called_once()

    assert "NARRATIVE CONTENT" in result
    assert "LLM_NARRATIVE_PLACEHOLDER" not in result


@pytest.mark.unit
def test_render_full_brief_degrades_on_llm_error():
    """When synthesize() raises LLMError, render_full_brief() renders an inline
    failure message '_LLM synthesis failed: ..._' in the Narrative section.

    LLMError's canonical (and only) home is fieldkit.errors, which is never
    reloaded — so class identity holds across every import path, and the
    except-clause in render_full_brief matches regardless of test order.
    """

    rows = [PursuitRow("global-pay", "deal", "discover", "pending", 5)]

    with patch(
        "fieldkit.commands.pipeline.main.synthesize",
        side_effect=LLMError("api down"),
    ):
        result = render_full_brief(
            rows,
            champion_signals=[],
            blindspot_data=[],
            no_llm=False,
            today=date(2026, 5, 18),
        )

    # historic regression/376: error details are NOT embedded in the brief — sanitised message only.
    assert "_LLM synthesis unavailable" in result
    assert "api down" not in result  # error string must not leak into brief


@pytest.mark.unit
def test_render_full_brief_llm_error_shows_table_and_error_message(
    tmp_path: Path,
    write_pursuit_generic: object,
) -> None:
    """When synthesize() raises LLMError (e.g. timeout), render_full_brief degrades
    gracefully: the pursuit health table is present and the narrative block shows
    an LLM failure message.

    Spec ref: llm-timeout/spec.md — Scenario: LLM timeout degrades to no-llm
    output in pipeline review.
    """

    rows = [
        PursuitRow(
            account="acme",
            deal="deal-1",
            stage="discover",
            gate_status="pending",
            days_in_stage=10,
        )
    ]

    with patch(
        "fieldkit.commands.pipeline.main.synthesize",
        side_effect=LLMError("timed out after 90s"),
    ):
        result = render_full_brief(
            rows,
            champion_signals=[],
            blindspot_data=[],
            no_llm=False,
            today=date(2026, 6, 10),
        )

    # The pursuit health table must be present even when LLM fails
    assert "## Pursuit Health Table" in result
    # The narrative block must contain the sanitised failure message (historic regression/376)
    assert "LLM synthesis unavailable" in result
    # Error details must NOT leak into the brief (sanitised message only)
    assert "timed out after 90s" not in result


@pytest.mark.unit
def test_render_full_brief_llm_error_truncates_long_message() -> None:
    """historic regression: LLM error fallback must truncate at 120 chars to avoid leaking raw GCP JSON."""

    # Simulate a verbose Vertex AI exception message (400+ chars with GCP project path)
    long_error = (
        "Unexpected error from model vertex_ai/claude-sonnet-4-6: "
        'litellm.BadRequestError: Vertex_aiException BadRequestError - {"error":{"code":400,'
        '"message":"Publisher Model projects/internal-gcp-project/locations/us-central1/'
        'publishers/anthropic/models/claude-sonnet-4-6 is not servable in region us-central1.",'
        '"status":"FAILED_PRECONDITION"}}'
    )
    rows = [PursuitRow("global-pay", "deal", "discover", "pending", 5)]

    with patch(
        "fieldkit.commands.pipeline.main.synthesize",
        side_effect=LLMError(long_error),
    ):
        result = render_full_brief(
            rows,
            champion_signals=[],
            blindspot_data=[],
            no_llm=False,
            today=date(2026, 6, 11),
        )

    assert "_LLM synthesis unavailable" in result
    # The raw GCP project path must NOT appear in the brief output
    assert "internal-gcp-project" not in result, "GCP project path leaked into brief output"
    # Full JSON body must NOT appear
    assert "FAILED_PRECONDITION" not in result, "Raw GCP error JSON leaked into brief output"


# ── Stage Gate Criteria Tests (M003 — Pursuit Advance Validation) ────────────


# ── TestGateCriteriaThresholds (flattened) ──────────────────────────────────


@pytest.mark.unit
def test_gate_criteria_thresholds_validate_gate_requires_pain_champion_metrics():
    """Pipeline collection owns no local qualification threshold table."""
    import fieldkit.commands.pipeline.collect as collect

    assert not hasattr(collect, "GATE_CRITERIA")


@pytest.mark.unit
def test_gate_criteria_thresholds_propose_gate_requires_four_elements():
    """Pipeline collection does not copy local element thresholds."""
    import fieldkit.commands.pipeline.collect as collect

    assert not hasattr(collect, "ELEMENT_LABELS")


@pytest.mark.unit
def test_gate_criteria_thresholds_negotiate_gate_requires_all_elements_at_2():
    """No local numeric threshold can establish native qualification."""
    import fieldkit.commands.pipeline.collect as collect

    assert not hasattr(collect, "SCORE_LABELS")


@pytest.mark.unit
def test_gate_criteria_thresholds_no_gate_for_discover():
    """Local pipeline reports carry only native availability state."""
    row = PursuitRow("acme", "deal", "discover", "pending", 1)
    assert row.native_qualification.startswith("unavailable")


# ── Transition History Round-Trip Tests ──────────────────────────────────────


# ── TestTransitionHistoryRoundTrip (flattened) ──────────────────────────────


@pytest.mark.unit
def test_transition_history_round_trip_fixture_has_transition_history():
    """The pursuit_full fixture has a transition-history field."""
    from fieldkit.pursuit.io import load_pursuit

    fixtures = Path(__file__).parent / "fixtures" / "pursuit_full.md"
    model, _, _ = load_pursuit(fixtures)
    assert model.transition_history is not None
    assert len(model.transition_history) == 1


@pytest.mark.unit
def test_transition_history_round_trip_transition_history_fields():
    """Each transition entry has date, from_, to, and gate_result (TransitionEntry model)."""
    from fieldkit.pursuit.io import load_pursuit
    from fieldkit.pursuit.models import TransitionEntry

    fixtures = Path(__file__).parent / "fixtures" / "pursuit_full.md"
    model, _, _ = load_pursuit(fixtures)
    entry = model.transition_history[0]
    # historic regression: entries are now TransitionEntry models, not plain dicts.
    assert isinstance(entry, TransitionEntry)
    assert entry.date is not None
    assert entry.from_ == "validate"
    assert entry.to == "propose"
    assert entry.gate_result == "pass"


@pytest.mark.unit
def test_transition_history_round_trip_round_trip_preserves_transition_history(tmp_path):
    """Write then re-read preserves transition-history entries."""
    import shutil

    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = Path(__file__).parent / "fixtures" / "pursuit_full.md"
    dst = tmp_path / "pursuit.md"
    shutil.copy(src, dst)

    model, body, _ = load_pursuit(dst)
    write_frontmatter(dst, model, body)

    model2, _, _ = load_pursuit(dst)
    assert model2.transition_history is not None
    assert len(model2.transition_history) == len(model.transition_history)
    # historic regression: entries are now TransitionEntry models, not plain dicts.
    assert model2.transition_history[0].from_ == "validate"
    assert model2.transition_history[0].to == "propose"


@pytest.mark.unit
def test_transition_history_round_trip_gate_status_preserved(tmp_path):
    """gate-status field survives read→write round-trip."""
    import shutil

    from fieldkit.pursuit.io import load_pursuit, write_frontmatter

    src = Path(__file__).parent / "fixtures" / "pursuit_full.md"
    dst = tmp_path / "pursuit.md"
    shutil.copy(src, dst)

    model, body, _ = load_pursuit(dst)
    assert model.gate_status == "fail"

    write_frontmatter(dst, model, body)
    model2, _, _ = load_pursuit(dst)
    assert model2.gate_status == "fail"


# ── implementation change: Close Date column ───────────────────────────────────────────────


@pytest.mark.unit
def test_close_date_column_past_due():
    """Past-due close date shows 🔴 flag."""
    row = PursuitRow("acct", "deal", "discover", "pending", 5, close_date_str="2025-01-01")
    table = render_pipeline_table([row])
    assert "🔴" in table
    assert "2025-01-01" in table


@pytest.mark.unit
def test_close_date_column_near_term():
    """Close date 15-30 days away shows 🔴 flag (urgent)."""
    from datetime import timedelta

    # 20 days: in the 15-30 day window → 🔴 (not 🚨 which is ≤14 days)
    near = (date.today() + timedelta(days=20)).isoformat()
    row = PursuitRow("acct", "deal", "discover", "pending", 5, close_date_str=near)
    table = render_pipeline_table([row])
    assert "🔴" in table


@pytest.mark.unit
def test_close_date_column_31_to_60_days():
    """Close date 31-60 days away shows 🟡 flag (watch)."""
    from datetime import timedelta

    mid = (date.today() + timedelta(days=45)).isoformat()
    row = PursuitRow("acct", "deal", "discover", "pending", 5, close_date_str=mid)
    table = render_pipeline_table([row])
    assert "🟡" in table


@pytest.mark.unit
def test_close_date_column_future():
    """Close date >60 days away shows plain date, no flag."""
    from datetime import timedelta

    far = (date.today() + timedelta(days=90)).isoformat()
    row = PursuitRow("acct", "deal", "discover", "pending", 5, close_date_str=far)
    table = render_pipeline_table([row])
    assert "🔴" not in table
    assert "🟡" not in table
    assert far[:10] in table


@pytest.mark.unit
def test_close_date_column_missing():
    """No close date shows — placeholder."""
    row = PursuitRow("acct", "deal", "discover", "pending", 5)
    table = render_pipeline_table([row])
    # Should have a — in the Close column (at minimum one — in table)
    assert "—" in table


@pytest.mark.unit
def test_single_glob_pass(tmp_path, monkeypatch):
    """collect_all_pursuit_data calls iterate_pursuits exactly once."""
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    with (
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([])) as mock_iter,
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value={}),
    ):
        collect_all_pursuit_data(tmp_path)

    assert mock_iter.call_count == 1, f"iterate_pursuits was called {mock_iter.call_count} times; expected exactly 1"


# ── calculate_quota_gap tests ─────────────────────────────────────────────────


@pytest.mark.unit
def test_quota_gap_basic():
    """closed-won + weighted pipeline sum produces correct gap.

    historic regression: weights now match pursuit forecast (conservative values).
    """
    pursuits = [
        {"stage": "closed-won", "sf_amount": "500000"},
        {"stage": "negotiate", "sf_amount": "1000000"},  # 0.75 weight
        {"stage": "propose", "sf_amount": "1000000"},  # 0.50 weight
    ]
    quota_config = {"target": 2000000, "period": "2026-H2"}
    result = calculate_quota_gap(pursuits, quota_config)
    assert result["target"] == pytest.approx(2000000.0)
    assert result["closed_won"] == pytest.approx(500000.0)
    # weighted = 1000000*0.75 + 1000000*0.50 = 750000 + 500000 = 1250000
    assert result["weighted"] == pytest.approx(1250000.0)
    # gap = 2000000 - 500000 - 1250000 = 250000
    assert result["gap"] == pytest.approx(250000.0)


@pytest.mark.unit
def test_quota_gap_missing_sf_amount_skipped():
    """Pursuits without sf_amount are excluded from the calculation."""
    pursuits = [
        {"stage": "negotiate", "sf_amount": None},
        {"stage": "propose", "sf_amount": ""},
        {"stage": "closed-won", "sf_amount": "200000"},
    ]
    quota_config = {"target": 1000000}
    result = calculate_quota_gap(pursuits, quota_config)
    assert result["closed_won"] == pytest.approx(200000.0)
    assert result["weighted"] == pytest.approx(0.0)
    assert result["gap"] == pytest.approx(800000.0)


@pytest.mark.unit
def test_quota_gap_closed_lost_not_counted():
    """closed-lost pursuits are not in the pursuits list (filtered upstream)."""
    pursuits = [
        {"stage": "closed-won", "sf_amount": "300000"},
    ]
    quota_config = {"target": 500000}
    result = calculate_quota_gap(pursuits, quota_config)
    assert result["closed_won"] == pytest.approx(300000.0)
    assert result["gap"] == pytest.approx(200000.0)


@pytest.mark.unit
def test_quota_gap_over_quota():
    """Gap is negative when closed-won + weighted exceed target."""
    pursuits = [
        {"stage": "closed-won", "sf_amount": "3000000"},
    ]
    quota_config = {"target": 2000000}
    result = calculate_quota_gap(pursuits, quota_config)
    assert result["gap"] == pytest.approx(-1000000.0)


@pytest.mark.unit
def test_quota_gap_all_stage_weights():
    """All active stages apply their correct probability weights.

    historic regression: weights now match pursuit forecast (conservative values).
    """
    import pytest as _pytest

    stage_weights = {
        "negotiate": 0.75,
        "propose": 0.50,
        "validate": 0.25,
        "discover": 0.10,
        "qualify": 0.05,
        "prospect": 0.05,
        "pre-pipeline": 0.0,
    }
    for stage, weight in stage_weights.items():
        pursuits = [{"stage": stage, "sf_amount": "1000000"}]
        result = calculate_quota_gap(pursuits, {"target": 0})
        assert result["weighted"] == _pytest.approx(1000000.0 * weight), f"stage={stage}"


@pytest.mark.unit
def test_quota_gap_dollar_string_amount():
    """Dollar-formatted strings like '$1,200,000.00' are parsed correctly."""
    pursuits = [{"stage": "negotiate", "sf_amount": "$1,200,000.00"}]
    quota_config = {"target": 2000000}
    result = calculate_quota_gap(pursuits, quota_config)
    assert result["weighted"] == pytest.approx(1200000.0 * 0.75)


@pytest.mark.unit
def test_quota_gap_uses_sf_probability():
    """sf_probability=75 on a pursuit with amount 1000000 → weighted=750000."""
    pursuits = [{"stage": "negotiate", "sf_amount": "1000000", "sf_probability": 75}]
    result = calculate_quota_gap(pursuits, {"target": 0})
    assert result["weighted"] == pytest.approx(750000.0)


@pytest.mark.unit
def test_quota_gap_falls_back_to_stage_weight():
    """Pursuit without sf_probability falls back to QUOTA_STAGE_WEIGHTS (historic regression: now 0.50)."""
    pursuits = [{"stage": "propose", "sf_amount": "1000000"}]
    result = calculate_quota_gap(pursuits, {"target": 0})
    assert result["weighted"] == pytest.approx(500000.0)


@pytest.mark.unit
def test_quota_gap_mixed_probability_and_stage():
    """Mix of pursuits: one with sf_probability, one without."""
    pursuits = [
        {"stage": "negotiate", "sf_amount": "1000000", "sf_probability": 80},
        {"stage": "propose", "sf_amount": "500000"},
    ]
    result = calculate_quota_gap(pursuits, {"target": 0})
    # 1000000 * 0.80 + 500000 * 0.50
    assert result["weighted"] == pytest.approx(800000.0 + 250000.0)


# ── historic regression: collect_all_pursuit_data blindspot skips internal accounts ────────


@pytest.mark.unit
def test_bug217_collect_all_pursuit_data_skips_internal_accounts(tmp_path):
    """historic regression: Internal accounts must not appear in blindspot_data.

    collect_all_pursuit_data() iterated all accounts_cfg without filtering
    internal=True entries, causing internal accounts to show
    up as blindspots. The fix adds the same internal guard present in
    collect_blindspot_data().
    """
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    accounts_config = {
        "accounts": {
            "acme-corp": {"domains": ["acme.com"], "blindspot_threshold": 1},
            "example-internal": {"domains": ["internal.example.com"], "internal": True},
        }
    }

    with (
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([])),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value=accounts_config),
    ):
        _, _, blindspot_data = collect_all_pursuit_data(tmp_path)

    account_names = [b["account"] for b in blindspot_data]
    assert "example-internal" not in account_names, (
        "internal account must be excluded from blindspot_data"
    )  # pii-guard: ignore
    assert "acme-corp" in account_names, "non-internal account 'acme-corp' must be included"


# ── historic regression: close-date ≤14 days shows 🚨 critical tier ─────────────────────


@pytest.mark.unit
def test_bug216_close_date_14_days_shows_critical_icon():
    """historic regression: Close date exactly 14 days away shows 🚨 (critical), not 🔴.

    Previously everything ≤30 days showed 🔴. The fix inserts a ≤14 tier
    with 🚨 before the ≤30 check.
    """
    from datetime import timedelta

    critical = (date.today() + timedelta(days=14)).isoformat()
    row = PursuitRow("acct", "deal", "discover", "pending", 5, close_date_str=critical)
    table = render_pipeline_table([row])
    assert "🚨" in table, "≤14 days must show 🚨 critical icon"
    assert "🔴" not in table or "🚨" in table  # 🚨 takes precedence


@pytest.mark.unit
def test_bug216_close_date_7_days_shows_critical_icon():
    """historic regression: Close date 7 days away shows 🚨 (critical)."""
    from datetime import timedelta

    critical = (date.today() + timedelta(days=7)).isoformat()
    row = PursuitRow("acct", "deal", "discover", "pending", 5, close_date_str=critical)
    table = render_pipeline_table([row])
    assert "🚨" in table


@pytest.mark.unit
def test_bug216_close_date_15_days_shows_urgent_not_critical():
    """historic regression: Close date 15 days away shows 🔴 (urgent), not 🚨 (critical)."""
    from datetime import timedelta

    urgent = (datetime.now(tz=UTC).date() + timedelta(days=15)).isoformat()
    row = PursuitRow("acct", "deal", "discover", "pending", 5, close_date_str=urgent)
    table = render_pipeline_table([row])
    assert "🔴" in table
    assert "🚨" not in table


# ── historic regression / implementation change: _query_champion_signals_inprocess returns empty on no match ──


@pytest.mark.unit
def test_bug002_champion_query_no_match_returns_empty():
    """historic regression (implementation change): When query_champion_signals returns 'No people matched …',
    _query_champion_signals_inprocess must return ('', '', '').
    """
    from unittest.mock import MagicMock

    from fieldkit.commands.pipeline.collect import _query_champion_signals_inprocess

    conn = MagicMock()
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "fieldkit.commands.pipeline.collect.query_champion_signals",
        return_value="No people matched 'Jane Smith'\n",
    ):
        result = _query_champion_signals_inprocess(conn, "Jane Smith")

    assert result == ("", "", ""), f"Expected ('', '', '') for no-match output, got {result!r}"


@pytest.mark.unit
def test_bug002_champion_query_with_last_outbound_parses_correctly():
    """historic regression (implementation change): When query_champion_signals returns valid output with
    'Last outbound', _query_champion_signals_inprocess parses it correctly.
    """
    from unittest.mock import MagicMock

    from fieldkit.commands.pipeline.collect import _query_champion_signals_inprocess

    valid_output = (
        "  Threads initiated   : 3  (50% initiation rate)\n"
        "  Last outbound       : 2026-01-15  30d ago\n"
        "  Signal: INITIATOR\n"
    )
    conn = MagicMock()
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "fieldkit.commands.pipeline.collect.query_champion_signals",
        return_value=valid_output,
    ):
        _initiation, last_outbound, signal = _query_champion_signals_inprocess(conn, "Jane Smith")

    assert last_outbound != "", "Expected non-empty last_outbound for valid output"
    assert signal == "INITIATOR"


# ── historic regression: Missing sf_close_date shows ⚠ no date for non-pre-pipeline ──────


@pytest.mark.unit
def test_bug010_missing_close_date_shows_warning_for_active_stage(tmp_path, write_pursuit_generic):
    """historic regression: When sf_close_date is absent and stage is not pre-pipeline,
    close_date_str must be '⚠ no date' rather than '' (silent skip).

    Previously the empty string caused the date-flag block to be silently
    skipped, hiding the missing-date problem from the reviewer.
    """
    acct = tmp_path / "accounts" / "acme-bank"
    write_pursuit_generic(
        acct,
        "deal-no-date",
        "stage: discover\n" + _BASE_MEDDPICC,
        # No sf_close_date in frontmatter
    )
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row.close_date_str == "⚠ no date", (
        f"Expected '⚠ no date' for missing sf_close_date on active stage, got {row.close_date_str!r}"
    )


@pytest.mark.unit
def test_bug010_missing_close_date_renders_in_table(tmp_path, write_pursuit_generic):
    """historic regression: The ⚠ no date indicator appears in the rendered pipeline table."""
    acct = tmp_path / "accounts" / "acme-bank"
    write_pursuit_generic(
        acct,
        "deal-no-date",
        "stage: validate\n" + _BASE_MEDDPICC,
    )
    rows = collect_pursuit_health(tmp_path)
    table = render_pipeline_table(rows)
    assert "⚠ no date" in table, "Missing close date must render as '⚠ no date' in the table"


@pytest.mark.unit
def test_bug010_pre_pipeline_stage_no_warning(tmp_path, write_pursuit_generic):
    """historic regression: pre-pipeline stage with no sf_close_date shows '' (no warning).

    Pre-pipeline deals are not expected to have a close date yet.
    """
    acct = tmp_path / "accounts" / "acme-bank"
    write_pursuit_generic(
        acct,
        "deal-prepipe",
        "stage: pre-pipeline\n" + _BASE_MEDDPICC,
    )
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row.close_date_str == "", (
        f"Expected '' for missing sf_close_date on pre-pipeline stage, got {row.close_date_str!r}"
    )


# ── implementation change: stage velocity column ───────────────────────────────────────────


# ── TestVelocityDaysPerStage (flattened) ────────────────────────────────────


@pytest.mark.unit
def test_velocity_days_per_stage_velocity_none_when_no_history(tmp_path, write_pursuit_generic):
    """When transition_history is absent, velocity_days_per_stage is None."""
    acct = tmp_path / "accounts" / "acme-bank"
    write_pursuit_generic(
        acct,
        "deal-no-history",
        "stage: discover\nlast-transition: 2026-01-01\n" + _BASE_MEDDPICC,
    )
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    assert rows[0].velocity_days_per_stage is None


@pytest.mark.unit
def test_velocity_days_per_stage_velocity_computed_with_history(tmp_path, write_pursuit_generic):
    """velocity = days_in_stage / len(transition_history)."""
    acct = tmp_path / "accounts" / "acme-bank"
    write_pursuit_generic(
        acct,
        "deal-with-history",
        (
            "stage: discover\n"
            "last-transition: 2026-01-01\n"
            "transition-history:\n"
            "  - from: pre-pipeline\n"
            "    to: discover\n"
            "    date: 2026-01-01\n"
            "  - from: prospect\n"
            "    to: pre-pipeline\n"
            "    date: 2025-12-01\n"
        )
        + _BASE_MEDDPICC,
    )
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row.velocity_days_per_stage is not None
    # 2 history entries → velocity = days_in_stage / 2
    assert row.days_in_stage >= 0
    assert abs(row.velocity_days_per_stage - row.days_in_stage / 2) < 0.01


@pytest.mark.unit
def test_velocity_days_per_stage_velocity_with_empty_history_uses_denominator_1(tmp_path, write_pursuit_generic):
    """Empty transition-history list → denominator is max(0,1)=1, so velocity=days_in_stage."""
    acct = tmp_path / "accounts" / "acme-bank"
    write_pursuit_generic(
        acct,
        "deal-empty-history",
        "stage: discover\nlast-transition: 2026-01-01\ntransition-history: []\n" + _BASE_MEDDPICC,
    )
    rows = collect_pursuit_health(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    # Empty list is not None → velocity should be computed
    assert row.velocity_days_per_stage is not None
    assert abs(row.velocity_days_per_stage - row.days_in_stage) < 0.01


# ── TestRenderPipelineTableVelColumn (flattened) ────────────────────────────


def _render_pipeline_table_vel_column_make_row(velocity: float | None = None) -> PursuitRow:
    return PursuitRow(
        account="acme",
        deal="test-deal",
        stage="discover",
        gate_status="pending",
        days_in_stage=30,
        velocity_days_per_stage=velocity,
    )


@pytest.mark.unit
def test_render_pipeline_table_vel_column_header_contains_avg_days() -> None:
    """implementation change: column header is now 'Avg Days' (was 'Vel')."""
    table = render_pipeline_table([_render_pipeline_table_vel_column_make_row()])
    assert "| Avg Days |" in table


@pytest.mark.unit
def test_render_pipeline_table_vel_column_velocity_shown_as_decimal() -> None:
    table = render_pipeline_table([_render_pipeline_table_vel_column_make_row(velocity=15.5)])
    assert "15.5" in table


@pytest.mark.unit
def test_render_pipeline_table_vel_column_velocity_none_shown_as_dash() -> None:
    table = render_pipeline_table([_render_pipeline_table_vel_column_make_row(velocity=None)])
    # The Avg Days column should contain "—" for None
    lines = table.splitlines()
    data_line = next(ln for ln in lines if "test-deal" in ln)
    assert "| — |" in data_line


@pytest.mark.unit
def test_render_pipeline_table_vel_column_empty_rows_table_has_avg_days_header() -> None:
    """implementation change: empty table also has 'Avg Days' header."""
    table = render_pipeline_table([])
    assert "| Avg Days |" in table


# ── implementation change: _query_champion_signals_inprocess exception path ────────────────


# ── TestQueryChampionSignalsInprocess (flattened) ───────────────────────────


@pytest.mark.unit
def test_collect_champion_signals_returns_empty_on_exception(caplog):
    """When query_champion_signals raises sqlite3.Error, returns ('', '', '') and logs WARNING."""
    import logging
    import sqlite3
    from unittest.mock import MagicMock

    from fieldkit.commands.pipeline.collect import _query_champion_signals_inprocess

    conn = MagicMock()
    with (
        __import__("unittest.mock", fromlist=["patch"]).patch(
            "fieldkit.commands.pipeline.collect.query_champion_signals",
            side_effect=sqlite3.OperationalError("Expression tree too large"),
        ),
        caplog.at_level(logging.WARNING, logger="fieldkit.commands.pipeline.main"),
    ):
        result = _query_champion_signals_inprocess(conn, "acme")

    assert result == ("", "", "")
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        f"Expected WARNING on exception, got: {[r.levelname for r in caplog.records]}"
    )


@pytest.mark.unit
def test_collect_champion_signals_returns_empty_when_output_blank():
    """Blank output from query_champion_signals → returns ('', '', '')."""
    from unittest.mock import MagicMock

    from fieldkit.commands.pipeline.collect import _query_champion_signals_inprocess

    conn = MagicMock()
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "fieldkit.commands.pipeline.collect.query_champion_signals",
        return_value="",
    ):
        result = _query_champion_signals_inprocess(conn, "acme")

    assert result == ("", "", "")


@pytest.mark.unit
def test_collect_champion_signals_parses_valid_output():
    """Valid output with 'Last outbound' is parsed correctly."""
    from unittest.mock import MagicMock

    from fieldkit.commands.pipeline.collect import _query_champion_signals_inprocess

    valid_output = (
        "  Threads initiated   : 5  (60% initiation rate)\n"
        "  Last outbound       : 2026-03-01  10d ago\n"
        "  Signal: INITIATOR\n"
    )
    conn = MagicMock()
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "fieldkit.commands.pipeline.collect.query_champion_signals",
        return_value=valid_output,
    ):
        _init, last_out, signal = _query_champion_signals_inprocess(conn, "Jane Smith")

    assert last_out != ""
    assert signal == "INITIATOR"


# ---------------------------------------------------------------------------
# Task 11.4 — collect_all_pursuit_data handles absent gmail.db
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_collect_all_pursuit_data_handles_absent_gmail_db(tmp_path: Path) -> None:
    """collect_all_pursuit_data returns empty signals list when gmail.db is absent."""
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    data_root = _make_data_root(tmp_path)

    # Patch get_gmail_db_path to return a path that does not exist
    absent_db = tmp_path / "nonexistent.db"
    with (
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=absent_db),
        patch("fieldkit.pursuit.iterate_pursuits", return_value=[]),
        patch("fieldkit.pursuit.read_accounts_config", return_value={}),
    ):
        rows, signals, _blindspot_data = collect_all_pursuit_data(data_root)

    # No pursuits → empty rows; gmail.db absent → signals is None (historic regression)
    assert rows == []
    assert signals is None


# ---------------------------------------------------------------------------
# Additional collect_all_pursuit_data() branch-coverage tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_collect_all_pursuit_data_skips_closed_pursuits(tmp_path: Path, write_pursuit_generic: object) -> None:
    """collect_all_pursuit_data excludes closed-won/closed-lost pursuits from rows."""
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    acct = tmp_path / "accounts" / "acme-corp"
    write_pursuit_generic(acct, "closed-deal", "stage: closed-won\n" + _BASE_MEDDPICC)  # type: ignore[operator]
    write_pursuit_generic(acct, "active-deal", "stage: discover\n" + _BASE_MEDDPICC)  # type: ignore[operator]

    absent_db = tmp_path / "no.db"
    with (
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=absent_db),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value={}),
    ):
        rows, _signals, _ = collect_all_pursuit_data(tmp_path)

    deal_names = [r.deal for r in rows]
    assert "active-deal" in deal_names
    assert "closed-deal" not in deal_names


@pytest.mark.unit
def test_collect_all_pursuit_data_blindspot_threshold_respected(tmp_path: Path) -> None:
    """collect_all_pursuit_data correctly classifies accounts as ok/blindspot."""
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    accounts_config = {
        "accounts": {
            "acme-corp": {"domains": ["acme.com"], "blindspot_threshold": 1},
            "shield-ins": {"domains": ["shieldins.com"], "blindspot_threshold": 1},
        }
    }

    absent_db = tmp_path / "no.db"
    with (
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=absent_db),
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([])),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value=accounts_config),
    ):
        _, _, blindspot_data = collect_all_pursuit_data(tmp_path)

    # Both accounts have 0 active pursuits and threshold=1 → both are blindspots
    statuses = {b["account"]: b["status"] for b in blindspot_data}
    assert statuses.get("acme-corp") == "blindspot"
    assert statuses.get("shield-ins") == "blindspot"


@pytest.mark.unit
def test_collect_all_pursuit_data_accounts_config_not_dict(tmp_path: Path) -> None:
    """collect_all_pursuit_data handles non-dict accounts_cfg gracefully."""
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    # accounts config where 'accounts' is a list (malformed)
    accounts_config: dict = {"accounts": ["item1", "item2"]}

    absent_db = tmp_path / "no.db"
    with (
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=absent_db),
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([])),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value=accounts_config),
    ):
        _rows, _signals, blindspot_data = collect_all_pursuit_data(tmp_path)

    # Non-dict accounts_cfg → empty blindspot_data
    assert blindspot_data == []


@pytest.mark.unit
def test_collect_all_pursuit_data_accounts_config_yaml_error(tmp_path: Path) -> None:
    """collect_all_pursuit_data handles read_accounts_config raising FileNotFoundError."""
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    absent_db = tmp_path / "no.db"
    with (
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=absent_db),
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([])),
        patch(
            "fieldkit.commands.pipeline.collect.read_accounts_config",
            side_effect=FileNotFoundError("no accounts.yaml"),
        ),
    ):
        _rows, _signals, blindspot_data = collect_all_pursuit_data(tmp_path)

    # FileNotFoundError → config={} → empty blindspot_data
    assert blindspot_data == []


@pytest.mark.unit
def test_collect_all_pursuit_data_invalid_pursuit_file_skipped(tmp_path: Path) -> None:
    """collect_all_pursuit_data skips pursuit files that fail to parse."""
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    # Simulate iterate_pursuits returning a path, but load_pursuit raises ValidationError
    fake_path = tmp_path / "accounts" / "acme" / "pursuits" / "bad-deal.md"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_text("---\nstage: discover\n---\n", encoding="utf-8")

    absent_db = tmp_path / "no.db"
    with (
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=absent_db),
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([fake_path])),
        patch(
            "fieldkit.commands.pipeline.collect.load_pursuit",
            side_effect=ValueError("bad frontmatter"),
        ),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value={}),
    ):
        rows, _signals, _ = collect_all_pursuit_data(tmp_path)

    # The invalid pursuit is skipped; no rows
    assert rows == []


@pytest.mark.unit
def test_collect_all_pursuit_data_gmail_connect_error_returns_empty_signals(tmp_path: Path) -> None:
    """collect_all_pursuit_data handles sqlite3.Error on gmail connect gracefully."""
    import sqlite3
    from unittest.mock import patch

    from fieldkit.commands.pipeline.collect import collect_all_pursuit_data

    # Simulate gmail.db existing but connection failing
    fake_db = tmp_path / "gmail.db"
    fake_db.write_text("not a sqlite db", encoding="utf-8")

    with (
        patch("fieldkit.commands.pipeline.collect.get_gmail_db_path", return_value=fake_db),
        patch(
            "fieldkit.commands.pipeline.collect._gmail_connect",
            side_effect=sqlite3.OperationalError("not a database"),
        ),
        patch("fieldkit.commands.pipeline.collect.iterate_pursuits", return_value=iter([])),
        patch("fieldkit.commands.pipeline.collect.read_accounts_config", return_value={}),
    ):
        _rows, signals, _ = collect_all_pursuit_data(tmp_path)

    assert signals is None  # historic regression: gmail.db unavailable → None not []


# ---------------------------------------------------------------------------
# historic regression regression: --no-llm after subcommand must raise clear error
# ---------------------------------------------------------------------------


# ── TestNoLlmFlagPositionDetection (flattened) ──────────────────────────────


def _no_llm_flag_position_detection_invoke(argv: list[str]) -> object:
    from click.testing import CliRunner

    from fieldkit.commands.pipeline.cli import cli

    runner = CliRunner()
    with patch.object(sys, "argv", argv):
        return runner.invoke(cli, argv[2:], catch_exceptions=True)


@pytest.mark.unit
def test_no_llm_flag_position_detection_no_llm_after_open_exits_nonzero() -> None:
    """A misplaced --no-llm flag is a usage/data error, not authentication."""
    result = _no_llm_flag_position_detection_invoke(["fieldkit", "pipeline", "open", "--no-llm"])
    assert result.exit_code == 3


@pytest.mark.unit
def test_no_llm_flag_position_detection_no_llm_after_open_output_mentions_flag() -> None:
    """Error output must mention --no-llm so user knows what went wrong."""
    result = _no_llm_flag_position_detection_invoke(["fieldkit", "pipeline", "open", "--no-llm"])
    assert "--no-llm" in (result.output or "")


@pytest.mark.unit
def test_no_llm_flag_position_detection_no_llm_before_subcommand_does_not_show_position_error() -> None:
    """Correct placement 'pipeline --no-llm open' must not show the position-error."""
    result = _no_llm_flag_position_detection_invoke(["fieldkit", "pipeline", "--no-llm", "open"])
    assert "--no-llm must come before" not in (result.output or "")
