"""Artifact publication and overall brief success are separate outcomes."""

import json
from datetime import date
from pathlib import Path

import pytest

import fieldkit.brief.merged as merged
import fieldkit.commands.brief.generate as generate
import fieldkit.watch.morning_brief as morning
import fieldkit.watch.morning_brief_render as render
import fieldkit.watch.status as status
from fieldkit.config import ConfigError
from fieldkit.errors import AuthError, EmptyOutputError
from fieldkit.watch._morning_brief_types import SourceNotReady
from fieldkit.watch.mcp import MCPAuthError
from fieldkit.watch.status import write_run_status

pytestmark = pytest.mark.unit
_DATE = date(2026, 4, 2)
_CONTENT = "# Morning brief\n\nLocal report.\n"


@pytest.mark.parametrize("phase", ["initialize", "fetch"])
@pytest.mark.parametrize("error_type", [AuthError, MCPAuthError, ConfigError])
@pytest.mark.parametrize("via_cli", [False, True])
def test_calendar_auth_preserves_report_and_propagates_through_generator(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error_type: type[Exception],
    via_cli: bool,
) -> None:
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.brief.cli import cli
    from fieldkit.watch.integration_plan import IntegrationPlan

    output_dir = workspace / "briefs"
    output_dir.mkdir()
    target = output_dir / "morning-brief-2026-04-02.md"
    target.write_text("Prior report\n", encoding="utf-8")
    monkeypatch.setattr(merged, "get_fieldkit_home", lambda: workspace)
    monkeypatch.setattr(merged, "get_accounts_config", lambda: {"accounts": {}})
    monkeypatch.setattr(merged, "get_mcp_endpoint", lambda _name: None)
    monkeypatch.setattr(merged, "_collect_alert_source", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(merged, "resolve_user_email", lambda _config: "user@example.com")
    monkeypatch.setattr(morning, "get_mcp_endpoint", lambda _name: "https://calendar.example.com/mcp")
    monkeypatch.setattr(morning, "write_run_status", lambda **_kwargs: pytest.fail("status attempted"))
    monkeypatch.setattr(merged, "_write_brief_to_disk", lambda *_args, **_kwargs: pytest.fail("publication attempted"))
    error = error_type("Synthetic credential rejection")
    with (
        patch.object(morning, "MCPSession") as session,
        patch.object(morning, "fetch_external_meetings") as fetch,
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch(
            "fieldkit.watch.integration_plan.build_integration_plan",
            return_value=IntegrationPlan((), (), True, False, ()),
        ),
    ):
        operation = session.return_value.initialize if phase == "initialize" else fetch
        operation.side_effect = error
        if via_cli:
            result = CliRunner().invoke(cli, ["generate", "--date", _DATE.isoformat()])
            assert result.exit_code == (2 if isinstance(error, AuthError) else 3)
        else:
            with pytest.raises(error_type, match="Synthetic credential rejection") as captured:
                generate._run_generate_inner(date_str=_DATE.isoformat(), dry_run=False, verbose=False)
            assert captured.value is error
    session.return_value.close.assert_called_once_with()
    assert target.read_text(encoding="utf-8") == "Prior report\n"


@pytest.mark.parametrize("phase", ["collect", "render"])
@pytest.mark.parametrize("error_type", [AuthError, ConfigError])
@pytest.mark.parametrize("via_cli", [False, True])
def test_pipeline_user_action_errors_propagate_without_publication(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error_type: type[Exception],
    via_cli: bool,
) -> None:
    from unittest.mock import patch

    from click.testing import CliRunner

    from fieldkit.commands.brief.cli import cli
    from fieldkit.watch.integration_plan import IntegrationPlan

    output_dir = workspace / "briefs"
    output_dir.mkdir()
    target = output_dir / "morning-brief-2026-04-02.md"
    target.write_text("Prior report\n", encoding="utf-8")
    monkeypatch.setattr(merged, "get_fieldkit_home", lambda: workspace)
    monkeypatch.setattr(merged, "get_accounts_config", lambda: {"accounts": {}})
    monkeypatch.setattr(merged, "get_mcp_endpoint", lambda _name: None)
    monkeypatch.setattr(merged, "_collect_alert_source", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(merged, "_collect_calendar_meetings", lambda *_args, **_kwargs: SourceNotReady("Absent"))
    monkeypatch.setattr(merged, "resolve_user_email", lambda _config: "user@example.com")
    monkeypatch.setattr(morning, "write_run_status", lambda **_kwargs: pytest.fail("status attempted"))
    monkeypatch.setattr(merged, "_write_brief_to_disk", lambda *_args, **_kwargs: pytest.fail("publication attempted"))
    error = error_type("Synthetic user action required")
    with (
        patch.object(merged, "collect_all_pursuit_data", return_value=([], {}, {})) as collect,
        patch.object(merged, "render_full_brief") as renderer,
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch(
            "fieldkit.watch.integration_plan.build_integration_plan",
            return_value=IntegrationPlan((), (), False, False, ()),
        ),
    ):
        operation = collect if phase == "collect" else renderer
        operation.side_effect = error
        if via_cli:
            result = CliRunner().invoke(cli, ["generate", "--date", _DATE.isoformat()])
            assert result.exit_code == (2 if isinstance(error, AuthError) else 3)
        else:
            with pytest.raises(error_type, match="Synthetic user action required") as captured:
                generate._run_generate_inner(date_str=_DATE.isoformat(), dry_run=False, verbose=False, no_llm=True)
            assert captured.value is error
    assert target.read_text(encoding="utf-8") == "Prior report\n"


@pytest.mark.parametrize("content", ["", " \n\t"])
def test_empty_render_preserves_previous_report_without_status_attempt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    output_dir = workspace / "briefs"
    output_dir.mkdir()
    target = output_dir / "morning-brief-2026-04-02.md"
    target.write_text("Prior report\n", encoding="utf-8")
    monkeypatch.setattr(morning, "write_run_status", lambda **_kwargs: pytest.fail("status attempted"))
    with pytest.raises(EmptyOutputError, match="Empty output detected"):
        morning._write_brief_to_disk(content, _DATE, 0.1, [], dry_run=False, output_dir=output_dir)
    assert target.read_text(encoding="utf-8") == "Prior report\n"


@pytest.mark.parametrize("status_result", ["written", "failed"])
def test_artifact_failure_counts_sources_and_actual_status_attempt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, status_result: str
) -> None:
    def fail_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("Synthetic artifact failure")

    monkeypatch.setattr(morning, "atomic_text_write", fail_publication)
    monkeypatch.setattr(morning, "write_run_status", lambda **_kwargs: status_result)
    result = morning._write_brief_to_disk(
        _CONTENT,
        _DATE,
        0.1,
        [[], "Unavailable", SourceNotReady("Absent")],
        dry_run=False,
        output_dir=workspace / "briefs",
    )
    assert result.run.outcome == "fatal"
    assert result.run.status_write == status_result
    assert result.run.completed is False
    assert result.written is False
    assert result.records_checked == 3
    assert result.failures == 2 + int(status_result == "failed")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(morning, "write_run_status", write_run_status)
    return tmp_path


@pytest.mark.parametrize("degraded", [False, True])
def test_written_artifact_retains_source_failure_status(workspace: Path, degraded: bool) -> None:
    result = morning._write_brief_to_disk(
        _CONTENT,
        _DATE,
        0.1,
        ["Source unavailable"] if degraded else [SourceNotReady("Optional input absent")],
        dry_run=False,
        output_dir=workspace / "briefs",
    )

    assert result.written is True
    assert result.run.outcome == ("partial" if degraded else "ok")
    assert result.run.completed is True
    assert result.run.status_write == "written"
    assert result.run.exit_code == int(degraded)
    assert result.records_checked == 1
    assert result.failures == int(degraded)
    assert (workspace / "briefs/morning-brief-2026-04-02.md").read_text(encoding="utf-8") == _CONTENT
    saved = json.loads((workspace / "watchers/watcher-run-status.json").read_text(encoding="utf-8"))
    assert saved["morning-brief"]["outcome"] == ("partial" if degraded else "ok")
    assert saved["morning-brief"]["records_checked"] == result.records_checked
    assert saved["morning-brief"]["alerts_generated"] == result.alerts_generated
    assert saved["morning-brief"]["failures"] == result.failures


def test_real_status_publication_failure_does_not_hide_written_artifact(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (
        write_run_status(
            watcher="morning-brief",
            outcome="ok",
            records_checked=2,
            alerts_generated=1,
            failures=0,
            elapsed_seconds=0.1,
            dry_run=False,
        )
        == "written"
    )
    status_path = workspace / "watchers/watcher-run-status.json"
    prior_status = status_path.read_bytes()
    original_replace = Path.replace

    def fail_status_replace(path: Path, target: str | Path) -> Path:
        if Path(target).name == "watcher-run-status.json":
            raise OSError("Synthetic status publication failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_status_replace)
    result = morning._write_brief_to_disk(_CONTENT, _DATE, 0.1, [], dry_run=False, output_dir=workspace / "briefs")

    assert result.written is True
    assert result.run.outcome == "fatal"
    assert result.run.completed is True
    assert result.run.status_write == "failed"
    assert result.failures == 1
    assert (workspace / "briefs/morning-brief-2026-04-02.md").read_text(encoding="utf-8") == _CONTENT
    assert status_path.read_bytes() == prior_status
    assert "watcher-run-status: write failed" in caplog.messages


def test_failed_artifact_publication_preserves_stale_target_and_reports_not_written(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = workspace / "briefs"
    output_dir.mkdir()
    target = output_dir / "morning-brief-2026-04-02.md"
    target.write_text("Prior report\n", encoding="utf-8")
    original_replace = Path.replace

    def fail_brief_replace(path: Path, destination: str | Path) -> Path:
        if Path(destination) == target:
            raise OSError("Synthetic artifact publication failure")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_brief_replace)
    result = morning._write_brief_to_disk(_CONTENT, _DATE, 0.1, [], dry_run=False, output_dir=output_dir)

    assert result.written is False
    assert result.run.outcome == "fatal"
    assert result.run.completed is False
    assert result.run.status_write == "written"
    assert result.failures == 1
    assert target.read_text(encoding="utf-8") == "Prior report\n"
    assert list(output_dir.iterdir()) == [target]


def test_atomic_artifact_replacement_preserves_existing_mode(workspace: Path) -> None:
    output_dir = workspace / "briefs"
    output_dir.mkdir()
    target = output_dir / "morning-brief-2026-04-02.md"
    target.write_text("Prior report\n", encoding="utf-8")
    target.chmod(0o640)

    result = morning._write_brief_to_disk(_CONTENT, _DATE, 0.1, [], dry_run=False, output_dir=output_dir)

    assert result.written is True
    assert result.run.exit_code == 0
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("failure", ["source", "status", "artifact"])
def test_generator_json_reports_actual_publication_on_partial_exit(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    monkeypatch.setattr(merged, "get_fieldkit_home", lambda: workspace)
    monkeypatch.setattr(render, "get_fieldkit_home", lambda: workspace)
    monkeypatch.setattr(merged, "get_accounts_config", lambda: {"accounts": {}})
    monkeypatch.setattr(merged, "get_mcp_endpoint", lambda _name: None)
    monkeypatch.setattr(merged, "_parse_internal_domains", lambda _config: set())
    monkeypatch.setattr(merged, "resolve_user_email", lambda _config: "user@example.com")
    monkeypatch.setattr(merged, "_collect_alert_source", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(merged, "_collect_pipeline_review", lambda **_kwargs: "# Pipeline review\n")
    monkeypatch.setattr(merged, "detect_cross_account_signals", lambda *_args: [])
    monkeypatch.setattr(merged, "_collect_pursuits_for_quota", lambda _root: [])
    if failure == "source":
        monkeypatch.setattr(
            merged, "_collect_pipeline_review", lambda **_kwargs: "[Pipeline Review] unavailable: retry"
        )
    else:
        original_replace = Path.replace

        def fail_selected_replace(path: Path, target: str | Path) -> Path:
            destination = Path(target)
            if (failure == "status" and destination.name == "watcher-run-status.json") or (
                failure == "artifact" and destination.name == "morning-brief-2026-04-02.md"
            ):
                raise OSError("Synthetic selected publication failure")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_selected_replace)

    result = generate._run_generate_inner(
        date_str=_DATE.isoformat(),
        dry_run=False,
        verbose=False,
        as_json=True,
        no_llm=True,
        calendar_enabled=False,
    )

    assert result.outcome == ("partial" if failure == "source" else "fatal")
    assert result.exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is (failure != "artifact")
    assert payload["dry_run"] is False
    assert payload["records_checked"] == 7
    assert payload["failures"] == 1
    assert Path(payload["path"]).is_file() is (failure != "artifact")
