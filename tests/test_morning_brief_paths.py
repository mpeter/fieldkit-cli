"""Regression tests for historic regression and historic regression path resolution fixes.

historic regression: collect_stale_prose() used to accept a project_root parameter
  supplied by the caller (get_fieldkit_root()), which could point to the
  wrong directory when fieldkit_root is misconfigured.  The fix derives the
  script path from __file__ inside the function itself.

historic regression: _run() used to fall back to get_fieldkit_root() as data_root when
  config was absent, causing collect_tasks() to look for TASKS.md in the
  code repo (which has none) and return empty placeholder strings.  The fix
  separates output_dir (brief file location) from data_root (user data repo)
  in the fallback path.

Tests use tmp_path and monkeypatch for full filesystem isolation — no live
config, no real data repo, no external API calls (P14).
"""

from pathlib import Path

import pytest

import fieldkit.commands.brief.main as _brief_mod
from fieldkit.commands.brief.main import _run, collect_stale_prose, collect_tasks

# ── collect_stale_prose ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_collect_stale_prose_finds_script_via_package_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """collect_stale_prose finds stale.py via __file__-derived path.

    Monkeypatches the module's __file__ so the internal path derivation
    resolves to tmp_path/fieldkit/morning_brief/main.py, making the script
    expected at tmp_path/fieldkit/pursuit/stale.py.
    """
    # Create the fake package layout that mirrors the real repo structure:
    #   tmp_path/
    #     fieldkit/morning_brief/main.py   ← monkeypatched __file__
    #     fieldkit/pursuit/stale.py            ← fake script (prints nothing)
    fake_module_file = tmp_path / "fieldkit" / "morning_brief" / "main.py"
    fake_module_file.parent.mkdir(parents=True, exist_ok=True)
    fake_module_file.touch()

    fake_script = tmp_path / "fieldkit" / "pursuit" / "stale.py"
    fake_script.parent.mkdir(parents=True, exist_ok=True)
    # Script that exits 0 with no output (simulates "no stale prose found")
    fake_script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    # Create a minimal data_root with one pursuit directory so iterate_pursuits
    # returns at least one path (otherwise the function returns "None detected."
    # before even checking the script).
    data_root = tmp_path / "data"
    pursuit_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True, exist_ok=True)
    pursuit_file = pursuit_dir / "deal.md"
    pursuit_file.write_text(
        "---\nstage: propose\nsf_stage: Propose\n---\n# Deal\n",
        encoding="utf-8",
    )

    # Patch __file__ on the module so Path(__file__).resolve().parent.parent.parent
    # resolves to tmp_path (the fake repo root).
    monkeypatch.setattr(_brief_mod, "__file__", str(fake_module_file))

    result = collect_stale_prose(data_root)

    # The script ran (it exists) and produced no output → "None detected."
    # This confirms the function found the script via the __file__-derived path,
    # not via a caller-supplied root.
    assert result == "None detected.", f"Expected 'None detected.' but got: {result!r}"


# ── collect_tasks ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_collect_tasks_reads_today_and_waiting_sections(tmp_path: Path) -> None:
    """collect_tasks extracts Today and Waiting On sections from TASKS.md."""
    tasks_file = tmp_path / "TASKS.md"
    tasks_file.write_text(
        """\
## Today

- [ ] Send follow-up to Acme Corp
- [ ] Review proposal draft

## Waiting On

- [ ] Acme Corp: approval on SOW (due 2026-06-15)

## Done

