from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.brief import pipeline_only as brief_main
from fieldkit.commands.brief.cli import cli

BRIEF_SKILL = Path("src/fieldkit/skills/brief")


def _read(relative_path: str) -> str:
    return (BRIEF_SKILL / relative_path).read_text(encoding="utf-8")


@pytest.mark.unit
def test_documented_local_preview_uses_fixed_argv_without_network_llm_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(brief_main, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(brief_main, "get_config_path", lambda name: tmp_path / "config" / name)
    monkeypatch.setattr(brief_main, "collect_pursuit_alerts", lambda _root, _account: "None detected.")
    monkeypatch.setattr(brief_main, "collect_champion_signals", lambda _root, _account: "None detected.")
    monkeypatch.setattr(brief_main, "collect_decay_signals", lambda _root, _account: "None detected.")
    monkeypatch.setattr(brief_main, "collect_stale_prose", lambda _root: "None detected.")
    monkeypatch.setattr(brief_main, "collect_tasks", lambda _root: ("None.", "None."))
    monkeypatch.setattr(brief_main, "synthesize", lambda *_args, **_kwargs: pytest.fail("LLM must not run"))

    result = CliRunner().invoke(
        cli,
        ["generate", "--pipeline-only", "--no-llm", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "Morning Brief" in result.output
    assert not (tmp_path / "briefs").exists()


@pytest.mark.unit
def test_daily_brief_documents_safe_preview_and_save_boundaries() -> None:
    content = _read("SKILL.md")

    assert "fieldkit brief generate --pipeline-only --no-llm --dry-run" in content
    assert "fieldkit brief generate --dry-run" in content
    assert "does not provide a lookback option" in content
    assert "does not create a historical source snapshot" in content
    assert "not a privacy-isolation boundary" in content
    assert "pipeline review still calculates against the actual run date" in content
    assert "same dated path" in content
    assert "explicit overwrite approval" in content
    assert "does not contact an LLM or live service" in content
    assert "does not request\ncalendar or LLM data" in content
    assert "configured calendar collection can still contact" not in content


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsupported_claim",
    [
        "brief.json",
        "--fresh",
        "--no-refresh",
        "tools/brief",
        "fieldkit-sales",
        "gws tasks",
        "slackcli",
        "tvly search",
    ],
)
def test_brief_skill_does_not_require_private_or_removed_routes(unsupported_claim: str) -> None:
    content = "\n".join(path.read_text(encoding="utf-8") for path in sorted(BRIEF_SKILL.rglob("*.md")))

    assert unsupported_claim not in content


@pytest.mark.unit
def test_weekly_and_comprehensive_workflows_preview_writes_and_bound_queries() -> None:
    week_start = _read("ops/week-start.md")
    week_end = _read("ops/week-end.md")
    comprehensive = _read("references/comprehensive-scan.md")

    assert "fieldkit sync --sf --dry-run" in week_start
    assert "fieldkit pipeline --no-llm" in week_start
    assert "fieldkit pursuit audit --json" in week_end
    assert "fieldkit gmail query account acme-corp --since YYYY-MM-DD --limit 10 --json" in comprehensive
    assert "fieldkit gmail decay --account acme-corp --limit 10 --json" in comprehensive
    assert "fieldkit gmail query blindspots acme-corp --since YYYY-MM-DD --limit 10 --json" in comprehensive


@pytest.mark.unit
def test_comprehensive_scan_requires_published_cache_without_query_migration() -> None:
    content = _read("references/comprehensive-scan.md")

    assert "ready published Gmail cache" in content
    assert "do not migrate a legacy database or create query indexes" in content
    assert "fieldkit gmail import-cache" in content
    assert "Query setup can prepare local indexes" not in content


@pytest.mark.unit
def test_mid_session_update_distinguishes_baseline_coverage_and_approved_writes() -> None:
    content = _read("ops/update.md")

    assert "describe current state rather than claiming a change" in content
    assert "Empty complete results and unavailable results are different outcomes" in content
    assert "an isolated cache for a rehearsal" in content
    assert "Agree on each write's" in content
    assert "complete remote read-back" in content
