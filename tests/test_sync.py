"""Tests for fieldkit.commands.datasync — ordered data pipeline runner."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.datasync.cli import RunConfig, _build_steps, _run_people_index_step, cli

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _build_steps — step configuration
# ---------------------------------------------------------------------------


# ── TestBuildSteps (flattened) ──────────────────────────────────────────────


def test_build_steps_full_run_has_nine_steps(_preflight_runner) -> None:
    cfg = RunConfig()
    steps = _build_steps(cfg)
    assert len(steps) == 9


def test_build_steps_includes_people_index_step(_preflight_runner) -> None:
    cfg = RunConfig()
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
    cfg = RunConfig(account="acme-bank")
    steps = dict(_build_steps(cfg))

    for label in ("account-tags", "enrich-pursuits"):
        assert "--account" in steps[label]
        assert "acme-bank" in steps[label]

    assert "--account" not in steps["gmail sync"]


def test_build_steps_quick_plus_sf_has_three_steps(_preflight_runner) -> None:
    cfg = RunConfig(quick=True, sf=True)
    steps = _build_steps(cfg)
    # 2 ingest + 3 watchers + 1 sf = 6
    assert len(steps) == 6


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


def test_run_people_index_step_accepts_empty_gmail_cache() -> None:
    with (
        patch(
            "fieldkit.contact.people_index.build_people_index",
            side_effect=sqlite3.OperationalError("no such table: messages"),
        ),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
    ):
        result = _run_people_index_step(1, 1, None, dry_run=False)

    assert result.success is True
    assert result.note == "no gmail data yet"


def test_run_people_index_step_reports_database_and_unexpected_errors() -> None:
    with (
        patch(
            "fieldkit.contact.people_index.build_people_index",
            side_effect=sqlite3.OperationalError("database is locked"),
        ),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
    ):
        database_error = _run_people_index_step(1, 1, None, dry_run=False)

    with (
        patch("fieldkit.contact.people_index.build_people_index", side_effect=RuntimeError("service unavailable")),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value="cache.sqlite"),
    ):
        unexpected_error = _run_people_index_step(1, 1, None, dry_run=False)

    assert database_error.success is False
    assert database_error.note == "database is locked"
    assert unexpected_error.success is False
    assert unexpected_error.note == "service unavailable"


# ---------------------------------------------------------------------------
# cli — Click integration
# ---------------------------------------------------------------------------


# ── TestCli (flattened) ─────────────────────────────────────────────────────


@pytest.fixture
def _preflight_runner():
    """CliRunner with preflight bypass (implementation note)."""
    runner = CliRunner()
    patcher = patch("fieldkit.watch.preflight.preflight_check", return_value=[])
    patcher.start()
    yield runner
    patcher.stop()


def test_cli_help_exits_zero(_preflight_runner) -> None:
    result = _preflight_runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "--quick" in result.output
    assert "--sf" in result.output
    assert "--dry-run" in result.output


def test_cli_dry_run_exits_zero(_preflight_runner) -> None:
    result = _preflight_runner.invoke(cli, ["--dry-run"])
    assert result.exit_code == 0


def test_cli_dry_run_does_not_invoke_subprocess(_preflight_runner) -> None:
    with patch("subprocess.run") as mock_run:
        result = _preflight_runner.invoke(cli, ["--dry-run"])
        mock_run.assert_not_called()
    assert result.exit_code == 0


def test_cli_json_dry_run_exercises_real_pipeline(_preflight_runner) -> None:
    result = _preflight_runner.invoke(cli, ["--quick", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 5
    assert payload["failed"] == 0
    assert [item["label"] for item in payload["items"]] == [
        "ingest discover",
        "ingest run",
        "backstory-health",
        "pursuit-stalls",
        "slack-threads",
    ]


def test_cli_all_steps_succeed_exits_zero(_preflight_runner) -> None:
    mock_completed = MagicMock()
    mock_completed.returncode = 0
    mock_completed.stdout = "done"
    mock_completed.stderr = ""
    with patch("subprocess.run", return_value=mock_completed):
        result = _preflight_runner.invoke(cli, [])
    assert result.exit_code == 0


def test_cli_step_failure_exits_one(_preflight_runner) -> None:
    call_count = 0

    def side_effect(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        # Fail the first step only
        m.returncode = 1 if call_count == 1 else 0
        m.stdout = ""
        m.stderr = "error occurred"
        return m

    with patch("subprocess.run", side_effect=side_effect):
        result = _preflight_runner.invoke(cli, [])
    assert result.exit_code == 1


def test_cli_quick_flag_skips_gmail(_preflight_runner) -> None:
    invoked: list[list[str]] = []

    def capture(*args: object, **kwargs: object) -> MagicMock:
        invoked.append(list(args[0]))  # type: ignore[arg-type]
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=capture):
        _preflight_runner.invoke(cli, ["--quick"])

    all_cmds = " ".join(str(c) for c in invoked)
    assert "gmail sync" not in all_cmds
    assert "ingest" in all_cmds


def test_cli_sf_flag_invokes_sf_listview(_preflight_runner) -> None:
    invoked: list[list[str]] = []

    def capture(*args: object, **kwargs: object) -> MagicMock:
        invoked.append(list(args[0]))  # type: ignore[arg-type]
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=capture):
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
    assert list(results[0].__dict__) == ["index", "total", "label", "cmd", "success", "elapsed", "note"]
    executor.assert_called_once_with(max_workers=3, thread_name_prefix="fieldkit-sync-watch")


def test_watcher_phase_waits_for_failure_and_renders_source_order() -> None:
    import fieldkit.commands.datasync.cli as datasync

    later_finished = threading.Event()

    def execute(index: int, total: int, label: str, cmd: list[str]) -> datasync._StepExecution:
        if label == "slack-threads":
            later_finished.set()
        else:
            assert later_finished.wait(timeout=2)
        if label == "pursuit-stalls":
            raise PermissionError("watcher executable denied")
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
    assert results[1].note == "PermissionError: watcher executable denied"
    assert rendered == ["backstory-health", "pursuit-stalls", "slack-threads"]


def test_sync_dry_run_does_not_create_watcher_executor(_preflight_runner) -> None:
    with patch("fieldkit.commands.datasync.cli.ThreadPoolExecutor") as executor:
        result = _preflight_runner.invoke(cli, ["--dry-run"])

    assert result.exit_code == 0
    executor.assert_not_called()


def test_cli_waits_for_all_watcher_siblings_and_exits_partial(_preflight_runner) -> None:
    invoked: list[str] = []

    def run_subprocess(cmd: list[str], **_: object) -> MagicMock:
        label = cmd[-1]
        invoked.append(label)
        completed = MagicMock()
        completed.returncode = 1 if label == "pursuit-stalls" else 0
        completed.stdout = ""
        completed.stderr = "watcher failed" if completed.returncode else ""
        return completed

    with patch("subprocess.run", side_effect=run_subprocess):
        result = _preflight_runner.invoke(cli, [])

    assert result.exit_code == 1
    assert {"backstory-health", "pursuit-stalls", "slack-threads"}.issubset(invoked)