- [x] Completed task
""",
        encoding="utf-8",
    )

    today, waiting = collect_tasks(tmp_path)

    assert "Send follow-up to Acme Corp" in today
    assert "Review proposal draft" in today
    assert "Acme Corp: approval on SOW" in waiting
    # Done section must not bleed into either output
    assert "Completed task" not in today
    assert "Completed task" not in waiting


@pytest.mark.unit
def test_collect_tasks_returns_placeholders_when_tasks_md_absent(tmp_path: Path) -> None:
    """collect_tasks returns placeholder strings when TASKS.md does not exist."""
    # tmp_path is empty — no TASKS.md present
    today, waiting = collect_tasks(tmp_path)

    assert today == "(nothing committed for today yet)"
    assert waiting == "(nothing in the queue)"


# ── LLM timeout / fallback path ───────────────────────────────────────────────


@pytest.mark.unit
def test_run_falls_back_to_no_llm_brief_on_llm_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When synthesize() raises LLMError during _run(), the brief falls back to
    _render_no_llm_brief() output, writes it to disk, and does NOT raise.

    Spec ref: llm-timeout/spec.md — Scenario: LLM timeout degrades to no-llm
    output in morning brief.

    Setup mirrors the existing historic regression tests: monkeypatch get_fieldkit_home() to
    return a tmp_path with an accounts/ directory so _run() resolves data_root
    without touching the real config.  All collectors are mocked to return
    deterministic strings so the test is fully isolated (no gmail.db, no
    pursuit files, no subprocess calls).
    """
    from unittest.mock import patch

    from fieldkit.errors import LLMError

    # Minimal data_root layout — _run() only needs the directory to exist so
    # it can create briefs/ and write the output file.
    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    # Patch get_fieldkit_home() so _run() uses our tmp data_root instead of the
    # real ~/.config/fieldkit/config.yaml path.
    monkeypatch.setattr(
        _brief_mod,
        "get_fieldkit_home",
        lambda: data_root,
    )

    # Mock all collectors with deterministic strings so no real I/O occurs.
    with (
        patch.object(_brief_mod, "collect_pursuit_alerts", return_value="No active pursuits flagged today."),
        patch.object(_brief_mod, "collect_champion_signals", return_value="No champion signal data available."),
        patch.object(_brief_mod, "collect_decay_signals", return_value="No decay data available."),
        patch.object(_brief_mod, "collect_stale_prose", return_value="None detected."),
        patch.object(
            _brief_mod, "collect_tasks", return_value=("(nothing committed for today yet)", "(nothing in the queue)")
        ),
        patch.object(_brief_mod, "_collect_degraded_sources", return_value=[]),
        # synthesize raises LLMError to simulate a timeout
        patch.object(_brief_mod, "synthesize", side_effect=LLMError("timed out after 90s")),
    ):
        # historic regression: _run() raises LLMError on failure; cli_main() maps it to exit 1.
        from fieldkit.cli_exit import cli_main

        with pytest.raises(SystemExit) as exc_info, cli_main():
            _run(no_llm=False, account=None)
        assert exc_info.value.code == 1, f"Expected exit code 1, got {exc_info.value.code}"

    # The brief file must have been written to disk
    briefs_dir = data_root / "briefs"
    written = sorted(briefs_dir.glob("morning-brief-*.md"))
    assert written, "Expected at least one morning-brief-*.md file in briefs/"

    content = written[0].read_text(encoding="utf-8")

    # The fallback brief must contain the no-llm section header
    assert "## Pursuit Health" in content or "Pursuit Alerts" in content
    # historic regression: the degraded brief must contain the DEGRADED marker
    assert "[DEGRADED]" in content


# ── --verbose flag ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_verbose_flag_prints_collector_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--verbose prints [brief] collect_... timing lines to stderr.

    Spec ref: brief-verbose/spec.md — Scenario: verbose flag prints timing to stderr.

    CliRunner uses mix_stderr=True by default, so stderr lines appear in
    result.output alongside stdout.  All collectors are mocked to return
    deterministic strings so the test is fully isolated.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.brief.cli import cli

    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    monkeypatch.setattr(
        _brief_mod,
        "get_fieldkit_home",
        lambda: data_root,
    )

    # Click 8.2+ mixes stdout and stderr into result.output automatically;
    # result.stderr is also available for stderr-only assertions.
    runner = CliRunner()
    with (
        patch.object(_brief_mod, "collect_pursuit_alerts", return_value="No active pursuits flagged today."),
        patch.object(_brief_mod, "collect_champion_signals", return_value="No champion signal data available."),
        patch.object(_brief_mod, "collect_decay_signals", return_value="No decay data available."),
        patch.object(_brief_mod, "collect_stale_prose", return_value="None detected."),
        patch.object(
            _brief_mod,
            "collect_tasks",
            return_value=("(nothing committed for today yet)", "(nothing in the queue)"),
        ),
        patch.object(_brief_mod, "_collect_degraded_sources", return_value=[]),
    ):
        result = runner.invoke(cli, ["generate", "--pipeline-only", "--verbose", "--no-llm"], catch_exceptions=False)

    assert result.exit_code == 0, f"Unexpected exit code {result.exit_code}:\n{result.output}"
    # result.output mixes stdout+stderr (Click 8.2+); result.stderr is stderr-only
    assert "[brief] collect_" in result.output, (
        "Expected at least one '[brief] collect_...' timing line in output, got:\n" + result.output
    )


