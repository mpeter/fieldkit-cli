"""Tests for the nightly health sensor (implementation change): checks, filing/dedup, runner outcomes."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from fieldkit.health import runner as runner_mod
from fieldkit.health.checks import HEALTH_CHECKS, CheckResult, HealthCheck, run_check
from fieldkit.health.filing import (
    HEALTH_TITLE_MARKER,
    file_regressions,
    issue_title_for,
)
from fieldkit.health.runner import (
    HealthRunResult,
    prepare_main_worktree,
    run_health,
    write_health_run_status,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(check_id: str, status: str, output: str = "boom") -> CheckResult:
    return CheckResult(check_id=check_id, status=status, output=output, elapsed_seconds=0.1)  # type: ignore[arg-type]


class _RecordingFiler:
    """IssueFiler double that records filings against a fixed open-titles list."""

    def __init__(self, open_titles: list[str] | None = None) -> None:
        self._open_titles = open_titles or []
        self.filed: list[tuple[str, str]] = []
        self.open_titles_calls = 0

    def open_titles(self) -> list[str]:
        self.open_titles_calls += 1
        return self._open_titles

    def file_regression(self, *, title: str, body: str) -> str:
        self.filed.append((title, body))
        return f"historic regression (#{len(self.filed)})"


def _read_status_entries(data_root: Path) -> list[dict[str, Any]]:
    status_file = data_root / "logs" / "health" / "health-run-status.json"
    if not status_file.exists():
        return []
    loaded: list[dict[str, Any]] = json.loads(status_file.read_text(encoding="utf-8"))["runs"]
    return loaded


# ---------------------------------------------------------------------------
# Failure → issue-title mapping (task 5.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("check_id", [c.check_id for c in HEALTH_CHECKS])
def test_issue_title_for_is_stable_and_dedupable(check_id: str) -> None:
    title = issue_title_for(check_id)

    assert title == f"nightly health: {check_id} gate failing on main"
    assert HEALTH_TITLE_MARKER in title


def test_check_id_vocabulary_pins_makefile_bundle() -> None:
    """The check table mirrors the Makefile quality recipe — renames orphan open issues."""
    check_ids = [c.check_id for c in HEALTH_CHECKS]

    assert check_ids == [
        "ruff-lint",
        "ruff-format",
        "mypy",
        "tach",
        "skill-integrity",
        "click-params",
        "cli-docs",
        "dep-map",
        "doc-freshness",
        "schema-sync",
        "skillsaw",
        "pytest-cov",
        "gazepy-crap",
        "gazepy-quality",
        "agentready-assess",
        "agentready-gate",
        "skill-eval-behavioral",
    ]
    assert len(set(check_ids)) == len(check_ids)


# ---------------------------------------------------------------------------
# Dedup predicate (task 5.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "open_title",
    [
        "nightly health: mypy gate failing on main",
        "historic regression: nightly health: mypy gate failing on main",
        "NIGHTLY HEALTH: MYPY gate failing on main (still broken)",
        # Operator edited the title tail — the marker + check-id still dedups.
        "historic regression: nightly health: mypy gate now flaky on main, investigating",
    ],
)
def test_still_open_finding_is_not_refiled(open_title: str) -> None:
    filer = _RecordingFiler(open_titles=[open_title])

    outcome = file_regressions([_result("mypy", "fail")], filer)

    # CR-014: direct return value assertion
    from fieldkit.health.filing import FilingOutcome

    assert isinstance(outcome, FilingOutcome)
    assert outcome.deduped == ("mypy",)
    assert outcome.filed == ()
    assert filer.filed == []


def test_new_regression_is_filed_once_with_output_in_body() -> None:
    filer = _RecordingFiler(open_titles=["historic regression: unrelated open issue"])

    outcome = file_regressions([_result("tach", "fail", output="boundary violation in fieldkit.watch")], filer)

    # CR-014: direct return value assertion
    from fieldkit.health.filing import FilingOutcome

    assert isinstance(outcome, FilingOutcome)
    assert outcome.filed == ("tach",)
    assert outcome.deduped == ()
    assert len(filer.filed) == 1
    title, body = filer.filed[0]
    assert title == issue_title_for("tach")
    assert "boundary violation in fieldkit.watch" in body
    assert "<!-- fieldkit-health-check: tach -->" in body


def test_mixed_run_files_new_and_dedups_persistent() -> None:
    filer = _RecordingFiler(open_titles=["historic regression: nightly health: mypy gate failing on main"])

    outcome = file_regressions([_result("mypy", "fail"), _result("pytest-cov", "fail")], filer)

    # CR-014: direct return value assertion
    from fieldkit.health.filing import FilingOutcome

    assert isinstance(outcome, FilingOutcome)
    assert outcome.filed == ("pytest-cov",)
    assert outcome.deduped == ("mypy",)


def test_error_and_skipped_checks_are_never_filed() -> None:
    filer = _RecordingFiler()

    outcome = file_regressions([_result("mypy", "error"), _result("skill-eval-behavioral", "skipped")], filer)

    # CR-014: direct return value assertion
    from fieldkit.health.filing import FilingOutcome

    assert isinstance(outcome, FilingOutcome)
    assert outcome.filed == ()
    assert outcome.deduped == ()
    assert filer.filed == []


# ---------------------------------------------------------------------------
# Green run files nothing (task 5.2)
# ---------------------------------------------------------------------------


def test_green_bundle_files_nothing_and_never_invokes_filer() -> None:
    filer = MagicMock()

    outcome = file_regressions([_result(c.check_id, "pass") for c in HEALTH_CHECKS], filer)

    # CR-014: direct return value assertion
    from fieldkit.health.filing import FilingOutcome

    assert isinstance(outcome, FilingOutcome)
    assert outcome.filed == ()
    assert outcome.deduped == ()
    filer.open_titles.assert_not_called()
    filer.file_regression.assert_not_called()


def test_dry_run_senses_but_does_not_file() -> None:
    filer = _RecordingFiler()

    outcome = file_regressions([_result("mypy", "fail")], filer, dry_run=True)

    # CR-014: direct return value assertion
    from fieldkit.health.filing import FilingOutcome

    assert isinstance(outcome, FilingOutcome)
    assert outcome.filed == ("mypy",)
    assert filer.filed == []


# ---------------------------------------------------------------------------
# Runner self-failure is fatal, not partial, and not silent (task 5.3)
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the runner's status writes to tmp_path (patch the import-site binding)."""
    monkeypatch.setattr(runner_mod, "get_fieldkit_data", lambda: tmp_path)
    return tmp_path


