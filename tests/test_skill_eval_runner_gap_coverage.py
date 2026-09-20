"""Gap-coverage tests for eval_runner._resolve_behavioral_skill_dirs() and run_eval_cmd().

Targets two CRAP-flagged functions in commands/skill/eval_runner.py:
- _resolve_behavioral_skill_dirs (CRAP=18.86, complexity=14)
- run_eval_cmd (CRAP=18.67, complexity=13)

Both are already covered by tests/test_eval_runner.py, tests/test_skill_eval_behavioral.py,
tests/test_skill_eval_render.py, and tests/test_skill_eval_resolve_dirs.py (which covers the
sibling static resolver _resolve_skill_dirs, not the behavioral variant tested here). This
file fills the specific remaining branch gaps identified from the coverage artifact:

_resolve_behavioral_skill_dirs:
  (a) run_all=True with zero qualifying candidates (fail-loud guard)
  (b) skill_names containing a name in _BEHAVIORAL_SKIP (distinct from "not found")
  (c) --limit truncation applied to the named-skills branch
  (d) neither run_all nor skill_names provided (usage fallthrough)

run_eval_cmd:
  (a) skills_dir.is_dir() False guard, before any dispatch
  (b) scaffold_name matching an existing directory
  (c) scaffold_name given but no matching directory found (not-found branch)
  (d) the static eval path (_run_evals + _print_eval_summary + exit-code ternary)
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.skill.eval_runner import _resolve_behavioral_skill_dirs, run_eval_cmd

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skills_dir(tmp_path: Path, names: list[str]) -> Path:
    """Create a bare skills directory with an empty subdirectory per name."""
    skills = tmp_path / "skills"
    skills.mkdir()
    for name in names:
        (skills / name).mkdir()
    return skills


def _make_behavioral_skills_dir(tmp_path: Path, names: list[str]) -> Path:
    """Create a skills directory where each named skill has evals/evals.json."""
    skills = tmp_path / "skills"
    skills.mkdir()
    for name in names:
        evals_dir = skills / name / "evals"
        evals_dir.mkdir(parents=True)
        (evals_dir / "evals.json").write_text('{"evals": []}', encoding="utf-8")
    return skills


# ---------------------------------------------------------------------------
# _resolve_behavioral_skill_dirs
# ---------------------------------------------------------------------------


def test_resolve_behavioral_skill_dirs_run_all_no_candidates_prints_and_returns_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run_all=True over an empty skills dir fails loudly instead of returning an empty list."""
    skills_dir = _make_skills_dir(tmp_path, [])

    result = _resolve_behavioral_skill_dirs(skills_dir, run_all=True, skill_names=[], limit=None)

    assert result is None
    captured = capsys.readouterr()
    assert "No skills with evals/evals.json found" in captured.err


def test_resolve_behavioral_skill_dirs_run_all_all_excluded_still_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run_all=True where every subdir is skip-listed or lacks evals.json still triggers the guard."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "no-evals").mkdir()  # exists, but no evals/evals.json
    behavioral_skip_dir = skills / "skipped"
    (behavioral_skip_dir / "evals").mkdir(parents=True)
    (behavioral_skip_dir / "evals" / "evals.json").write_text("{}", encoding="utf-8")

    with patch("fieldkit.commands.skill.eval_runner._BEHAVIORAL_SKIP", frozenset({"skipped"})):
        result = _resolve_behavioral_skill_dirs(skills, run_all=True, skill_names=[], limit=None)

    assert result is None
    captured = capsys.readouterr()
    assert "No skills with evals/evals.json found" in captured.err


def test_resolve_behavioral_skill_dirs_skill_names_skip_listed_stops_before_not_found_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A requested name in _BEHAVIORAL_SKIP is rejected by the skip guard, not the not-found guard."""
    skills_dir = _make_skills_dir(tmp_path, ["other-skill"])  # "skipped-skill" is not a real dir

    with patch("fieldkit.commands.skill.eval_runner._BEHAVIORAL_SKIP", frozenset({"skipped-skill"})):
        result = _resolve_behavioral_skill_dirs(skills_dir, run_all=False, skill_names=["skipped-skill"], limit=None)

    assert result is None
    captured = capsys.readouterr()
    assert "'skipped-skill' is excluded from behavioral evals" in captured.err
    assert "Skill not found" not in captured.err


def test_resolve_behavioral_skill_dirs_skill_names_limit_truncates_in_request_order(
    tmp_path: Path,
) -> None:
    """--skill a --skill b --skill c with limit=2 keeps the first two, in request order (not sorted)."""
    skills_dir = _make_behavioral_skills_dir(tmp_path, ["a", "b", "c"])

    result = _resolve_behavioral_skill_dirs(skills_dir, run_all=False, skill_names=["a", "b", "c"], limit=2)

    assert result is not None
    assert [d.name for d in result] == ["a", "b"]


def test_resolve_behavioral_skill_dirs_neither_run_all_nor_skill_names_prints_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither --all nor --skill provided falls through to the usage message and returns None."""
    skills_dir = _make_skills_dir(tmp_path, ["some-skill"])

    result = _resolve_behavioral_skill_dirs(skills_dir, run_all=False, skill_names=[], limit=None)

    assert result is None
    captured = capsys.readouterr()
    assert "Usage: fieldkit skill eval --behavioral (--all | --skill NAME)" in captured.err


