"""Tests for fieldkit.skill.eval_runner._render_eval_result — branch coverage."""

import json
import subprocess
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.skill.eval_runner import _render_eval_result

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(
    skill: str = "my-skill",
    has_evals: bool = True,
    static_pass: int = 2,
    static_fail: int = 0,
    static_results: list[dict[str, Any]] | None = None,
    behavioral_cases: list[dict[str, Any]] | None = None,
    no_evals_message: str = "",
) -> dict[str, Any]:
    return {
        "skill": skill,
        "has_evals": has_evals,
        "static_pass": static_pass,
        "static_fail": static_fail,
        "static_results": static_results or [],
        "behavioral_cases": behavioral_cases or [],
        "no_evals_message": no_evals_message,
    }


def _capture(result: dict[str, Any], **kwargs: Any) -> str:
    """Capture printed output from _render_eval_result."""
    buf = StringIO()
    with patch("click.echo", side_effect=lambda *a, **kw: buf.write(str(a[0]) + "\n") if a else buf.write("\n")):
        _render_eval_result(result, **kwargs)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# No evals path
# ---------------------------------------------------------------------------


# ── TestNoEvals (flattened) ─────────────────────────────────────────────────


def test_run_evals_no_evals_message_default() -> None:
    out = _capture(_result(has_evals=False, no_evals_message=""))
    assert "No evals.json" in out or "create evals" in out


def test_run_evals_no_evals_message_custom() -> None:
    out = _capture(_result(has_evals=False, no_evals_message="run scaffold to enable"))
    assert "run scaffold to enable" in out


def test_run_evals_no_evals_returns_early() -> None:
    """When has_evals=False, no static check summary should be printed."""
    out = _capture(_result(has_evals=False))
    assert "Static checks" not in out


def test_run_evals_skill_name_always_printed() -> None:
    out = _capture(_result(skill="my-cool-skill", has_evals=False))
    assert "my-cool-skill" in out


# ---------------------------------------------------------------------------
# Static checks: zero checks
# ---------------------------------------------------------------------------


# ── TestStaticChecksZero (flattened) ────────────────────────────────────────


def test_run_static_check_no_static_checks_defined() -> None:
    out = _capture(_result(static_pass=0, static_fail=0, static_results=[]))
    assert "No static checks defined" in out


# ---------------------------------------------------------------------------
# Static checks: passing
# ---------------------------------------------------------------------------


# ── TestStaticChecksPassing (flattened) ─────────────────────────────────────


def test_run_static_check_all_passing_checkmark() -> None:
    checks = [
        {"id": "c1", "passed": True, "message": "Has trigger words", "detail": ""},
        {"id": "c2", "passed": True, "message": "Has examples", "detail": ""},
    ]
    out = _capture(_result(static_pass=2, static_fail=0, static_results=checks))
    assert "✅" in out
    assert "2/2" in out


def test_run_static_check_passing_check_uses_message() -> None:
    checks = [{"id": "c1", "passed": True, "message": "Trigger words present", "detail": ""}]
    out = _capture(_result(static_pass=1, static_fail=0, static_results=checks))
    assert "Trigger words present" in out


def test_run_static_check_passing_check_uses_id_when_no_message() -> None:
    checks = [{"id": "my-check-id", "passed": True, "message": "", "detail": ""}]
    out = _capture(_result(static_pass=1, static_fail=0, static_results=checks))
    assert "my-check-id" in out


# ---------------------------------------------------------------------------
# Static checks: failing
# ---------------------------------------------------------------------------


# ── TestStaticChecksFailing (flattened) ─────────────────────────────────────


def test_run_static_check_fail_shows_x_mark() -> None:
    checks = [{"id": "c1", "passed": False, "message": "Missing trigger", "detail": ""}]
    out = _capture(_result(static_pass=0, static_fail=1, static_results=checks))
    assert "❌" in out


def test_run_static_check_fail_shows_detail() -> None:
    checks = [{"id": "c1", "passed": False, "message": "Missing trigger", "detail": "Pattern not found: 'use when'"}]
    out = _capture(_result(static_pass=0, static_fail=1, static_results=checks))
    assert "Pattern not found" in out


def test_run_static_check_fail_no_detail_not_printed() -> None:
    checks = [{"id": "c1", "passed": False, "message": "Missing trigger", "detail": ""}]
    out = _capture(_result(static_pass=0, static_fail=1, static_results=checks))
    assert "→" not in out