def test_worktree_failure_is_fatal_and_recorded(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "prepare_main_worktree", lambda repo_root: None)
    filer = MagicMock()

    result = run_health(Path("/nonexistent-repo"), filer)

    assert result.outcome == "fatal"
    assert result.checks_run == 0
    entries = _read_status_entries(data_root)
    assert [e["outcome"] for e in entries] == ["fatal"]
    filer.file_regression.assert_not_called()


def test_runner_crash_writes_fatal_status_then_raises(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(repo_root: Path) -> Path:
        raise RuntimeError("git binary exploded")

    monkeypatch.setattr(runner_mod, "prepare_main_worktree", _boom)

    with pytest.raises(RuntimeError, match="git binary exploded"):
        run_health(Path("/nonexistent-repo"), MagicMock())

    entries = _read_status_entries(data_root)
    assert [e["outcome"] for e in entries] == ["fatal"]
    assert "git binary exploded" in entries[0]["error"]


@pytest.mark.parametrize(
    ("statuses", "expected_outcome"),
    [
        (["pass", "pass", "pass"], "ok"),
        (["pass", "fail", "pass"], "ok"),  # gate failure is a sensed regression, not a runner failure
        (["pass", "error", "pass"], "partial"),
        (["skipped", "skipped", "skipped"], "fatal"),  # zero checks executed
    ],
)
def test_outcome_classification(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[str],
    expected_outcome: str,
) -> None:
    worktree = data_root / "wt"
    worktree.mkdir()
    monkeypatch.setattr(runner_mod, "prepare_main_worktree", lambda repo_root: worktree)
    monkeypatch.setattr(runner_mod, "remove_main_worktree", lambda path, repo_root: None)
    monkeypatch.setattr(runner_mod, "_bootstrap_check_environment", lambda worktree, env: None)
    fake_checks = tuple(HealthCheck(f"check-{i}", ("true",)) for i in range(len(statuses)))
    monkeypatch.setattr(runner_mod, "HEALTH_CHECKS", fake_checks)
    status_iter = iter(statuses)
    monkeypatch.setattr(
        runner_mod,
        "run_check",
        lambda check, *, cwd, env: _result(check.check_id, next(status_iter)),
    )

    result = run_health(Path("/repo"), _RecordingFiler())

    assert result.outcome == expected_outcome
    entries = _read_status_entries(data_root)
    assert [e["outcome"] for e in entries] == [expected_outcome]


def test_filing_failure_downgrades_to_partial_not_silent(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = data_root / "wt"
    worktree.mkdir()
    monkeypatch.setattr(runner_mod, "prepare_main_worktree", lambda repo_root: worktree)
    monkeypatch.setattr(runner_mod, "remove_main_worktree", lambda path, repo_root: None)
    monkeypatch.setattr(runner_mod, "_bootstrap_check_environment", lambda worktree, env: None)
    monkeypatch.setattr(runner_mod, "HEALTH_CHECKS", (HealthCheck("mypy", ("true",)),))
    monkeypatch.setattr(runner_mod, "run_check", lambda check, *, cwd, env: _result("mypy", "fail"))
    broken_filer = MagicMock()
    broken_filer.open_titles.side_effect = RuntimeError("gh issue list failed: auth")

    result = run_health(Path("/repo"), broken_filer)

    assert result.outcome == "partial"
    assert any("filing failed" in err for err in result.runner_errors)
    entries = _read_status_entries(data_root)
    assert entries[0]["outcome"] == "partial"


def test_write_health_run_status_skips_dry_run_and_caps_entries(data_root: Path) -> None:
    ok_result = HealthRunResult(
        outcome="ok",
        checks_run=3,
        gate_failures=(),
        runner_errors=(),
        issues_filed=(),
        issues_deduped=(),
        elapsed_seconds=1.0,
    )

    write_health_run_status(ok_result, dry_run=True)
    assert _read_status_entries(data_root) == []

    for _ in range(105):
        write_health_run_status(ok_result, dry_run=False)
    entries = _read_status_entries(data_root)
    assert len(entries) == 100


# ---------------------------------------------------------------------------
# Check invocation classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returncode", "optional", "stderr", "expected_status"),
    [
        (0, False, "", "pass"),
        (1, False, "gate exploded", "fail"),
        (1, True, "Error: No such command 'eval'", "skipped"),
        (1, True, "real failure output", "fail"),
    ],
)
def test_run_check_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    optional: bool,
    stderr: str,
    expected_status: str,
) -> None:
    completed = subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout="out", stderr=stderr)
    monkeypatch.setattr("fieldkit.health.checks.subprocess.run", lambda *a, **kw: completed)
    check = HealthCheck("mypy", ("mypy",), optional=optional)

    result = run_check(check, cwd=tmp_path, env={})

    assert result.status == expected_status
    assert result.check_id == "mypy"