@pytest.mark.unit
def test_no_verbose_flag_no_timing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --verbose, no [brief] collect_ timing lines appear.

    Spec ref: brief-verbose/spec.md — Scenario: no --verbose flag produces no
    timing output.  The existing '[morning_brief] Collecting data...' line is
    still emitted but the per-collector '[brief] collect_...' lines must not be.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.brief.cli import cli

    data_root = tmp_path / "data"
    (data_root / "accounts").mkdir(parents=True)

    monkeypatch.setattr(
        _brief_mod,
        "get_fieldkit_home",
        lambda: data_root,
    )

    # Click 8.2+ mixes stdout and stderr into result.output automatically;
    # result.stderr is also available for stderr-only assertions.
    runner = CliRunner()
    with (
        patch.object(_brief_mod, "collect_pursuit_alerts", return_value="No active pursuits flagged today."),
        patch.object(_brief_mod, "collect_champion_signals", return_value="No champion signal data available."),
        patch.object(_brief_mod, "collect_decay_signals", return_value="No decay data available."),
        patch.object(_brief_mod, "collect_stale_prose", return_value="None detected."),
        patch.object(
            _brief_mod,
            "collect_tasks",
            return_value=("(nothing committed for today yet)", "(nothing in the queue)"),
        ),
        patch.object(_brief_mod, "_collect_degraded_sources", return_value=[]),
    ):
        result = runner.invoke(cli, ["generate", "--pipeline-only", "--no-llm"], catch_exceptions=False)

    assert result.exit_code == 0, f"Unexpected exit code {result.exit_code}:\n{result.output}"
    # result.output mixes stdout+stderr (Click 8.2+); timing lines must be absent
    assert "[brief] collect_" not in result.output, (
        "Expected no '[brief] collect_...' timing lines without --verbose, got:\n" + result.output
    )


# ── Task 4 — morning brief alert lookback extended to 3 days ─────────────────


@pytest.mark.unit
def test_collect_alert_source_includes_alert_two_days_ago(tmp_path: Path) -> None:
    """_collect_alert_source includes alert blocks dated 2 days ago (lookback=3).

    Task 4 changed _collect_alert_source to pass lookback_days=3 to
    extract_today_alerts.  An alert block whose heading date is 2 days before
    target_date must be returned in the results.
    """
    from datetime import date, timedelta

    from fieldkit.watch.morning_brief import _collect_alert_source

    target_date = date(2026, 6, 10)
    two_days_ago = target_date - timedelta(days=2)  # 2026-06-08

    alert_file = tmp_path / "backstory-alerts.md"
    alert_file.write_text(
        f"## {two_days_ago.isoformat()}\n\nOld but recent alert body.\n",
        encoding="utf-8",
    )

    result = _collect_alert_source(alert_file, target_date, "Test")

    assert isinstance(result, list), f"Expected list, got: {result!r}"
    assert len(result) == 1, f"Expected 1 block (2-day-old alert), got {len(result)}: {result}"
    assert two_days_ago.isoformat() in result[0]


@pytest.mark.unit
def test_collect_alert_source_excludes_alert_four_days_ago(tmp_path: Path) -> None:
    """_collect_alert_source excludes alert blocks dated 4 days ago (lookback=3).

    A block whose heading date is 4 days before target_date falls outside the
    [today-3, today] window and must NOT appear in the output.
    """
    from datetime import date, timedelta

    from fieldkit.watch.morning_brief import _collect_alert_source

    target_date = date(2026, 6, 10)
    four_days_ago = target_date - timedelta(days=4)  # 2026-06-06

    alert_file = tmp_path / "backstory-alerts.md"
    alert_file.write_text(
        f"## {four_days_ago.isoformat()}\n\nStale alert body — should be excluded.\n",
        encoding="utf-8",
    )

    result = _collect_alert_source(alert_file, target_date, "Test")

    assert isinstance(result, list), f"Expected list, got: {result!r}"
    assert len(result) == 0, f"Expected 0 blocks (4-day-old alert excluded), got {len(result)}: {result}"


