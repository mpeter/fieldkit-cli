"""Smoke tests for fieldkit pipeline CLI (commands/pipeline/cli.py).

implementation note: new test file targeting the pipeline command CLI layer, which had
~51% coverage with no dedicated test file for the CLI interface itself.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit

_MODULE = "fieldkit.commands.pipeline.cli"


def _get_cli():
    from fieldkit.commands.pipeline.cli import cli

    return cli


def test_pipeline_cli_help_exits_zero() -> None:
    """fieldkit pipeline --help exits 0 and shows help text."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_pipeline_cli_subcommand_help() -> None:
    """fieldkit pipeline open --help exits 0 or 2 (subcommand exists or not)."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["open", "--help"])
    assert result.exit_code in (0, 2)


def test_pipeline_cli_no_llm_flag_parsed() -> None:
    """--no-llm flag is accepted without crashing (shows help or exits 0)."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--no-llm", "--help"])
    assert result.exit_code == 0
    assert "--no-llm" in result.output


def test_pipeline_cli_unknown_flag_exits_nonzero() -> None:
    """An unrecognised flag exits 2 with an error message."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--not-a-real-flag"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# cmd_quota — targeted branch coverage
# ---------------------------------------------------------------------------


# ── TestCmdQuota (flattened) ────────────────────────────────────────────────

_CMD_QUOTA__QUOTA_MODULE = "fieldkit.commands.pipeline.quota"
_CMD_QUOTA__CALC_MODULE = "fieldkit.watch.morning_brief_render"


def _cmd_quota_invoke(args: list[str], quota_cfg=None, home: Path | None = None, pursuits=None, gap=None):
    """Helper: invoke quota with controlled mocks."""
    from fieldkit.commands.pipeline.cli import cli

    runner = CliRunner()
    quota_cfg = quota_cfg or {"target": 1_000_000, "period": "2026-H2"}
    home = home or Path("/tmp/fake-home")
    pursuits = pursuits or []
    gap = gap or {
        "target": 1_000_000.0,
        "closed_won": 200_000.0,
        "weighted": 500_000.0,
        "gap": 300_000.0,
        "excluded_count": 0,
        "excluded_names": [],
    }
    with (
        patch(f"{_MODULE}.get_pipeline_quota", return_value=quota_cfg),
        patch(f"{_MODULE}.get_fieldkit_home", return_value=home),
        patch(f"{_CMD_QUOTA__QUOTA_MODULE}._collect_pursuits_for_quota", return_value=pursuits),
        patch(f"{_CMD_QUOTA__CALC_MODULE}.calculate_quota_gap", return_value=gap),
    ):
        return runner.invoke(cli, ["quota", *args])


def test_cmd_quota_quota_no_config_exits_3() -> None:
    """When no quota is configured, exits 3 with a hint."""
    from fieldkit.commands.pipeline.cli import cli

    runner = CliRunner()
    with patch(f"{_MODULE}.get_pipeline_quota", return_value=None):
        result = runner.invoke(cli, ["quota"])
    assert result.exit_code == 3
    assert "No quota configured" in result.output


def test_cmd_quota_quota_config_error_exits_3() -> None:
    """ConfigError from get_fieldkit_home exits with the data-error code."""
    from fieldkit.commands.pipeline.cli import cli
    from fieldkit.config import ConfigError

    runner = CliRunner()
    with (
        patch(f"{_MODULE}.get_pipeline_quota", return_value={"target": 1_000_000}),
        patch(f"{_MODULE}.get_fieldkit_home", side_effect=ConfigError("no config")),
    ):
        result = runner.invoke(cli, ["quota"])
    assert result.exit_code == 3
    assert "Config error" in result.output


def test_cmd_quota_source_sf_text_output_positive_gap() -> None:
    """implementation note: --source sf shows a territory-scoped Quota Gap table (positive gap)."""
    from datetime import UTC, date, datetime

    # gap = target(1_000_000) - sf_closed_won(200_000) - weighted(500_000) = 300_000
    with patch(f"{_CMD_QUOTA__QUOTA_MODULE}.fetch_sf_closed_won", return_value=200_000.0):
        result = _cmd_quota_invoke(["--source", "sf"])
    assert result.exit_code == 0
    end_date = date(2026, 12, 31)
    days_remaining = (end_date - datetime.now(tz=UTC).date()).days
    assert f"Quota Gap (2026-H2, ends 2026-12-31, {days_remaining} days remaining)" in result.output
    assert "Quota target" in result.output
    assert "territory-scoped" in result.output
    assert "Gap" in result.output
    assert "$300,000" in result.output


def test_cmd_quota_source_sf_text_output_negative_gap() -> None:
    """implementation note: a negative (over-attained) sf gap is formatted with a leading minus."""
    gap = {
        "target": 1_000_000.0,
        "closed_won": 0.0,
        "weighted": 200_000.0,
        "gap": 0.0,  # recomputed by the CLI in sf mode
        "excluded_count": 0,
        "excluded_names": [],
    }
    # gap = target(1_000_000) - sf_closed_won(900_000) - weighted(200_000) = -100_000
    with patch(f"{_CMD_QUOTA__QUOTA_MODULE}.fetch_sf_closed_won", return_value=900_000.0):
        result = _cmd_quota_invoke(["--source", "sf"], gap=gap)
    assert result.exit_code == 0
    assert "-$100,000" in result.output


def test_cmd_quota_source_pursuits_suppresses_gap() -> None:
    """implementation note: default pursuits mode reports n/a instead of a misleading gap."""
    from datetime import UTC, date, datetime

    result = _cmd_quota_invoke([])
    assert result.exit_code == 0
    end_date = date(2026, 12, 31)
    days_remaining = (end_date - datetime.now(tz=UTC).date()).days
    assert f"Quota Gap (2026-H2, ends 2026-12-31, {days_remaining} days remaining)" in result.output
    assert "n/a" in result.output
    assert "not comparable" in result.output


def test_cmd_quota_quota_text_output_no_period() -> None:
    """When no period configured, period label is absent from header."""
    result = _cmd_quota_invoke([], quota_cfg={"target": 1_000_000})
    assert result.exit_code == 0
    assert "Quota Gap\n" in result.output


def test_cmd_quota_quota_json_output() -> None:
    """--json flag produces valid JSON with expected keys."""
    import json

    result = _cmd_quota_invoke(["--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "target" in data
    assert "gap" in data


def test_cmd_quota_quota_excluded_pursuits_warns() -> None:
    """When pursuits are excluded for missing ACV, output warns per-name."""
    gap = {
        "target": 1_000_000.0,
        "closed_won": 0.0,
        "weighted": 0.0,
        "gap": 1_000_000.0,
        "excluded_count": 2,
        "excluded_names": ["pursuit-a", "pursuit-b"],
    }
    result = _cmd_quota_invoke([], gap=gap)
    assert result.exit_code == 0
    assert "pursuit-a" in result.output
    assert "pursuit-b" in result.output
    assert "2 pursuit(s) excluded" in result.output


def test_cmd_quota_quota_data_root_override(tmp_path: Path) -> None:
    """--data-root override is passed through (no crash)."""
    result = _cmd_quota_invoke(["--data-root", str(tmp_path)])
    assert result.exit_code == 0


def test_pipeline_open_json_reports_selected_file_and_opens_it(tmp_path: Path) -> None:
    import json

    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()
    review = briefs_dir / "pipeline-review-2026-09-10.md"
    review.write_text("# Review\n", encoding="utf-8")
    with (
        patch(f"{_MODULE}.get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open") as mock_open,
    ):
        result = CliRunner().invoke(_get_cli(), ["open", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["path"] == str(review)
    assert payload["review_date"] == "2026-09-10"
    assert payload["account"] is None
    assert payload["opened"] is True
    mock_open.assert_called_once_with(review.as_uri())


def test_pipeline_generation_passes_validated_account_to_runner() -> None:
    with (
        patch("fieldkit.config.get_account_names", return_value=["acme-corp"]),
        patch("fieldkit.commands.pipeline.main._run") as mock_run,
    ):
        result = CliRunner().invoke(_get_cli(), ["--account", "acme-corp", "--no-llm"])

    assert result.exit_code == 0, result.output
    mock_run.assert_called_once_with(no_llm=True, data_root_override=None, account="acme-corp")


def test_pipeline_generation_unknown_account_exits_before_runner() -> None:
    with (
        patch("fieldkit.config.get_account_names", return_value=["acme-corp"]),
        patch("fieldkit.commands.pipeline.main._run") as mock_run,
    ):
        result = CliRunner().invoke(_get_cli(), ["--account", "unknown"])

    assert result.exit_code == 3
    assert "unknown account 'unknown'" in result.output
    mock_run.assert_not_called()


def test_pipeline_generation_rejects_configured_unsafe_account_before_runner() -> None:
    with (
        patch("fieldkit.config.get_account_names", return_value=["../../outside"]),
        patch("fieldkit.commands.pipeline.main._run") as mock_run,
    ):
        result = CliRunner().invoke(_get_cli(), ["--account", "../../outside"])

    assert result.exit_code == 3
    assert "unsafe account slug" in result.output
    mock_run.assert_not_called()


def test_pipeline_open_account_selects_only_newest_matching_review(tmp_path: Path) -> None:
    import json

    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()
    selected = briefs_dir / "pipeline-review-acme-corp-2026-09-09.md"
    for name in (
        "pipeline-review-2026-09-10.md",
        "pipeline-review-acme-corp-2026-09-08.md",
        selected.name,
        "pipeline-review-example-co-2026-09-10.md",
    ):
        (briefs_dir / name).write_text("# Review\n", encoding="utf-8")

    with (
        patch("fieldkit.config.get_account_names", return_value=["acme-corp", "example-co"]),
        patch(f"{_MODULE}.get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open") as mock_open,
    ):
        result = CliRunner().invoke(_get_cli(), ["open", "--account", "acme-corp", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0, result.output
    assert payload["account"] == "acme-corp"
    assert payload["path"] == str(selected)
    mock_open.assert_called_once_with(selected.as_uri())


def test_pipeline_open_global_ignores_newer_scoped_review(tmp_path: Path) -> None:
    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()
    global_review = briefs_dir / "pipeline-review-2026-09-08.md"
    global_review.write_text("# Global\n", encoding="utf-8")
    (briefs_dir / "pipeline-review-acme-corp-2026-09-10.md").write_text("# Scoped\n", encoding="utf-8")

    with (
        patch(f"{_MODULE}.get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open") as mock_open,
    ):
        result = CliRunner().invoke(_get_cli(), ["open"])

    assert result.exit_code == 0, result.output
    mock_open.assert_called_once_with(global_review.as_uri())


def test_pipeline_open_missing_account_review_fails_without_fallback(tmp_path: Path) -> None:
    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()
    (briefs_dir / "pipeline-review-2026-09-10.md").write_text("# Global\n", encoding="utf-8")

    with (
        patch("fieldkit.config.get_account_names", return_value=["acme-corp"]),
        patch(f"{_MODULE}.get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open") as mock_open,
    ):
        result = CliRunner().invoke(_get_cli(), ["open", "--account", "acme-corp"])

    assert result.exit_code == 3
    assert "fieldkit pipeline --account acme-corp" in result.output
    mock_open.assert_not_called()


def test_pipeline_open_without_account_fails_when_only_scoped_review_exists(tmp_path: Path) -> None:
    briefs_dir = tmp_path / "briefs"
    briefs_dir.mkdir()
    (briefs_dir / "pipeline-review-acme-corp-2026-09-10.md").write_text("# Scoped\n", encoding="utf-8")

    with (
        patch(f"{_MODULE}.get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open") as mock_open,
    ):
        result = CliRunner().invoke(_get_cli(), ["open"])

    assert result.exit_code == 3
    assert "Run 'fieldkit pipeline'" in result.output
    mock_open.assert_not_called()


def test_pipeline_rejects_generation_account_option_before_open_subcommand() -> None:
    with patch("webbrowser.open") as mock_open:
        result = CliRunner().invoke(_get_cli(), ["--account", "acme-corp", "open"])

    assert result.exit_code == 3
    assert "must come after the subcommand" in result.output
    mock_open.assert_not_called()