@pytest.mark.parametrize("optional", [False, True])
def test_run_check_invocation_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, optional: bool) -> None:
    def _raise(*args: Any, **kwargs: Any) -> None:
        raise OSError("tool not found")

    monkeypatch.setattr("fieldkit.health.checks.subprocess.run", _raise)
    check = HealthCheck("tach", ("tach",), optional=optional)

    result = run_check(check, cwd=tmp_path, env={})

    assert result.status == ("skipped" if optional else "error")
    assert "tool not found" in result.output


def test_run_check_expands_globs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "hooks" / "b.py").write_text("", encoding="utf-8")
    seen: dict[str, Any] = {}

    def _capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fieldkit.health.checks.subprocess.run", _capture)

    result = run_check(HealthCheck("mypy", ("mypy", "hooks/*.py")), cwd=tmp_path, env={})

    assert result.status == "pass"
    assert seen["argv"] == ["mypy", "hooks/a.py", "hooks/b.py"]


def test_run_check_resolves_crapload_from_worktree_makefile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "Makefile").write_text(
        "# uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --max-crapload 999\n"
        "uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --baseline .gaze/baseline.json\n"
        "uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --max-crapload 67\n",
        encoding="utf-8",
    )
    seen: dict[str, Any] = {}

    def _capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fieldkit.health.checks.subprocess.run", _capture)
    check = HealthCheck("gazepy-crap", ("gazepy", "crap", "--max-crapload", "{makefile-crapload}"))

    result = run_check(check, cwd=tmp_path, env={})

    assert result.status == "pass"
    assert seen["argv"] == ["gazepy", "crap", "--max-crapload", "67"]


def test_run_check_resolves_crapload_from_backslash_continued_makefile_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Makefile").write_text(
        "uv run gazepy crap src/fieldkit/ --coverprofile coverage.json \\\n\t--max-crapload 67\n",
        encoding="utf-8",
    )
    seen: dict[str, Any] = {}

    def _capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fieldkit.health.checks.subprocess.run", _capture)
    check = HealthCheck("gazepy-crap", ("gazepy", "crap", "--max-crapload", "{makefile-crapload}"))

    result = run_check(check, cwd=tmp_path, env={})

    assert result.status == "pass"
    assert seen["argv"] == ["gazepy", "crap", "--max-crapload", "67"]


