"""Fail-closed tests for the pinned skill quality gate."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts import check_skillsaw, quality_plan

pytestmark = pytest.mark.unit


def test_tool_pin_is_locked_in_development_environment() -> None:
    """The executable policy and dependency closure use the same exact tool version."""
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    assert f"skillsaw=={check_skillsaw._SKILLSAW_VERSION}" in project["dependency-groups"]["dev"]
    packages = [package for package in lock["package"] if package["name"] == "skillsaw"]
    assert len(packages) == 1
    assert packages[0]["version"] == check_skillsaw._SKILLSAW_VERSION


def test_version_mismatch_prevents_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An installed tool with different behavior cannot inherit the frozen policy."""
    monkeypatch.setattr(check_skillsaw, "version", lambda _name: "0.0.0")
    with (
        patch.object(check_skillsaw.process_supervision, "run_bounded") as run,
        pytest.raises(SystemExit, match="1"),
    ):
        check_skillsaw.main()
    run.assert_not_called()


def test_public_gate_never_dispatches_to_private_files() -> None:
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["skillsaw"]["steps"]
    check_step = next(step for step in steps if step.get("name", "").startswith("Run skillsaw"))
    full = quality_plan.build_plan("full", repo=root)
    command = next(stage for stage in full.stages if stage.label == "skillsaw").commands[0].argv

    assert check_step["run"] == "uv run python scripts/check_skillsaw.py"
    assert command == ("uv", "run", "python", "scripts/check_skillsaw.py")
    assert all(".opencode/check_skillsaw.py" not in argument for argument in command)


def _report() -> dict[str, object]:
    return {
        "summary": {
            "errors": 0,
            "warnings": 0,
            "info": 106,
            "baseline_suppressed": 0,
            "grade": {"letter": "A", "density": 2.52},
        }
    }


def test_ci_proves_containment_on_supported_contributor_platforms() -> None:
    """Both native runners must exercise the real supervisor before linting."""
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["skillsaw"]
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-15"]
    assert job["strategy"]["fail-fast"] is False
    assert job.get("continue-on-error", False) is False
    commands = [step.get("run") for step in job["steps"]]
    containment = "uv run pytest tests/test_process_supervision.py tests/test_check_skillsaw.py -q -n 0"
    scan = "uv run python scripts/check_skillsaw.py"
    install = "uv sync --locked --all-extras --dev"
    assert commands.index(install) < commands.index(containment) < commands.index(scan)
    critical_steps = [step for step in job["steps"] if step.get("run") in (install, containment, scan)]
    assert len(critical_steps) == 3
    assert all(step.get("continue-on-error", False) is False for step in critical_steps)
    assert all(step["if"] == "needs.changes.outputs.code == 'true'" for step in critical_steps)
    assert "skillsaw" in workflow["jobs"]["required-checks"]["needs"]
    aggregate = next(
        step
        for step in workflow["jobs"]["required-checks"]["steps"]
        if step.get("name") == "Evaluate every required child"
    )
    assert aggregate["env"]["SKILLSAW_RESULT"] == "${{ needs.skillsaw.result }}"
    assert '--child "Skillsaw (skill lint)=$SKILLSAW_RESULT"' in aggregate["run"]


def test_success_uses_bounded_safe_tool_invocation() -> None:
    completed = subprocess.CompletedProcess([], 0, json.dumps(_report()), "")
    with patch.object(check_skillsaw.process_supervision, "run_bounded", return_value=completed) as run:
        result = check_skillsaw.main()
    assert result is None
    run.assert_called_once_with(
        [
            sys.executable,
            "-I",
            "-m",
            "skillsaw",
            "lint",
            ".",
            "--format",
            "json",
            "--no-progress",
            "--fail-on",
            "warning",
            "--no-custom-rules",
            "--no-plugins",
            "--no-baseline",
        ],
        timeout_seconds=120,
        output_limit=8 * 1024**2,
        cwd=Path.cwd(),
    )


@pytest.mark.parametrize("exit_code", [1, 2, 127])
def test_nonzero_tool_exit_never_passes(exit_code: int) -> None:
    completed = subprocess.CompletedProcess([], exit_code, json.dumps(_report()), "failed")
    with (
        patch.object(check_skillsaw.process_supervision, "run_bounded", return_value=completed),
        pytest.raises(SystemExit, match="1"),
    ):
        check_skillsaw.main()


@pytest.mark.parametrize("suppressed", [1, 37])
def test_public_gate_rejects_suppressions(suppressed: int) -> None:
    report = _report()
    summary = report["summary"]
    assert isinstance(summary, dict)
    summary["baseline_suppressed"] = suppressed
    completed = subprocess.CompletedProcess([], 0, json.dumps(report), "")
    with (
        patch.object(check_skillsaw.process_supervision, "run_bounded", return_value=completed),
        pytest.raises(SystemExit, match="1"),
    ):
        check_skillsaw.main()


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("skillsaw", 120), OSError("not available")])
def test_tool_launch_failure_is_controlled(error: Exception, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(check_skillsaw.process_supervision, "run_bounded", side_effect=error),
        pytest.raises(SystemExit, match="1"),
    ):
        check_skillsaw.main()
    assert "skillsaw" in capsys.readouterr().err


@pytest.mark.parametrize(
    "summary",
    [
        None,
        {},
        {"grade": {"letter": "A", "density": 0}},
        {"errors": 0, "warnings": 0, "info": 0, "baseline_suppressed": 0, "grade": {"density": 0}},
        {"errors": -1, "warnings": 0, "info": 0, "baseline_suppressed": 0, "grade": {"letter": "A", "density": 0}},
        {
            "errors": 0,
            "warnings": 0,
            "info": 0,
            "baseline_suppressed": 0,
            "grade": {"letter": "A", "density": float("nan")},
        },
    ],
)
def test_malformed_summary_never_passes(summary: object) -> None:
    completed = subprocess.CompletedProcess([], 0, json.dumps({"summary": summary}), "")
    with (
        patch.object(check_skillsaw.process_supervision, "run_bounded", return_value=completed),
        pytest.raises(SystemExit, match="1"),
    ):
        check_skillsaw.main()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("errors", 1),
        ("warnings", 1),
        ("grade", {"letter": "B", "density": 2}),
        ("grade", {"letter": "A", "density": 2.71}),
    ],
)
def test_frozen_thresholds_reject_regressions(field: str, value: object) -> None:
    report = _report()
    summary = report["summary"]
    assert isinstance(summary, dict)
    summary[field] = value
    completed = subprocess.CompletedProcess([], 0, json.dumps(report), "")
    with (
        patch.object(check_skillsaw.process_supervision, "run_bounded", return_value=completed),
        pytest.raises(SystemExit, match="1"),
    ):
        check_skillsaw.main()


@pytest.mark.parametrize(("grade", "passes"), [("A+", True), ("A", True), ("A-", False), ("B+", False)])
def test_grade_floor_accepts_improvement(grade: str, passes: bool) -> None:
    result = check_skillsaw._grade_ok(grade, "A")
    assert result is passes


def test_private_runner_preserves_existing_baseline_policy() -> None:
    report = _report()
    summary = report["summary"]
    assert isinstance(summary, dict)
    summary["baseline_suppressed"] = 37
    completed = subprocess.CompletedProcess([], 0, json.dumps(report), "")
    with patch.object(check_skillsaw.process_supervision, "run_bounded", return_value=completed) as run:
        result = check_skillsaw.run_check(Path.cwd(), allow_baseline=True)
    assert result is None
    assert "--no-baseline" not in run.call_args.args[0]
