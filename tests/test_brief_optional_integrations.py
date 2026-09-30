"""CLI contracts for the local-first assembled morning brief."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.brief.cli import cli
from fieldkit.watch.integration_plan import IntegrationPlan
from fieldkit.watch.status import WatcherRunResult

pytestmark = pytest.mark.unit


def test_generate_without_optional_services_uses_local_no_llm_path() -> None:
    plan = IntegrationPlan((), (), False, False, ("calendar: not configured", "llm: not configured"))
    with (
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]) as preflight,
        patch(
            "fieldkit.commands.brief.cli._run_generate", return_value=WatcherRunResult("ok", True, "written")
        ) as generate,
    ):
        result = CliRunner().invoke(cli, ["generate"])

    assert result.exit_code == 0
    preflight.assert_called_once_with([], dry_run=False)
    assert generate.call_args.kwargs["no_llm"] is True
    assert generate.call_args.kwargs["calendar_enabled"] is False


def test_generate_preflights_only_configured_llm() -> None:
    plan = IntegrationPlan(("llm",), (), True, True, ())
    with (
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]) as preflight,
        patch(
            "fieldkit.commands.brief.cli._run_generate", return_value=WatcherRunResult("ok", True, "written")
        ) as generate,
    ):
        result = CliRunner().invoke(cli, ["generate"])

    assert result.exit_code == 0
    preflight.assert_called_once_with(["llm"], dry_run=False)
    assert generate.call_args.kwargs["no_llm"] is False
    assert generate.call_args.kwargs["calendar_enabled"] is True


def test_generate_dry_run_performs_no_preflight() -> None:
    plan = IntegrationPlan(("llm",), (), True, True, ())
    with (
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.preflight.preflight_check") as preflight,
        patch(
            "fieldkit.commands.brief.cli._run_generate", return_value=WatcherRunResult("ok", True, "written")
        ) as generate,
    ):
        result = CliRunner().invoke(cli, ["generate", "--dry-run"])

    assert result.exit_code == 0
    preflight.assert_not_called()
    assert generate.call_args.kwargs["no_llm"] is False


def test_generate_selected_provider_preflight_failure_maps_to_auth_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.__main__ import main

    plan = IntegrationPlan(("llm",), (), False, True, ())
    with (
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.preflight.preflight_check", return_value=["credential unavailable"]),
    ):
        exit_code = main(["brief", "generate"])

    assert exit_code == 2
    assert "Auth error" in capsys.readouterr().err