@pytest.mark.parametrize(
    "makefile_text",
    [
        "uv run gazepy crap src/fieldkit/ --coverprofile coverage.json\n",
        "\n".join(
            [
                "uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --max-crapload 67",
                "uv run gazepy crap src/fieldkit/ --coverprofile coverage.json --max-crapload 68",
            ]
        ),
    ],
)
def test_run_check_reports_error_for_missing_or_ambiguous_makefile_crapload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    makefile_text: str,
) -> None:
    (tmp_path / "Makefile").write_text(makefile_text, encoding="utf-8")
    run = MagicMock()
    monkeypatch.setattr("fieldkit.health.checks.subprocess.run", run)
    check = HealthCheck("gazepy-crap", ("gazepy", "crap", "--max-crapload", "{makefile-crapload}"))

    result = run_check(check, cwd=tmp_path, env={})

    assert result.status == "error"
    assert "Gazepy --max-crapload" in result.output
    run.assert_not_called()


# ---------------------------------------------------------------------------
# Worktree preparation senses origin/main
# ---------------------------------------------------------------------------


def test_prepare_main_worktree_fetch_failure_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(argv: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(returncode=128, cmd=argv, stderr="no origin")

    monkeypatch.setattr(runner_mod, "get_harness_scratch_root", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.health.runner.subprocess.run", _fail)

    worktree = prepare_main_worktree(tmp_path)

    assert worktree is None


def test_prepare_main_worktree_targets_origin_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "get_harness_scratch_root", lambda: tmp_path)
    calls: list[list[str]] = []

    def _capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fieldkit.health.runner.subprocess.run", _capture)

    worktree = prepare_main_worktree(tmp_path)

    assert worktree is not None
    assert calls[0][:4] == ["git", "fetch", "origin", "main"]
    assert calls[1][:4] == ["git", "worktree", "add", "--force"]
    assert calls[1][-1] == "origin/main"  # senses committed main, never the dirty tree


def test_check_env_isolates_home_and_removes_runtime_data_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    operator_home = tmp_path / "operator-home"
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("FIELDKIT_DATA_DIR", str(tmp_path / "operator-data"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "operator-venv"))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "operator-uv-venv"))

    env = runner_mod._check_env(worktree)

    assert env["HOME"] == str(worktree / ".fieldkit-home")
    assert Path(env["HOME"]).is_dir()
    assert env["HOME"] != str(operator_home)
    assert env["TMPDIR"] == str(worktree / ".tmp")
    assert Path(env["TMPDIR"]).is_dir()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "FIELDKIT_DATA_DIR" not in env
    assert "VIRTUAL_ENV" not in env
    assert "UV_PROJECT_ENVIRONMENT" not in env


def test_bootstrap_check_environment_syncs_locked_all_extras_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def _capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["cwd"] = kwargs["cwd"]
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fieldkit.health.runner.subprocess.run", _capture)
    environment = {"TMPDIR": str(tmp_path / ".tmp")}

    result = runner_mod._bootstrap_check_environment(tmp_path, environment)

    assert result is None
    assert seen["argv"] == ["uv", "sync", "--frozen", "--all-extras", "--dev"]
    assert seen["cwd"] == tmp_path
    assert seen["env"] == environment


