"""Tests for fieldkit.commands.datasync — ordered data pipeline runner."""

import json
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.datasync.cli import RunConfig, _build_steps, _run_people_index_step, cli
from fieldkit.config import ConfigError
from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.util.bounded_process import BoundedProcessError, BoundedProcessResult, ProcessFailureReason
from fieldkit.watch.integration_plan import IntegrationPlan

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _build_steps — step configuration
# ---------------------------------------------------------------------------


# ── TestBuildSteps (flattened) ──────────────────────────────────────────────


def test_build_steps_full_run_has_nine_steps(_preflight_runner) -> None:
    cfg = RunConfig(google=True, backstory=True, slack=True)
    steps = _build_steps(cfg)
    assert len(steps) == 9


def test_build_steps_includes_people_index_step(_preflight_runner) -> None:
    cfg = RunConfig(google=True)
    steps = _build_steps(cfg)
    labels = [s[0] for s in steps]
    assert "people-index" in labels


def test_build_steps_quick_mode_skips_gmail(_preflight_runner) -> None:
    cfg = RunConfig(quick=True)
    steps = _build_steps(cfg)
    labels = [s[0] for s in steps]
    assert "gmail sync" not in labels
    assert "account-tags" not in labels
    assert "enrich-pursuits" not in labels
    assert "ingest discover" in labels
    assert "ingest run" in labels


def test_build_steps_sf_flag_adds_sf_step(_preflight_runner) -> None:
    cfg = RunConfig(sf=True)
    steps = _build_steps(cfg)
    labels = [s[0] for s in steps]
    assert "sf listview" in labels


def test_build_steps_no_sf_flag_excludes_sf_step(_preflight_runner) -> None:
    cfg = RunConfig(sf=False)
    steps = _build_steps(cfg)
    labels = [s[0] for s in steps]
    assert "sf listview" not in labels


def test_build_steps_account_flag_appended_to_gmail_analysis_steps(_preflight_runner) -> None:
    """--account scopes the gmail analysis steps, but not the cache-filling sync.

    This test previously asserted the opposite — that the `gmail sync` step
    carried `--account`. `gmail sync` has never accepted that option, so the
    assertion held while the command it described exited
    "Error: No such option: --account". Checking that a string appears in an
    argv list says nothing about whether the argv is valid; see
    tests/test_account_filter_plumbing.py for the check that does.
    """
    cfg = RunConfig(account="acme-bank", google=True)
    steps = dict(_build_steps(cfg))

    for label in ("account-tags", "enrich-pursuits"):
        assert "--account" in steps[label]
        assert "acme-bank" in steps[label]

    assert "--account" not in steps["gmail sync"]


def test_build_steps_quick_plus_sf_has_four_steps(_preflight_runner) -> None:
    cfg = RunConfig(quick=True, sf=True)
    steps = _build_steps(cfg)
    # 2 ingest + the local pursuit watcher + 1 sf = 4
    assert len(steps) == 4


# ---------------------------------------------------------------------------
# _run_people_index_step — cache rebuild outcomes
# ---------------------------------------------------------------------------


def test_run_people_index_step_succeeds() -> None:
    with (
        patch("fieldkit.contact.people_index.build_people_index") as build_index,
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
    ):
        result = _run_people_index_step(1, 1, "acme", dry_run=False)

    assert result.success is True
    assert result.note == ""
    build_index.assert_called_once_with("cache.sqlite", account_filter="acme", show_progress=False)


def test_run_people_index_step_rejects_unready_gmail_cache() -> None:
    with (
        patch(
            "fieldkit.contact.people_index.build_people_index",
            side_effect=GmailSyncPartialError("fictional-private-unready-detail"),
        ),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
    ):
        result = _run_people_index_step(1, 1, None, dry_run=False)

    assert result.success is False
    assert result.note == "Gmail cache is not ready"


def test_run_people_index_step_reports_active_snapshot_as_retryable() -> None:
    with (
        patch(
            "fieldkit.contact.people_index.build_people_index",
            side_effect=SQLiteSnapshotError("fictional-private-snapshot-detail", reason="active"),
        ),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
    ):
        result = _run_people_index_step(1, 1, None, dry_run=False)

    assert result.success is False
    assert result.exit_code == 1
    assert result.note == "Gmail cache is active"
    assert "fictional-private" not in result.note


