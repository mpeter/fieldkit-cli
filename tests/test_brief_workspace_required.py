"""Brief generation requires a workspace before collecting or synthesizing data."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import fieldkit.brief.pipeline_only as brief_main
from fieldkit.commands.brief.cli import cli
from fieldkit.config import ConfigError, _loader, clear_config_caches


@pytest.mark.unit
def test_pipeline_only_domain_returns_typed_preview_without_publishing(tmp_path: Path) -> None:
    from fieldkit.brief.pipeline_only import PipelineOnlyResult, generate_pipeline_only

    with (
        patch("fieldkit.brief.pipeline_only.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.brief.pipeline_only.collect_pursuit_alerts", return_value="alerts"),
        patch("fieldkit.brief.pipeline_only.collect_champion_signals", return_value="signals"),
        patch("fieldkit.brief.pipeline_only.collect_decay_signals", return_value="decay"),
        patch("fieldkit.brief.pipeline_only.collect_stale_prose", return_value="none"),
        patch("fieldkit.brief.pipeline_only.collect_tasks", return_value=("today", "waiting")),
        patch("fieldkit.brief.pipeline_only._collect_degraded_sources", return_value=[]),
        patch("fieldkit.brief.pipeline_only.get_llm_model", return_value="vertex_ai/test-model"),
        patch("fieldkit.brief.pipeline_only.synthesize", side_effect=AssertionError("provider called")) as provider,
    ):
        result = generate_pipeline_only(no_llm=False, account=None, dry_run=True)

    assert isinstance(result, PipelineOnlyResult)
    assert result.dry_run is True
    assert result.provider_failure is None
    assert "alerts" in result.text
    assert not (tmp_path / "briefs").exists()
    provider.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("no_llm", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_workspace_stops_before_side_effects(no_llm: bool, dry_run: bool) -> None:
    with (
        patch.object(brief_main, "get_fieldkit_home", side_effect=ConfigError("no workspace")),
        patch.object(brief_main, "synthesize", return_value="# Brief") as synthesize,
        patch.object(brief_main, "collect_pursuit_alerts") as collect,
        patch.object(brief_main, "_persist_brief") as persist,
        pytest.raises(ConfigError, match="fieldkit init"),
    ):
        brief_main.generate_pipeline_only(no_llm=no_llm, account=None, dry_run=dry_run)

    synthesize.assert_not_called()
    collect.assert_not_called()
    persist.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize("no_llm", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_config_cli_exits_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_llm: bool, dry_run: bool
) -> None:
    monkeypatch.setattr(_loader, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.chdir(tmp_path)
    clear_config_caches()
    args = ["generate", "--pipeline-only"]
    if no_llm:
        args.append("--no-llm")
    if dry_run:
        args.append("--dry-run")

    with patch.object(brief_main, "synthesize", return_value="# Brief") as synthesize:
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 3, result.output
    assert "fieldkit init" in result.output
    assert "Brief saved" not in result.output
    synthesize.assert_not_called()
    assert list(tmp_path.iterdir()) == []