# ---------------------------------------------------------------------------
# run_eval_cmd
# ---------------------------------------------------------------------------


def test_run_eval_cmd_skills_dir_missing_returns_1_without_dispatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing skills directory short-circuits before calibrate/scaffold/behavioral dispatch."""
    missing_dir = tmp_path / "does-not-exist"

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=missing_dir),
        patch("fieldkit.commands.skill.eval_runner._run_evals") as mock_run_evals,
        patch("fieldkit.commands.skill.eval_runner._resolve_behavioral_skill_dirs") as mock_resolve_behavioral,
        patch("fieldkit.commands.skill.eval_runner._run_calibrate") as mock_run_calibrate,
    ):
        result = run_eval_cmd(skill_names=[], run_all=True)

    assert result == 1
    captured = capsys.readouterr()
    assert f"Skills directory not found: {missing_dir}" in captured.err
    mock_run_evals.assert_not_called()
    mock_resolve_behavioral.assert_not_called()
    mock_run_calibrate.assert_not_called()


def test_run_eval_cmd_scaffold_name_matches_calls_scaffold_evals_and_propagates_return(
    tmp_path: Path,
) -> None:
    """--scaffold <existing-skill> calls _scaffold_evals exactly once and returns its value verbatim."""
    skills_dir = _make_skills_dir(tmp_path, ["my-skill"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner._scaffold_evals", return_value=42) as mock_scaffold,
    ):
        result = run_eval_cmd(skill_names=[], scaffold_name="my-skill")

    assert result == 42
    mock_scaffold.assert_called_once_with(skills_dir / "my-skill")


def test_run_eval_cmd_scaffold_name_not_found_returns_1_without_calling_scaffold_evals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--scaffold <name> with no matching directory prints not-found and returns 1."""
    skills_dir = _make_skills_dir(tmp_path, ["other-skill"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner._scaffold_evals") as mock_scaffold,
    ):
        result = run_eval_cmd(skill_names=[], scaffold_name="missing-skill")

    assert result == 1
    captured = capsys.readouterr()
    assert "Skill not found: 'missing-skill'" in captured.err
    mock_scaffold.assert_not_called()


def test_run_eval_cmd_static_path_forwards_args_and_returns_0_when_no_failures(
    tmp_path: Path,
) -> None:
    """Static eval path forwards failed_only/json_output/no_behavioral and returns 0 on success."""
    skills_dir = _make_skills_dir(tmp_path, ["skill-a"])
    resolved_dirs = [skills_dir / "skill-a"]
    static_results = [{"skill": "skill-a"}]

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner._resolve_skill_dirs", return_value=resolved_dirs) as mock_resolve,
        patch("fieldkit.commands.skill.eval_runner._run_evals", return_value=(static_results, 0)) as mock_run_evals,
        patch("fieldkit.commands.skill.eval_runner._print_eval_summary") as mock_summary,
    ):
        result = run_eval_cmd(skill_names=[], run_all=True, failed_only=True, json_output=False, no_behavioral=True)

    assert result == 0
    mock_resolve.assert_called_once_with(skills_dir, True, [])
    mock_run_evals.assert_called_once_with(resolved_dirs, failed_only=True, json_output=False, no_behavioral=True)
    mock_summary.assert_called_once_with(static_results, 0, json_output=False)


def test_run_eval_cmd_static_path_returns_1_when_failures_present(tmp_path: Path) -> None:
    """Static eval path returns 1 when _run_evals reports overall_failures > 0."""
    skills_dir = _make_skills_dir(tmp_path, ["skill-a"])
    resolved_dirs = [skills_dir / "skill-a"]

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner._resolve_skill_dirs", return_value=resolved_dirs),
        patch("fieldkit.commands.skill.eval_runner._run_evals", return_value=([], 3)),
        patch("fieldkit.commands.skill.eval_runner._print_eval_summary"),
    ):
        result = run_eval_cmd(skill_names=[], run_all=True)

    assert result == 1