@pytest.mark.parametrize(
    ("error", "expected_type", "payload_is_preserved"),
    [
        (
            SQLiteSnapshotError("fictional-private-snapshot-detail", reason="unverified"),
            SQLiteSnapshotError,
            True,
        ),
        (ConfigError("fictional-private-config-detail"), ConfigError, False),
        (ValueError("fictional-private-value-detail"), ConfigError, False),
        (RuntimeError("fictional-private-runtime-detail"), RuntimeError, False),
    ],
)
def test_run_people_index_step_propagates_nonretryable_failures(
    error: Exception,
    expected_type: type[Exception],
    payload_is_preserved: bool,
) -> None:
    with (
        patch("fieldkit.contact.people_index.build_people_index", side_effect=error),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
        pytest.raises(expected_type) as raised,
    ):
        _run_people_index_step(1, 1, None, dry_run=False)

    assert ("fictional-private" in str(raised.value)) is payload_is_preserved


@pytest.mark.parametrize(
    ("error", "expected_diagnostic"),
    [
        (
            SQLiteSnapshotError("fictional-private-snapshot-detail", reason="unverified"),
            "SQLite snapshot could not be verified",
        ),
        (ConfigError("fictional-private-config-detail"), "People index configuration is invalid"),
        (ValueError("fictional-private-value-detail"), "People index data is invalid"),
        (RuntimeError("fictional-private-runtime-detail"), "Unhandled exception"),
    ],
)
def test_people_index_nonretryable_failures_map_to_data_exit(
    error: Exception,
    expected_diagnostic: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.__main__ import main

    plan = IntegrationPlan(("gmail",), (), False, False, ())
    with (
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.datasync.cli.run_bounded_process", return_value=BoundedProcessResult(0, "", "")),
        patch("fieldkit.contact.people_index.build_people_index", side_effect=error),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
    ):
        exit_code = main(["sync", "--json"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert expected_diagnostic in captured.err
    assert "fictional-private" not in captured.out
    assert "fictional-private" not in captured.err


@pytest.mark.parametrize(
    "error",
    [
        GmailSyncPartialError("fictional-private-gmail-detail"),
        SQLiteSnapshotError("fictional-private-snapshot-detail", reason="active"),
    ],
)
def test_people_index_retryable_failure_continues_remaining_pipeline(error: Exception) -> None:
    plan = IntegrationPlan(("gmail",), (), False, False, ())
    completed = BoundedProcessResult(0, "", "")
    with (
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.datasync.cli.run_bounded_process", return_value=completed) as run,
        patch("fieldkit.contact.people_index.build_people_index", side_effect=error),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
    ):
        result = CliRunner().invoke(cli, ["--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["failed"] == 1
    assert payload["items"][-1]["label"] == "pursuit-stalls"
    assert payload["items"][-1]["success"] is True
    assert run.call_count == 6


def test_run_people_index_step_propagates_authentication_failure() -> None:
    from fieldkit.errors import AuthError

    with (
        patch(
            "fieldkit.contact.people_index.build_people_index",
            side_effect=AuthError("fictional-private-auth-detail"),
        ),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
        pytest.raises(AuthError, match="People index authentication is required") as raised,
    ):
        _run_people_index_step(1, 1, None, dry_run=False)

    assert "fictional-private" not in str(raised.value)


# ---------------------------------------------------------------------------
# cli — Click integration
# ---------------------------------------------------------------------------


# ── TestCli (flattened) ─────────────────────────────────────────────────────


@pytest.fixture
def _preflight_runner():
    """CliRunner with preflight bypass (implementation note)."""
    runner = CliRunner()

    def _plan(**options: bool) -> IntegrationPlan:
        watchers = ("slack-threads",) if options["slack_requested"] else ()
        services = ("sf",) if options["sf_requested"] else ()
        return IntegrationPlan(services, watchers, False, False, ())

    patcher = patch("fieldkit.watch.preflight.preflight_check", return_value=[])
    plan_patcher = patch("fieldkit.watch.integration_plan.build_integration_plan", side_effect=_plan)
    patcher.start()
    plan_patcher.start()
    yield runner
    plan_patcher.stop()
    patcher.stop()


def test_cli_help_exits_zero(_preflight_runner) -> None:
    result = _preflight_runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "--quick" in result.output
    assert "--sf" in result.output
    assert "--slack" in result.output
    assert "--dry-run" in result.output


def test_cli_dry_run_exits_zero(_preflight_runner) -> None:
    result = _preflight_runner.invoke(cli, ["--dry-run"])
    assert result.exit_code == 0


def test_cli_unknown_account_exits_before_planning_or_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.config.get_account_names", lambda: ["acme-corp"])
    with patch("fieldkit.watch.integration_plan.build_integration_plan") as build_plan:
        result = CliRunner().invoke(cli, ["--account", "unknown-account", "--dry-run"])

    assert result.exit_code == 3
    assert "unknown account" in result.output
    build_plan.assert_not_called()


def test_cli_dry_run_does_not_invoke_subprocess(_preflight_runner) -> None:
    with patch("fieldkit.commands.datasync.cli.run_bounded_process") as mock_run:
        result = _preflight_runner.invoke(cli, ["--dry-run"])
        mock_run.assert_not_called()
    assert result.exit_code == 0


def test_cli_json_dry_run_exercises_real_pipeline(_preflight_runner) -> None:
    result = _preflight_runner.invoke(cli, ["--quick", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 3
    assert payload["failed"] == 0
    assert [item["label"] for item in payload["items"]] == [
        "ingest discover",
        "ingest run",
        "pursuit-stalls",
    ]
    assert all(item["cmd"][0] == "fieldkit" for item in payload["items"])
    assert str(Path(sys.executable).parent) not in result.output


def test_cli_human_dry_run_uses_public_argv(_preflight_runner) -> None:
    result = _preflight_runner.invoke(cli, ["--quick", "--dry-run"])

    assert result.exit_code == 0
    assert "fieldkit ingest discover" in result.output
    assert str(Path(sys.executable).parent) not in result.output


def test_cli_dry_run_never_preflights_credentials(_preflight_runner) -> None:
    with patch("fieldkit.watch.preflight.preflight_check") as preflight:
        result = _preflight_runner.invoke(cli, ["--dry-run"])

    assert result.exit_code == 0
    preflight.assert_not_called()


def test_cli_json_reports_optional_inputs_that_were_not_run() -> None:
    plan = IntegrationPlan(
        preflight_services=(),
        optional_watchers=(),
        calendar=False,
        llm=False,
        skipped=("gmail: not configured", "slack-threads: not selected"),
    )
    with patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan):
        result = CliRunner().invoke(cli, ["--dry-run", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["not_run"] == ["gmail: not configured", "slack-threads: not selected"]


def test_selected_sync_preflight_failure_maps_to_auth_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.__main__ import main

    plan = IntegrationPlan(("gmail",), (), False, False, ())
    with (
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        patch("fieldkit.watch.preflight.preflight_check", return_value=["credential unavailable"]),
    ):
        exit_code = main(["sync"])

    assert exit_code == 2
    assert "Auth error" in capsys.readouterr().err


def test_cli_all_steps_succeed_exits_zero(_preflight_runner) -> None:
    completed = BoundedProcessResult(0, "done", "")
    with patch("fieldkit.commands.datasync.cli.run_bounded_process", return_value=completed):
        result = _preflight_runner.invoke(cli, [])
    assert result.exit_code == 0


def test_cli_step_failure_exits_one(_preflight_runner) -> None:
    call_count = 0

    def side_effect(*args: object, **kwargs: object) -> BoundedProcessResult:
        nonlocal call_count
        call_count += 1
        return BoundedProcessResult(1 if call_count == 1 else 0, "", "error occurred")

    with patch("fieldkit.commands.datasync.cli.run_bounded_process", side_effect=side_effect):
        result = _preflight_runner.invoke(cli, [])
    assert result.exit_code == 1


@pytest.mark.parametrize("child_exit", [2, 3])
def test_cli_preserves_canonical_child_exit_status(_preflight_runner, child_exit: int) -> None:
    completed = BoundedProcessResult(child_exit, "", "fictional-private-provider-detail")
    with patch("fieldkit.commands.datasync.cli.run_bounded_process", return_value=completed):
        result = _preflight_runner.invoke(cli, ["--quick", "--json"])

    assert result.exit_code == child_exit
    payload = json.loads(result.stdout)
    assert {item["exit_code"] for item in payload["items"]} == {child_exit}
    assert "fictional-private-provider-detail" not in result.output


def test_pipeline_exit_code_preserves_strongest_canonical_outcome() -> None:
    import fieldkit.commands.datasync.cli as datasync

    results = [
        datasync.StepResult(1, 3, "ok", ["fieldkit"], True, 0.0, exit_code=0),
        datasync.StepResult(2, 3, "auth", ["fieldkit"], False, 0.0, exit_code=2),
        datasync.StepResult(3, 3, "data", ["fieldkit"], False, 0.0, exit_code=3),
    ]

    exit_code = datasync._pipeline_exit_code(results)

    assert exit_code == 3


def test_cli_quick_flag_skips_gmail(_preflight_runner) -> None:
    invoked: list[list[str]] = []

    def capture(*args: object, **kwargs: object) -> BoundedProcessResult:
        invoked.append(list(args[0]))  # type: ignore[arg-type]
        return BoundedProcessResult(0, "", "")

    with patch("fieldkit.commands.datasync.cli.run_bounded_process", side_effect=capture):
        _preflight_runner.invoke(cli, ["--quick"])

    all_cmds = " ".join(str(c) for c in invoked)
    assert "gmail sync" not in all_cmds
    assert "ingest" in all_cmds


def test_cli_sf_flag_invokes_sf_listview(_preflight_runner) -> None:
    invoked: list[list[str]] = []

    def capture(*args: object, **kwargs: object) -> BoundedProcessResult:
        invoked.append(list(args[0]))  # type: ignore[arg-type]
        return BoundedProcessResult(0, "", "")

    with patch("fieldkit.commands.datasync.cli.run_bounded_process", side_effect=capture):
        _preflight_runner.invoke(cli, ["--sf"])

    all_cmds = " ".join(str(cmd) for cmds in invoked for cmd in cmds)
    assert "listview" in all_cmds


def test_watcher_phase_starts_all_siblings_before_any_finishes() -> None:
    import fieldkit.commands.datasync.cli as datasync

    barrier = threading.Barrier(3)

    def execute(index: int, total: int, label: str, cmd: list[str]) -> datasync._StepExecution:
        barrier.wait(timeout=2)
        result = datasync.StepResult(index, total, label, cmd, True, 1.0)
        return datasync._StepExecution(result)

    steps = [(label, [label]) for label in ("backstory-health", "pursuit-stalls", "slack-threads")]
    with (
        patch.object(datasync, "_execute_step", side_effect=execute),
        patch.object(datasync, "_render_step"),
        patch.object(datasync, "ThreadPoolExecutor", wraps=ThreadPoolExecutor) as executor,
    ):
        results = datasync._run_concurrent_watchers(steps, start_index=4, total=7, verbose=False, as_json=True)

    assert [result.label for result in results] == [label for label, _ in steps]
    assert list(results[0].__dict__) == [
        "index",
        "total",
        "label",
        "cmd",
        "success",
        "elapsed",
        "note",
        "exit_code",
    ]
    executor.assert_called_once_with(max_workers=3, thread_name_prefix="fieldkit-sync-watch")


def test_watcher_phase_waits_for_failure_and_renders_source_order(tmp_path: Path) -> None:
    import fieldkit.commands.datasync.cli as datasync

    later_finished = threading.Event()
    private_path = str(tmp_path / "private" / "bin" / "fieldkit")

    def execute(index: int, total: int, label: str, cmd: list[str]) -> datasync._StepExecution:
        if label == "slack-threads":
            later_finished.set()
        else:
            assert later_finished.wait(timeout=2)
        if label == "pursuit-stalls":
            raise PermissionError(private_path)
        result = datasync.StepResult(index, total, label, cmd, True, 1.0)
        return datasync._StepExecution(result, stdout=f"{label} output")

    steps = [(label, [label]) for label in ("backstory-health", "pursuit-stalls", "slack-threads")]
    rendered: list[str] = []
    with (
        patch.object(datasync, "_execute_step", side_effect=execute),
        patch.object(
            datasync, "_render_step", side_effect=lambda execution, **_: rendered.append(execution.result.label)
        ),
    ):
        results = datasync._run_concurrent_watchers(steps, start_index=4, total=7, verbose=True, as_json=True)

    assert [result.success for result in results] == [True, False, True]
    assert results[1].note == "Unable to start command"
    assert private_path not in json.dumps([result.__dict__ for result in results])
    assert rendered == ["backstory-health", "pursuit-stalls", "slack-threads"]


def test_watcher_phase_propagates_authentication_failure() -> None:
    import fieldkit.commands.datasync.cli as datasync
    from fieldkit.errors import AuthError

    future: Future[datasync._StepExecution] = Future()
    future.set_exception(AuthError("fictional-private-auth-detail"))

    with pytest.raises(AuthError, match="fictional-private-auth-detail"):
        datasync._collect_watcher(future, 1, 1, "pursuit-stalls", ["fieldkit"])


def test_sequential_process_start_failures_are_nonpassing_and_payload_free(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import fieldkit.commands.datasync.cli as datasync

    private_path = str(tmp_path / "private" / "bin" / "fieldkit")
    error = BoundedProcessError(private_path, reason="start")
    with patch("fieldkit.commands.datasync.cli.run_bounded_process", side_effect=error):
        result = datasync._run_step(1, 1, "gmail", ["fieldkit", "gmail", "sync"], dry_run=False)

    assert result.success is False
    assert result.note == "Unable to start command"
    assert private_path not in json.dumps(result.__dict__)
    assert private_path not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("reason", "expected_note"),
    [
        ("timeout", "timeout"),
        ("overflow", "Command output exceeded limit"),
        ("pipes", "Command execution failed"),
        ("cleanup", "Command execution failed"),
    ],
)
def test_bounded_process_failures_are_nonpassing_and_payload_free(
    reason: ProcessFailureReason,
    expected_note: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fieldkit.commands.datasync.cli as datasync

    private_payload = "/private-fixture/customer/process-detail"
    error = BoundedProcessError(private_payload, reason=reason)
    with patch("fieldkit.commands.datasync.cli.run_bounded_process", side_effect=error):
        result = datasync._run_step(1, 1, "ingest", ["fieldkit", "ingest", "run"], dry_run=False)

    assert result.success is False
    assert result.exit_code == 1
    assert result.note == expected_note
    assert private_payload not in json.dumps(result.__dict__)
    assert private_payload not in capsys.readouterr().err


def test_pipeline_banner_uses_utc_date(capsys: pytest.CaptureFixture[str]) -> None:
    import fieldkit.commands.datasync.cli as datasync

    with patch.object(datasync, "datetime") as clock:
        clock.now.return_value.date.return_value.isoformat.return_value = "2026-09-28"
        datasync._render_pipeline_start(datasync.RunConfig())

    clock.now.assert_called_once_with(tz=UTC)
    assert "2026-09-28" in capsys.readouterr().out


def test_sync_json_process_start_failure_continues_with_partial_exit(
    _preflight_runner: CliRunner, tmp_path: Path
) -> None:
    private_path = str(tmp_path / "private" / "bin" / "fieldkit")
    error = BoundedProcessError(private_path, reason="start")
    with patch("fieldkit.commands.datasync.cli.run_bounded_process", side_effect=error):
        result = _preflight_runner.invoke(cli, ["--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["failed"] >= 1
    assert payload["count"] == len(payload["items"])
    assert any(item["note"] == "Unable to start command" for item in payload["items"])
    assert private_path not in result.output


def test_subprocess_failure_note_omits_child_payload(capsys: pytest.CaptureFixture[str]) -> None:
    import fieldkit.commands.datasync.cli as datasync

    private_payload = "/private-fixture/customer/acme.db: permission denied"
    completed = BoundedProcessResult(1, "", private_payload)
    with patch("fieldkit.commands.datasync.cli.run_bounded_process", return_value=completed):
        result = datasync._run_step(1, 1, "ingest", [sys.executable, "-m", "fieldkit"], dry_run=False)

    assert result.success is False
    assert result.exit_code == 1
    assert result.note == "Command failed"
    assert result.cmd == ["fieldkit"]
    assert private_payload not in json.dumps(result.__dict__)
    assert private_payload not in capsys.readouterr().err


def test_sync_dry_run_does_not_create_watcher_executor(_preflight_runner) -> None:
    with patch("fieldkit.commands.datasync.cli.ThreadPoolExecutor") as executor:
        result = _preflight_runner.invoke(cli, ["--dry-run"])

    assert result.exit_code == 0
    executor.assert_not_called()


def test_cli_waits_for_all_watcher_siblings_and_exits_partial(_preflight_runner) -> None:
    invoked: list[str] = []

    def run_subprocess(cmd: list[str], **_: object) -> BoundedProcessResult:
        label = cmd[-1]
        invoked.append(label)
        returncode = 1 if label == "pursuit-stalls" else 0
        return BoundedProcessResult(returncode, "", "watcher failed" if returncode else "")

    with patch("fieldkit.commands.datasync.cli.run_bounded_process", side_effect=run_subprocess):
        result = _preflight_runner.invoke(cli, ["--slack"])

    assert result.exit_code == 1
    assert {"pursuit-stalls", "slack-threads"}.issubset(invoked)
