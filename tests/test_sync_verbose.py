"""Tests for implementation change (--verbose flag) and implementation change (--account scopes slack-threads).

implementation change covers:
- Full stderr is printed to stderr when verbose=True and subprocess fails.
- Full stdout is printed to stderr when verbose=True and subprocess succeeds.
- Full output is suppressed when verbose=False (default unchanged).

implementation change covers:
- When RunConfig(account="acme-corp"), slack-threads step includes --account acme-corp.
- When RunConfig(account=None), no --account flag appears in any step command.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.datasync.cli import (
    MAX_VERBOSE_LINES,
    RunConfig,
    _build_steps,
    _run_step,
    _truncate_output,
    cli,
)
from fieldkit.config import TIMEOUT_DATASYNC

pytestmark = pytest.mark.unit


def test_verbose_help_discloses_output_limit() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "last 100 lines" in result.output
    assert "full subprocess output" not in result.output


# ---------------------------------------------------------------------------
# implementation change: _build_steps account scoping for slack-threads
# ---------------------------------------------------------------------------


# ── TestBuildStepsAccountScoping (flattened) ────────────────────────────────


def test_build_steps_account_scoped_slack_threads_includes_account_flag() -> None:
    """slack-threads step must include --account when cfg.account is set."""
    cfg = RunConfig(account="acme-corp")
    steps = _build_steps(cfg)

    slack_cmd = next((cmd for label, cmd in steps if label == "slack-threads"), None)
    assert slack_cmd is not None, "slack-threads step not found in _build_steps output"
    assert "--account" in slack_cmd, "slack-threads step missing --account flag"
    assert "acme-corp" in slack_cmd, "slack-threads step missing account value"

    # Verify the flag appears in the correct position
    idx = slack_cmd.index("--account")
    assert slack_cmd[idx + 1] == "acme-corp"


def test_build_steps_account_scoped_backstory_and_stalls_also_scoped() -> None:
    """Existing scoping for backstory-health and pursuit-stalls is preserved."""
    cfg = RunConfig(account="acme-corp")
    steps = _build_steps(cfg)

    for label in ("backstory-health", "pursuit-stalls"):
        cmd = next((cmd for lbl, cmd in steps if lbl == label), None)
        assert cmd is not None, f"{label} step not found"
        assert "--account" in cmd, f"{label} step missing --account flag"
        assert "acme-corp" in cmd, f"{label} step missing account value"


def test_build_steps_no_account_flag_when_account_is_none() -> None:
    """When RunConfig(account=None), no --account flag appears in any step."""
    cfg = RunConfig(account=None)
    steps = _build_steps(cfg)

    for label, cmd in steps:
        assert "--account" not in cmd, f"Step '{label}' unexpectedly contains --account when account=None: {cmd}"


def test_build_steps_account_scoped_full_run_step_count_unchanged() -> None:
    """Adding --account to slack-threads does not change the step count."""
    cfg_no_account = RunConfig()
    cfg_with_account = RunConfig(account="acme-corp")
    assert len(_build_steps(cfg_no_account)) == len(_build_steps(cfg_with_account))


# ---------------------------------------------------------------------------
# implementation change: _run_step verbose output
# ---------------------------------------------------------------------------


# ── TestRunStepVerbose (flattened) ──────────────────────────────────────────


def _run_step_make_completed_process(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_run_step_verbose_true_prints_stderr_on_failure() -> None:
    """Full stderr is printed to stderr when verbose=True and step fails."""
    multi_line_stderr = "line 1: error\nline 2: detail\nline 3: traceback"
    proc = _run_step_make_completed_process(returncode=1, stderr=multi_line_stderr)

    captured_lines: list[str] = []

    def fake_echo(msg: str = "", *, err: bool = False, **kwargs: object) -> None:
        captured_lines.append(msg)

    with (
        patch("fieldkit.commands.datasync.cli.click.echo", side_effect=fake_echo),
        patch("subprocess.run", return_value=proc),
    ):
        _run_step(1, 1, "test-step", ["fieldkit", "watch", "test"], dry_run=False, verbose=True)

    output_text = "\n".join(captured_lines)
    assert "line 1: error" in output_text, "Full stderr line 1 not in verbose output"
    assert "line 2: detail" in output_text, "Full stderr line 2 not in verbose output"
    assert "line 3: traceback" in output_text, "Full stderr line 3 not in verbose output"


def test_run_step_verbose_true_prints_stdout_on_success() -> None:
    """Full stdout is printed when verbose=True even on success."""
    multi_line_stdout = "step started\nstep completed\nrecords: 42"
    proc = _run_step_make_completed_process(returncode=0, stdout=multi_line_stdout)

    captured_lines: list[str] = []

    def fake_echo(msg: str = "", *, err: bool = False, **kwargs: object) -> None:
        captured_lines.append(msg)

    with (
        patch("fieldkit.commands.datasync.cli.click.echo", side_effect=fake_echo),
        patch("subprocess.run", return_value=proc),
    ):
        _run_step(1, 1, "test-step", ["fieldkit", "watch", "test"], dry_run=False, verbose=True)

    output_text = "\n".join(captured_lines)
    assert "step started" in output_text
    assert "step completed" in output_text
    assert "records: 42" in output_text


def test_run_step_verbose_false_suppresses_full_output() -> None:
    """Full subprocess output is NOT printed when verbose=False (default)."""
    multi_line_stderr = "line 1: error\nline 2: detail\nline 3: traceback"
    proc = _run_step_make_completed_process(returncode=1, stderr=multi_line_stderr)

    captured_lines: list[str] = []

    def fake_echo(msg: str = "", *, err: bool = False, **kwargs: object) -> None:
        captured_lines.append(msg)

    with (
        patch("fieldkit.commands.datasync.cli.click.echo", side_effect=fake_echo),
        patch("subprocess.run", return_value=proc),
    ):
        _run_step(1, 1, "test-step", ["fieldkit", "watch", "test"], dry_run=False, verbose=False)

    output_text = "\n".join(captured_lines)
    # The summary line contains the first line of stderr (truncated), but
    # the full multi-line content must NOT appear verbatim.
    assert "line 2: detail" not in output_text, "verbose=False must not print full stderr"
    assert "line 3: traceback" not in output_text, "verbose=False must not print full stderr"


def test_run_step_verbose_default_is_false() -> None:
    """_run_step verbose parameter defaults to False (backward-compatible)."""
    proc = _run_step_make_completed_process(returncode=0, stdout="ok")

    captured_lines: list[str] = []

    def fake_echo(msg: str = "", *, err: bool = False, **kwargs: object) -> None:
        captured_lines.append(msg)

    with (
        patch("fieldkit.commands.datasync.cli.click.echo", side_effect=fake_echo),
        patch("subprocess.run", return_value=proc),
    ):
        # Call without verbose keyword — must not raise
        _run_step(1, 1, "test-step", ["fieldkit", "watch", "test"], dry_run=False)

    # Only the summary line should appear (no verbose header)
    header_lines = [ln for ln in captured_lines if "--- test-step" in ln]
    assert header_lines == [], "Default (verbose=False) must not print verbose headers"


def test_run_step_retains_datasync_timeout() -> None:
    proc = _run_step_make_completed_process(returncode=0)
    with patch("subprocess.run", return_value=proc) as run:
        result = _run_step(1, 1, "test-step", ["fieldkit", "watch", "test"], dry_run=False)

    assert result.success is True
    assert run.call_args.kwargs["timeout"] == TIMEOUT_DATASYNC


def test_run_step_verbose_empty_output_no_header() -> None:
    """No verbose header is printed when subprocess output is empty."""
    proc = _run_step_make_completed_process(returncode=0, stdout="", stderr="")

    captured_lines: list[str] = []

    def fake_echo(msg: str = "", *, err: bool = False, **kwargs: object) -> None:
        captured_lines.append(msg)

    with (
        patch("fieldkit.commands.datasync.cli.click.echo", side_effect=fake_echo),
        patch("subprocess.run", return_value=proc),
    ):
        _run_step(1, 1, "test-step", ["fieldkit", "watch", "test"], dry_run=False, verbose=True)

    header_lines = [ln for ln in captured_lines if "--- test-step" in ln]
    assert header_lines == [], "No verbose header when subprocess output is empty"


# ---------------------------------------------------------------------------
# implementation change: CLI --verbose flag integration
# ---------------------------------------------------------------------------


# ── TestCliVerboseFlag (flattened) ──────────────────────────────────────────


@pytest.fixture
def _cli_verbose_runner():
    """Set up CliRunner with preflight bypass (implementation note)."""
    runner = CliRunner()
    # implementation note: bypass preflight checks in unit tests (no real credentials).
    patcher = patch("fieldkit.watch.preflight.preflight_check", return_value=[])
    patcher.start()
    yield runner
    patcher.stop()


def test_cli_verbose_flag_in_help(_cli_verbose_runner) -> None:
    """--verbose appears in the sync help text."""
    result = _cli_verbose_runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "--verbose" in result.output


def test_cli_verbose_dry_run_exits_zero(_cli_verbose_runner) -> None:
    """--verbose --dry-run exits 0 (dry-run skips subprocess)."""
    result = _cli_verbose_runner.invoke(cli, ["--verbose", "--dry-run"])
    assert result.exit_code == 0


def test_cli_verbose_flag_passed_to_run_pipeline(_cli_verbose_runner) -> None:
    """--verbose is threaded through to run_pipeline via RunConfig."""
    captured_configs: list[RunConfig] = []

    def fake_run_pipeline(cfg: RunConfig) -> list[object]:
        captured_configs.append(cfg)
        return []

    with patch("fieldkit.commands.datasync.cli.run_pipeline", side_effect=fake_run_pipeline):
        _cli_verbose_runner.invoke(cli, ["--verbose", "--dry-run"])

    assert captured_configs, "run_pipeline was not called"
    assert captured_configs[0].verbose is True, "verbose=True not passed to RunConfig"


def test_cli_no_verbose_flag_defaults_false(_cli_verbose_runner) -> None:
    """Omitting --verbose results in RunConfig.verbose=False."""
    captured_configs: list[RunConfig] = []

    def fake_run_pipeline(cfg: RunConfig) -> list[object]:
        captured_configs.append(cfg)
        return []

    with patch("fieldkit.commands.datasync.cli.run_pipeline", side_effect=fake_run_pipeline):
        _cli_verbose_runner.invoke(cli, ["--dry-run"])

    assert captured_configs, "run_pipeline was not called"
    assert captured_configs[0].verbose is False, "verbose should default to False"


# ---------------------------------------------------------------------------
# Review fix: verbose output line-count cap (_truncate_output)
# ---------------------------------------------------------------------------


# ── TestTruncateOutput (flattened) ──────────────────────────────────────────


def test_truncate_output_short_output_returned_unchanged() -> None:
    """Output with fewer lines than the cap is returned verbatim."""
    text = "\n".join(f"line {i}" for i in range(10))
    assert _truncate_output(text) == text


def test_truncate_output_output_at_exact_cap_returned_unchanged() -> None:
    """Output with exactly MAX_VERBOSE_LINES lines is returned verbatim."""
    text = "\n".join(f"line {i}" for i in range(MAX_VERBOSE_LINES))
    assert _truncate_output(text) == text


def test_truncate_output_output_over_cap_is_truncated() -> None:
    """Output exceeding MAX_VERBOSE_LINES is truncated to the tail."""
    total = MAX_VERBOSE_LINES + 50
    lines = [f"line {i}" for i in range(total)]
    result = _truncate_output("\n".join(lines))

    result_lines = result.splitlines()
    # Last content line before the truncation notice must be the last original line
    assert result_lines[-2] == f"line {total - 1}", "Tail lines must be preserved"
    # A truncation notice must be appended
    assert "truncated" in result_lines[-1].lower(), "Truncation notice must be present"
    assert str(total) in result_lines[-1], "Total line count must appear in notice"


def test_truncate_output_truncation_keeps_tail_not_head() -> None:
    """Truncation keeps the LAST max_lines lines, not the first."""
    lines = [f"line {i}" for i in range(200)]
    result = _truncate_output("\n".join(lines), max_lines=10)

    result_lines = result.splitlines()
    # Head lines must be dropped
    assert "line 0" not in result_lines, "Head lines must be dropped"
    # Tail lines must be present
    assert "line 199" in result_lines, "Last line must be present"
    assert "line 190" in result_lines, "Lines near the tail must be present"


def test_truncate_output_custom_max_lines_respected() -> None:
    """Custom max_lines parameter overrides the default."""
    text = "\n".join(f"line {i}" for i in range(20))
    result = _truncate_output(text, max_lines=5)

    result_lines = result.splitlines()
    # 5 content lines + 1 truncation notice
    assert len(result_lines) == 6, f"Expected 6 lines (5 content + notice), got {len(result_lines)}"


def test_truncate_output_verbose_step_output_is_capped() -> None:
    """_run_step verbose output is capped at MAX_VERBOSE_LINES per stream."""
    # Generate output with 3x the cap to ensure truncation fires
    big_stderr = "\n".join(f"error line {i}" for i in range(MAX_VERBOSE_LINES * 3))
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = ""
    proc.stderr = big_stderr

    captured_lines: list[str] = []

    def fake_echo(msg: str = "", *, err: bool = False, **kwargs: object) -> None:
        captured_lines.append(msg)

    with (
        patch("fieldkit.commands.datasync.cli.click.echo", side_effect=fake_echo),
        patch("subprocess.run", return_value=proc),
    ):
        _run_step(1, 1, "test-step", ["fieldkit", "watch", "test"], dry_run=False, verbose=True)

    # Isolate the verbose block: lines after the "--- test-step stderr ---" header
    all_output = "\n".join(captured_lines)
    assert "--- test-step stderr ---" in all_output, "Verbose header must be present"

    # The verbose content (the echo call after the header) must contain the truncation notice
    # and must NOT contain early head lines (they should have been dropped by the cap).
    # The summary line (before the header) may contain "error line 0" as the step note —
    # we only check the verbose block itself.
    header_idx = next(i for i, ln in enumerate(captured_lines) if "--- test-step stderr ---" in ln)
    verbose_content = "\n".join(captured_lines[header_idx + 1 :])
    assert "truncated" in verbose_content.lower(), "Truncation notice must appear in verbose content"
    # Head lines must be absent from the verbose content (they were dropped by the cap)
    assert "error line 0\n" not in verbose_content and not verbose_content.startswith("error line 0"), (
        "Head lines must be dropped by cap in verbose content"
    )