# ── collect_pipeline_pulse (implementation change) ─────────────────────────────────────────


def _make_pursuit_file(
    pursuits_dir: Path,
    name: str,
    stage: str = "discover",
    meddpicc_total: int = 16,
    sf_close_date: str | None = None,
) -> Path:
    """Write a minimal valid pursuit file for pipeline pulse tests."""
    # Build MEDDPICC block: distribute meddpicc_total across 8 elements (0-3 each)
    per_elem = min(3, meddpicc_total // 8)
    remainder = meddpicc_total - per_elem * 8
    elems = {
        "metrics": per_elem,
        "economic-buyer": per_elem,
        "decision-criteria": per_elem,
        "decision-process": per_elem,
        "identify-pain": per_elem,
        "champion": per_elem + (1 if remainder > 0 else 0),
        "competition": per_elem + (1 if remainder > 1 else 0),
        "paper-process": per_elem + (1 if remainder > 2 else 0),
    }
    meddpicc_block = "\n".join(f"  {k}: {v}" for k, v in elems.items())
    close_date_line = f"sf_close_date: {sf_close_date}" if sf_close_date else ""
    content = f"""\
---
stage: {stage}
gate-status: pending
last-transition: 2026-05-01
transition-history:
  - date: 2026-05-01
    from: ""
    to: {stage}
    gate-result: pass
meddpicc:
{meddpicc_block}
sf_opportunity_id: "006FAKE{name}"
sf_stage: Discover
{close_date_line}
sf_arr: 100000.0
sf_owner: Jane Doe
sf_next_steps: Follow up
sf_last_pulled: 2026-05-14T17:35:36Z
---

# {name}
"""
    p = pursuits_dir / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.mark.unit
def test_pipeline_pulse_red_deal_sorts_before_higher_meddpicc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """implementation change: A pursuit with close date ≤30 days sorts before higher-MEDDPICC non-RED pursuits.

    Setup: 6 pursuits — 5 with distant close dates and high MEDDPICC, 1 with
    close date 15 days away and lower MEDDPICC. The RED pursuit must appear
    first in the output.
    """
    from datetime import date, timedelta

    from fieldkit.commands.brief.collect import collect_pipeline_pulse

    data_root = tmp_path / "data"
    pursuits_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    pursuits_dir.mkdir(parents=True)

    today = date.today()
    red_close = (today + timedelta(days=15)).strftime("%Y-%m-%d")
    far_close = (today + timedelta(days=180)).strftime("%Y-%m-%d")

    # 5 non-RED pursuits with high MEDDPICC (score=20)
    for i in range(5):
        _make_pursuit_file(pursuits_dir, f"deal-high-{i}", meddpicc_total=20, sf_close_date=far_close)

    # 1 RED pursuit with lower MEDDPICC (score=12)
    _make_pursuit_file(pursuits_dir, "deal-red", meddpicc_total=12, sf_close_date=red_close)

    result = collect_pipeline_pulse(data_root)

    # The RED pursuit must appear in the output
    assert "deal-red" in result, f"Expected 'deal-red' in pulse output:\n{result}"
    # The RED pursuit must appear before any non-RED pursuit
    red_pos = result.index("deal-red")
    for i in range(5):
        if f"deal-high-{i}" in result:
            high_pos = result.index(f"deal-high-{i}")
            assert red_pos < high_pos, f"Expected RED deal before high-MEDDPICC deal, got:\n{result}"


@pytest.mark.unit
def test_pipeline_pulse_returns_at_most_five_pursuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """implementation change: collect_pipeline_pulse returns at most 5 pursuits even with 8 active."""
    from fieldkit.commands.brief.collect import collect_pipeline_pulse

    data_root = tmp_path / "data"
    pursuits_dir = data_root / "accounts" / "acme-corp" / "pursuits"
    pursuits_dir.mkdir(parents=True)

    # Create 8 active pursuits
    for i in range(8):
        _make_pursuit_file(pursuits_dir, f"deal-{i}", meddpicc_total=16)

    result = collect_pipeline_pulse(data_root)

    # Count numbered lines (each pursuit is "N. **account / name**...")
    numbered_lines = [line for line in result.splitlines() if line and line[0].isdigit() and ". " in line]
    assert len(numbered_lines) <= 5, f"Expected at most 5 pursuits in pulse, got {len(numbered_lines)}:\n{result}"