def test_run_static_check_mixed_pass_fail() -> None:
    checks = [
        {"id": "c1", "passed": True, "message": "OK", "detail": ""},
        {"id": "c2", "passed": False, "message": "Bad", "detail": "Missing X"},
    ]
    out = _capture(_result(static_pass=1, static_fail=1, static_results=checks))
    assert "1/2" in out
    assert "Missing X" in out


# ---------------------------------------------------------------------------
# Behavioral cases
# ---------------------------------------------------------------------------


# ── TestBehavioralCases (flattened) ─────────────────────────────────────────


def _run_behavioral_evals_behavioral_case(case_id: str = "case-1") -> dict[str, Any]:
    return {
        "id": case_id,
        "prompt": "Ask agent to do X",
        "expected_behavior": "Should trigger Y",
        "assertions": [{"text": "Verify output contains Z"}],
    }


def test_run_behavioral_evals_behavioral_shown_by_default() -> None:
    cases = [_run_behavioral_evals_behavioral_case()]
    out = _capture(_result(behavioral_cases=cases), show_behavioral=True)
    assert "Behavioral evals" in out
    assert "Ask agent to do X" in out


def test_run_behavioral_evals_behavioral_hidden_when_flag_false() -> None:
    cases = [_run_behavioral_evals_behavioral_case()]
    out = _capture(_result(behavioral_cases=cases), show_behavioral=False)
    assert "Behavioral evals" not in out
    assert "Ask agent to do X" not in out


def test_run_behavioral_evals_behavioral_count_in_header() -> None:
    cases = [_run_behavioral_evals_behavioral_case("c1"), _run_behavioral_evals_behavioral_case("c2")]
    out = _capture(_result(behavioral_cases=cases), show_behavioral=True)
    assert "2 cases" in out


def test_run_behavioral_evals_assertions_listed() -> None:
    cases = [_run_behavioral_evals_behavioral_case()]
    out = _capture(_result(behavioral_cases=cases), show_behavioral=True)
    assert "Verify output contains Z" in out


def test_run_behavioral_evals_no_behavioral_no_section_when_empty() -> None:
    out = _capture(_result(behavioral_cases=[]), show_behavioral=True)
    assert "Behavioral evals" not in out


def test_run_behavioral_evals_expected_behavior_shown() -> None:
    cases = [_run_behavioral_evals_behavioral_case()]
    out = _capture(_result(behavioral_cases=cases), show_behavioral=True)
    assert "Should trigger Y" in out


# ---------------------------------------------------------------------------
# Tests for uncovered lines: static check internals
# ---------------------------------------------------------------------------


# ── TestRunStaticCheck (flattened) ──────────────────────────────────────────


def test_run_static_check_contains_passes() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    result = _run_static_check({"id": "c1", "type": "contains", "pattern": "foo", "message": "Has foo"}, "foo bar")
    assert result["passed"] is True
    assert result["detail"] == ""


def test_run_static_check_contains_fails() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    result = _run_static_check({"id": "c1", "type": "contains", "pattern": "missing", "message": ""}, "foo bar")
    assert result["passed"] is False
    assert "missing" in result["detail"]


def test_run_static_check_not_contains_passes() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    result = _run_static_check({"id": "c1", "type": "not_contains", "pattern": "forbidden", "message": ""}, "safe text")
    assert result["passed"] is True


def test_run_static_check_not_contains_fails() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    result = _run_static_check(
        {"id": "c1", "type": "not_contains", "pattern": "forbidden", "message": ""}, "forbidden word here"
    )
    assert result["passed"] is False


def test_run_static_check_contains_all_passes() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    result = _run_static_check(
        {"id": "c1", "type": "contains_all", "patterns": ["foo", "bar"], "message": ""}, "foo and bar"
    )
    assert result["passed"] is True


def test_run_static_check_contains_all_fails_missing() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    result = _run_static_check(
        {"id": "c1", "type": "contains_all", "patterns": ["foo", "baz"], "message": ""}, "foo only"
    )
    assert result["passed"] is False
    assert "baz" in result["detail"]


def test_run_static_check_commands_exist_pass() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    mock_result = MagicMock()
    mock_result.stdout = "some-subcmd  Description"
    mock_result.stderr = ""

    with patch("fieldkit.commands.skill.eval_runner.subprocess.run", return_value=mock_result):
        result = _run_static_check(
            {"id": "c1", "type": "commands_exist", "commands": ["fieldkit some-subcmd"], "message": ""},
            "any text",
        )
    assert result["passed"] is True


def test_run_static_check_commands_exist_fail_not_in_output() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    mock_result = MagicMock()
    mock_result.stdout = "other stuff"
    mock_result.stderr = ""

    with patch("fieldkit.commands.skill.eval_runner.subprocess.run", return_value=mock_result):
        result = _run_static_check(
            {"id": "c1", "type": "commands_exist", "commands": ["fieldkit missing-subcmd"], "message": ""},
            "any text",
        )
    assert result["passed"] is False