def test_run_health_disables_implicit_uv_sync_after_bootstrap(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = data_root / "wt"
    worktree.mkdir()
    seen: dict[str, str] = {}
    monkeypatch.setattr(runner_mod, "prepare_main_worktree", lambda repo_root: worktree)
    monkeypatch.setattr(runner_mod, "remove_main_worktree", lambda path, repo_root: None)
    monkeypatch.setattr(runner_mod, "_bootstrap_check_environment", lambda worktree, env: None)
    monkeypatch.setattr(runner_mod, "HEALTH_CHECKS", (HealthCheck("mypy", ("true",)),))

    def _check(check: HealthCheck, *, cwd: Path, env: dict[str, str]) -> CheckResult:
        seen.update(env)
        return _result(check.check_id, "pass")

    monkeypatch.setattr(runner_mod, "run_check", _check)

    result = run_health(Path("/repo"), _RecordingFiler())

    assert result.outcome == "ok"
    assert seen["UV_NO_SYNC"] == "1"


def test_environment_bootstrap_failure_is_fatal_and_recorded(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = data_root / "wt"
    worktree.mkdir()
    monkeypatch.setattr(runner_mod, "prepare_main_worktree", lambda repo_root: worktree)
    monkeypatch.setattr(runner_mod, "remove_main_worktree", lambda path, repo_root: None)
    monkeypatch.setattr(
        runner_mod,
        "_bootstrap_check_environment",
        lambda worktree, env: "environment bootstrap failed (exit 1): resolver unavailable",
    )

    result = run_health(Path("/repo"), _RecordingFiler())

    assert result.outcome == "fatal"
    assert result.checks_run == 0
    assert result.error == "environment bootstrap failed (exit 1): resolver unavailable"
    assert _read_status_entries(data_root)[0]["outcome"] == "fatal"


def test_prepare_main_worktree_sweeps_stale_worktrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed prior run's leftover worktree is removed before the new run (disk guard)."""
    monkeypatch.setattr(runner_mod, "get_harness_scratch_root", lambda: tmp_path)
    stale = tmp_path / "health" / "worktrees" / "health-20260101-000000"
    stale.mkdir(parents=True)
    (stale / "junk.txt").write_text("leftover", encoding="utf-8")

    def _ok(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fieldkit.health.runner.subprocess.run", _ok)

    worktree = prepare_main_worktree(tmp_path)

    assert worktree is not None
    assert not stale.exists()


# ---------------------------------------------------------------------------
# Label policy (task 5.4, design Decision 1): the sensor never grants agent-ready
# ---------------------------------------------------------------------------


def test_filed_issue_carries_no_agent_ready_label(monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.commands.health.cli import _GHIssueFiler
    from fieldkit.commands.issue import gh_store

    captured: dict[str, tuple[str, ...]] = {}

    def _fake_gh(*args: str) -> str:
        captured["args"] = args
        return "123"

    monkeypatch.setattr(gh_store, "_gh", _fake_gh)
    monkeypatch.setattr(gh_store.GHIssueStore, "next_id", lambda self, issue_type: "historic regression")
    filer = _GHIssueFiler("acme-corp/fieldkit-cli")

    ref = filer.file_regression(title=issue_title_for("mypy"), body="body")

    assert ref == "historic regression (#123)"
    labels = [a for a in captured["args"] if a.startswith("labels[]=")]
    assert labels == ["labels[]=bug", "labels[]=severity:high", "labels[]=module:other"]
    assert "labels[]=agent-ready" not in captured["args"]


# ---------------------------------------------------------------------------
# CLI adapter stays thin: exit codes and dry-run plumbing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected_exit"),
    [("ok", 0), ("partial", 1), ("fatal", 1)],
)
def test_cli_run_exit_codes(monkeypatch: pytest.MonkeyPatch, outcome: str, expected_exit: int) -> None:
    from fieldkit.commands.health import cli as health_cli

    stub_result = HealthRunResult(
        outcome=outcome,  # type: ignore[arg-type]
        checks_run=2,
        gate_failures=("mypy",) if outcome != "ok" else (),
        runner_errors=("mypy: boom",) if outcome != "ok" else (),
        issues_filed=(),
        issues_deduped=(),
        elapsed_seconds=0.2,
    )
    monkeypatch.setattr(health_cli, "run_health", lambda repo_root, filer, *, dry_run: stub_result)
    monkeypatch.setattr(health_cli, "_GHIssueFiler", lambda repo: MagicMock())
    monkeypatch.setattr(health_cli, "get_github_repo", lambda: "acme-corp/fieldkit-cli")

    runner = CliRunner()
    invocation = runner.invoke(health_cli.cli, ["run", "--dry-run"])

    assert invocation.exit_code == expected_exit


def test_cli_status_renders_recent_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.commands.health import cli as health_cli

    status_file = tmp_path / "logs" / "health" / "health-run-status.json"
    status_file.parent.mkdir(parents=True)
    status_file.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "ts": "2026-07-19T06:15:00Z",
                        "outcome": "ok",
                        "checks_run": 17,
                        "gate_failures": [],
                        "issues_filed": [],
                        "issues_deduped": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(health_cli, "get_fieldkit_data", lambda: tmp_path)

    runner = CliRunner()
    invocation = runner.invoke(health_cli.cli, ["status"])

    assert invocation.exit_code == 0
    assert "checks=17" in invocation.output