def test_run_static_check_commands_exist_timeout() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    with patch(
        "fieldkit.commands.skill.eval_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("fieldkit", 10)
    ):
        result = _run_static_check(
            {"id": "c1", "type": "commands_exist", "commands": ["fieldkit slow"], "message": ""},
            "any text",
        )
    assert result["passed"] is False


def test_run_static_check_commands_exist_file_not_found() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    with patch("fieldkit.commands.skill.eval_runner.subprocess.run", side_effect=FileNotFoundError()):
        result = _run_static_check(
            {"id": "c1", "type": "commands_exist", "commands": ["fieldkit missing"], "message": ""},
            "any text",
        )
    assert result["passed"] is False


def test_run_static_check_unknown_check_type() -> None:
    from fieldkit.commands.skill.eval_runner import _run_static_check

    result = _run_static_check({"id": "c1", "type": "bogus_type", "message": ""}, "text")
    assert result["passed"] is False
    assert "Unknown check type" in result["detail"]


# ---------------------------------------------------------------------------
# _resolve_skill_dirs
# ---------------------------------------------------------------------------


# ── TestResolveSkillDirs (flattened) ────────────────────────────────────────


def test_resolve_skill_dirs_run_all_returns_all_dirs(tmp_path: Path) -> None:
    from fieldkit.commands.skill.eval_runner import _resolve_skill_dirs

    (tmp_path / "skill-a").mkdir()
    (tmp_path / "skill-b").mkdir()
    (tmp_path / "_private").mkdir()  # should be excluded

    dirs = _resolve_skill_dirs(tmp_path, run_all=True, skill_names=[])
    assert dirs is not None
    names = [d.name for d in dirs]
    assert "skill-a" in names
    assert "skill-b" in names
    assert "_private" not in names


def test_resolve_skill_dirs_specific_skill_found(tmp_path: Path) -> None:
    from fieldkit.commands.skill.eval_runner import _resolve_skill_dirs

    (tmp_path / "my-skill").mkdir()
    dirs = _resolve_skill_dirs(tmp_path, run_all=False, skill_names=["my-skill"])
    assert dirs is not None
    assert len(dirs) == 1
    assert dirs[0].name == "my-skill"


def test_resolve_skill_dirs_skill_not_found_returns_none(tmp_path: Path) -> None:
    from fieldkit.commands.skill.eval_runner import _resolve_skill_dirs

    dirs = _resolve_skill_dirs(tmp_path, run_all=False, skill_names=["nonexistent"])
    assert dirs is None


def test_resolve_skill_dirs_no_args_returns_none(tmp_path: Path) -> None:
    from fieldkit.commands.skill.eval_runner import _resolve_skill_dirs

    dirs = _resolve_skill_dirs(tmp_path, run_all=False, skill_names=[])
    assert dirs is None


# ---------------------------------------------------------------------------
# _print_eval_summary
# ---------------------------------------------------------------------------


# ── TestPrintEvalSummary (flattened) ────────────────────────────────────────


def test_print_eval_summary_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.skill.eval_runner import _print_eval_summary

    results = [
        {"skill": "s1", "has_evals": True, "static_pass": 2, "static_fail": 0},
        {"skill": "s2", "has_evals": False, "static_pass": 0, "static_fail": 0},
    ]
    _print_eval_summary(results, 0, json_output=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["evaluated"] == 2
    assert data["with_evals"] == 1
    assert data["without_evals"] == 1


def test_print_eval_summary_human_all_passing(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.skill.eval_runner import _print_eval_summary

    results = [{"skill": "s1", "has_evals": True, "static_pass": 3, "static_fail": 0}]
    _print_eval_summary(results, 0, json_output=False)
    out = capsys.readouterr().out
    assert "✅" in out


def test_print_eval_summary_human_failures(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.skill.eval_runner import _print_eval_summary

    results = [{"skill": "s1", "has_evals": True, "static_pass": 1, "static_fail": 2}]
    _print_eval_summary(results, 2, json_output=False)
    out = capsys.readouterr().out
    assert "❌" in out or "failing" in out


def test_print_eval_summary_no_static_checks_no_static_line(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.skill.eval_runner import _print_eval_summary

    results = [{"skill": "s1", "has_evals": False, "static_pass": 0, "static_fail": 0}]
    _print_eval_summary(results, 0, json_output=False)
    out = capsys.readouterr().out
    assert "Skills evaluated:  1" in out
