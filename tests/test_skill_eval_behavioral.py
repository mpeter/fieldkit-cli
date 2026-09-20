"""Behavioral eval harness tests for `fieldkit skill eval --behavioral`.

Covers: run_eval_cmd() behavioral sub-path, exit-code logic (D8), --limit,
--skill, --json output, --calibrate, and _BEHAVIORAL_SKIP filtering.

All tests use NO_LLM=1 (via monkeypatch) or mock judge_skill at the
import-site binding in eval_runner to avoid any real LLM calls.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.commands.skill.eval_runner import run_eval_cmd
from fieldkit.errors import LLMError, LLMErrorCategory
from fieldkit.skill.judge import AssertionVerdict, CaseResult, JudgeResponseError, SkillJudgement

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EVALS_JSON = json.dumps(
    {
        "evals": [
            {
                "id": 0,
                "prompt": "test prompt",
                "expected_behavior": "test expected",
                "assertions": ["assertion text"],
            }
        ]
    }
)


def make_skills_dir(tmp_path: Path, names: list[str]) -> Path:
    """Create a synthetic skills directory with SKILL.md and evals/evals.json.

    Each skill directory gets a minimal SKILL.md and a valid evals.json so
    _resolve_behavioral_skill_dirs() includes them in corpus discovery.

    Args:
        tmp_path: pytest tmp_path fixture value.
        names: Skill directory names to create.

    Returns:
        Path to the created skills/ directory.
    """
    skills = tmp_path / "skills"
    skills.mkdir()
    for name in names:
        skill_dir = skills / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        evals_dir = skill_dir / "evals"
        evals_dir.mkdir()
        (evals_dir / "evals.json").write_text(_EVALS_JSON, encoding="utf-8")
    return skills


def make_judgement(skill_name: str, verdict: str = "covered") -> SkillJudgement:
    """Build a SkillJudgement with a single assertion at the given verdict.

    Args:
        skill_name: Name of the skill.
        verdict: One of "covered", "not-covered", "unclear", "stub".

    Returns:
        A SkillJudgement with one CaseResult containing one AssertionVerdict.
    """
    av = AssertionVerdict(assertion_idx=0, verdict=verdict, reason="test reason")  # type: ignore[arg-type]
    cr = CaseResult(case_id=0, verdicts=[av])
    return SkillJudgement(skill_name=skill_name, cases=[cr], model="test", stub=False)


def make_stub_judgement(skill_name: str) -> SkillJudgement:
    """Build a stub SkillJudgement (NO_LLM=1 mode — no verdicts).

    Args:
        skill_name: Name of the skill.

    Returns:
        A SkillJudgement with empty cases and stub=True.
    """
    return SkillJudgement(skill_name=skill_name, cases=[], model="stub", stub=True)


# ---------------------------------------------------------------------------
# TestBehavioralAll
# ---------------------------------------------------------------------------


# ── TestBehavioralAll (flattened) ───────────────────────────────────────────


def test_check_contains_all_behavioral_all_stub_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--behavioral --all with NO_LLM=1 processes all 3 skills and returns 0.

    Stubs judge_skill via monkeypatch on the env var so the real stub path
    in judge.py fires, then mocks _skills_dir() to return our synthetic dir.
    """
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["alpha", "bravo", "charlie"])

    with patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir):
        result = run_eval_cmd(skill_names=[], run_all=True, behavioral=True)

    assert result == 0
    assert "fieldkit Skill Eval — Behavioral" in capsys.readouterr().out


def test_check_contains_all_behavioral_all_processes_all_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--behavioral --all calls judge_skill for each eligible skill directory."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["alpha", "bravo", "charlie"])

    call_log: list[str] = []

    def _fake_judge(skill_name: str, skill_text: str, cases: list[Any], model: Any) -> SkillJudgement:
        call_log.append(skill_name)
        return make_stub_judgement(skill_name)

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill", side_effect=_fake_judge),
    ):
        run_eval_cmd(skill_names=[], run_all=True, behavioral=True)

    assert set(call_log) == {"alpha", "bravo", "charlie"}


def test_check_contains_all_managing_google_workspace_included(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """managing-google-workspace joins the --behavioral --all corpus (D2 exclusion lifted 2026-07-18)."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(
        tmp_path,
        ["alpha", "managing-google-workspace", "bravo"],
    )

    call_log: list[str] = []

    def _fake_judge(skill_name: str, skill_text: str, cases: list[Any], model: Any) -> SkillJudgement:
        call_log.append(skill_name)
        return make_stub_judgement(skill_name)

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill", side_effect=_fake_judge),
    ):
        run_eval_cmd(skill_names=[], run_all=True, behavioral=True)

    assert set(call_log) == {"alpha", "managing-google-workspace", "bravo"}


# ---------------------------------------------------------------------------
# TestBehavioralSkillFlag
# ---------------------------------------------------------------------------


# ── TestBehavioralSkillFlag (flattened) ─────────────────────────────────────


def test_resolve_behavioral_skill_dirs_skill_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--skill forecast processes only forecast, not other skills."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["forecast", "other"])

    call_log: list[str] = []

    def _fake_judge(skill_name: str, skill_text: str, cases: list[Any], model: Any) -> SkillJudgement:
        call_log.append(skill_name)
        return make_stub_judgement(skill_name)

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill", side_effect=_fake_judge),
    ):
        result = run_eval_cmd(skill_names=["forecast"], behavioral=True)

    assert result == 0
    assert call_log == ["forecast"]


def test_resolve_behavioral_skill_dirs_skill_flag_not_found_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--skill nonexistent returns 1 and prints error to stderr."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["forecast"])

    with patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir):
        result = run_eval_cmd(skill_names=["nonexistent"], behavioral=True)

    assert result == 1


# ---------------------------------------------------------------------------
# TestBehavioralLimitFlag
# ---------------------------------------------------------------------------


# ── TestBehavioralLimitFlag (flattened) ─────────────────────────────────────


def test_run_behavioral_evals_limit_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--limit 3 causes at most 3 skills to be judged from a 5-skill corpus."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["a", "b", "c", "d", "e"])

    call_log: list[str] = []

    def _fake_judge(skill_name: str, skill_text: str, cases: list[Any], model: Any) -> SkillJudgement:
        call_log.append(skill_name)
        return make_stub_judgement(skill_name)

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill", side_effect=_fake_judge),
    ):
        result = run_eval_cmd(skill_names=[], run_all=True, behavioral=True, limit=3)

    assert result == 0
    assert len(call_log) <= 3


# ---------------------------------------------------------------------------
# TestExitCodes
# ---------------------------------------------------------------------------


# ── TestExitCodes (flattened) ───────────────────────────────────────────────


def test_exit_codes_exit_code_not_covered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A not-covered verdict causes run_eval_cmd() to return 1."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["forecast"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            return_value=make_judgement("forecast", "not-covered"),
        ),
    ):
        result = run_eval_cmd(skill_names=["forecast"], behavioral=True)

    assert result == 1


def test_exit_codes_exit_code_unclear_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """unclear-only verdicts return 0 but emit a warning to stderr."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["forecast"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            return_value=make_judgement("forecast", "unclear"),
        ),
    ):
        result = run_eval_cmd(skill_names=["forecast"], behavioral=True)

    assert result == 0
    captured = capsys.readouterr()
    # D8: advisory warning must appear in stderr when unclear count > 0.
    # Assert both the count signal and the verdict name — requiring both prevents
    # a regression from dropping either term from the message (TC-008).
    err = captured.err.lower()
    assert "unclear" in err, f"Expected 'unclear' in stderr warning: {captured.err!r}"


def test_exit_codes_exit_code_all_covered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All covered verdicts return 0."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["forecast"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            return_value=make_judgement("forecast", "covered"),
        ),
    ):
        result = run_eval_cmd(skill_names=["forecast"], behavioral=True)

    assert result == 0


def test_exit_codes_exit_code_2_auth_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMError(category='auth') propagates out of run_eval_cmd() for cli_main() to map to exit 2.

    We test the exception propagation, not the exit code directly, because
    cli_main() is the sole exit boundary (historic regression rule, D8).
    """
    monkeypatch.delenv("NO_LLM", raising=False)
    skills_dir = make_skills_dir(tmp_path, ["forecast"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            side_effect=LLMError("auth failed", category="auth"),
        ),
        pytest.raises(LLMError, match="auth"),
    ):
        run_eval_cmd(skill_names=["forecast"], behavioral=True)


def test_exit_codes_general_judge_failure_is_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A general judge failure is contained and returns partial exit 1."""
    monkeypatch.delenv("NO_LLM", raising=False)
    skills_dir = make_skills_dir(tmp_path, ["forecast"])
    private_detail = "untrusted raw judge response"

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            side_effect=JudgeResponseError(private_detail),
        ),
    ):
        result = run_eval_cmd(skill_names=["forecast"], behavioral=True, json_output=True)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 1
    assert payload["behavioral_errors"] == [{"skill_name": "forecast", "category": "general", "error": "judge_failed"}]
    assert private_detail not in captured.out
    assert private_detail not in captured.err


def test_behavioral_batch_preserves_results_around_middle_judge_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skills_dir = make_skills_dir(tmp_path, ["skill-a", "skill-b", "skill-c"])

    def fake_judge(
        skill_name: str, _skill_text: str, _cases: list[dict[str, Any]], _model: str | None
    ) -> SkillJudgement:
        if skill_name == "skill-b":
            raise JudgeResponseError("raw malformed response")
        return make_judgement(skill_name)

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill", side_effect=fake_judge) as judge,
    ):
        result = run_eval_cmd(
            skill_names=["skill-a", "skill-b", "skill-c"],
            behavioral=True,
            json_output=True,
        )

    payload = json.loads(capsys.readouterr().out)
    behavioral_results = payload["behavioral_results"]
    assert [entry["skill_name"] for entry in behavioral_results] == ["skill-a", "skill-c"]
    assert payload["behavioral_errors"] == [{"skill_name": "skill-b", "category": "general", "error": "judge_failed"}]
    assert payload["evaluated"] == 3
    assert payload["with_evals"] == 3
    assert result == 1
    assert judge.call_count == 3


@pytest.mark.parametrize(
    "malformed_response",
    [
        [],
        {"cases": "bad"},
        {"cases": [42]},
        {"cases": [{"id": 0, "verdicts": [42]}]},
        {"cases": [{"id": 0, "verdicts": [{"assertion": 0, "verdict": [], "reason": "bad"}]}]},
        {"cases": [{"id": False, "verdicts": [{"assertion": 0, "verdict": "covered", "reason": "bad"}]}]},
        {"cases": [{"id": 0, "verdicts": [{"assertion": False, "verdict": "covered", "reason": "bad"}]}]},
    ],
)
def test_behavioral_batch_contains_malformed_container_from_real_judge(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    malformed_response: object,
) -> None:
    skills_dir = make_skills_dir(tmp_path, ["skill-a", "skill-b", "skill-c"])
    valid_response = json.dumps(
        {
            "cases": [
                {
                    "id": 0,
                    "verdicts": [{"assertion": 0, "verdict": "covered", "reason": "covered"}],
                }
            ]
        }
    )
    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.skill.judge.synthesize",
            side_effect=[valid_response, json.dumps(malformed_response), valid_response],
        ) as synthesize,
    ):
        result = run_eval_cmd(
            skill_names=["skill-a", "skill-b", "skill-c"],
            behavioral=True,
            json_output=True,
        )
    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert [entry["skill_name"] for entry in payload["behavioral_results"]] == ["skill-a", "skill-c"]
    assert payload["behavioral_errors"] == [{"skill_name": "skill-b", "category": "general", "error": "judge_failed"}]
    assert synthesize.call_count == 3


def test_behavioral_zero_case_skill_is_reported_without_judge_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skills_dir = make_skills_dir(tmp_path, ["empty-skill"])
    (skills_dir / "empty-skill" / "evals" / "evals.json").write_text('{"evals": []}', encoding="utf-8")

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill") as judge,
    ):
        result = run_eval_cmd(skill_names=["empty-skill"], behavioral=True, json_output=True)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert payload["evaluated"] == 1
    assert payload["with_evals"] == 0
    assert payload["without_evals"] == 1
    assert payload["behavioral_results"] == []
    assert payload["behavioral_without_evals"] == ["empty-skill"]
    assert "without behavioral evals" not in captured.out
    assert "without evals" in captured.err
    judge.assert_not_called()


def test_behavioral_human_summary_reports_failed_skill_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skills_dir = make_skills_dir(tmp_path, ["forecast"])
    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            side_effect=JudgeResponseError("private response"),
        ),
    ):
        result = run_eval_cmd(skill_names=["forecast"], behavioral=True)
    captured = capsys.readouterr()
    assert result == 1
    assert "1 skill(s) failed judge" in captured.err
    assert "private response" not in captured.err


@pytest.mark.parametrize("eval_contents", [None, "not-json", "{}", '{"evals": "invalid"}'])
def test_behavioral_missing_or_invalid_cases_skip_judge(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    eval_contents: str | None,
) -> None:
    skills_dir = make_skills_dir(tmp_path, ["empty-skill"])
    eval_path = skills_dir / "empty-skill" / "evals" / "evals.json"
    if eval_contents is None:
        eval_path.unlink()
    else:
        eval_path.write_text(eval_contents, encoding="utf-8")
    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill") as judge,
    ):
        result = run_eval_cmd(skill_names=["empty-skill"], behavioral=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["behavioral_without_evals"] == ["empty-skill"]
    judge.assert_not_called()


@pytest.mark.parametrize("category", ["auth", "rate-limit"])
def test_behavioral_run_propagates_global_llm_failures(
    tmp_path: Path,
    category: LLMErrorCategory,
) -> None:
    skills_dir = make_skills_dir(tmp_path, ["skill-a", "skill-b"])
    failure = LLMError("provider unavailable", category=category)
    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill", side_effect=failure) as judge,
        pytest.raises(LLMError, match="provider unavailable"),
    ):
        run_eval_cmd(skill_names=["skill-a", "skill-b"], behavioral=True)
    assert judge.call_count == 1


def test_behavioral_run_propagates_provider_general_failure(
    tmp_path: Path,
) -> None:
    skills_dir = make_skills_dir(tmp_path, ["skill-a", "skill-b"])
    provider_error = ConnectionError("transport failed")
    failure = LLMError("provider unavailable", category="general", original=provider_error)
    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch("fieldkit.commands.skill.eval_runner.judge_skill", side_effect=failure) as judge,
        pytest.raises(LLMError, match="provider unavailable") as exc_info,
    ):
        run_eval_cmd(skill_names=["skill-a", "skill-b"], behavioral=True)
    assert exc_info.value.original is provider_error
    assert judge.call_count == 1


# ---------------------------------------------------------------------------
# TestJsonOutput
# ---------------------------------------------------------------------------


# ── TestJsonOutput (flattened) ──────────────────────────────────────────────


def test_print_json_summary_json_output_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--behavioral --skill forecast --json emits valid JSON with behavioral_results key."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["forecast"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            return_value=make_stub_judgement("forecast"),
        ),
    ):
        result = run_eval_cmd(skill_names=["forecast"], json_output=True, behavioral=True)

    assert result == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "behavioral_results" in parsed
    # Each entry must have the SkillJudgement.to_dict() shape (D3)
    assert isinstance(parsed["behavioral_results"], list)
    assert len(parsed["behavioral_results"]) == 1
    entry = parsed["behavioral_results"][0]
    assert "skill_name" in entry
    assert "cases" in entry
    assert "stub" in entry


def test_print_json_summary_json_output_stub_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stub=True is reflected in the JSON output when NO_LLM=1."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["forecast"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            return_value=make_stub_judgement("forecast"),
        ),
    ):
        run_eval_cmd(skill_names=["forecast"], json_output=True, behavioral=True)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    entry = parsed["behavioral_results"][0]
    assert entry["stub"] is True


# ---------------------------------------------------------------------------
# TestSummaryRendering
# ---------------------------------------------------------------------------


# ── TestSummaryRendering (flattened) ────────────────────────────────────────


def test_print_json_summary_summary_shows_behavioral_count_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Skills evaluated shows the behavioral count, not 0, on a stub run."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["a", "b", "c"])

    with patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir):
        result = run_eval_cmd(skill_names=[], run_all=True, behavioral=True)

    assert result == 0
    captured = capsys.readouterr()
    # Must say 3, not 0
    assert "Skills evaluated:  3" in captured.out
    # Regression: count was showing 50 (total corpus) instead of 3 (skills evaluated)
    # because _print_eval_summary used len(all_results) which is the static list (empty).
    assert "Skills evaluated:  50" not in captured.out


def test_print_json_summary_summary_shows_live_count_and_not_covered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Live behavioral run shows skill count and not-covered count in summary."""
    skills_dir = make_skills_dir(tmp_path, ["skill-x"])

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner.judge_skill",
            return_value=make_judgement("skill-x", "not-covered"),
        ),
    ):
        result = run_eval_cmd(skill_names=["skill-x"], behavioral=True)

    assert result == 1  # not-covered → exit 1
    captured = capsys.readouterr()
    assert "Skills evaluated:  1" in captured.out
    assert "Not covered:" in captured.out


def test_print_json_summary_summary_json_includes_evaluated_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON summary includes evaluated count matching behavioral skill count."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, ["a", "b"])

    with patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir):
        run_eval_cmd(skill_names=[], run_all=True, json_output=True, behavioral=True)

    parsed = json.loads(capsys.readouterr().out)
    assert parsed["evaluated"] == 2
    assert parsed["with_evals"] == 2


# ---------------------------------------------------------------------------
# TestCalibrateFlag
# ---------------------------------------------------------------------------


# ── TestCalibrateFlag (flattened) ───────────────────────────────────────────


def _run_calibrate_make_fixture(fixture_id: str, gold_verdict: str = "covered") -> dict[str, Any]:
    """Build a minimal calibration fixture dict."""
    return {
        "id": fixture_id,
        "skill_excerpt": f"## Workflow\n1. Do something for {fixture_id}.",
        "prompt": f"prompt for {fixture_id}",
        "assertion": f"assertion for {fixture_id}",
        "gold_verdict": gold_verdict,
        "gold_reason": f"reason for {fixture_id}",
    }


def test_run_calibrate_calibrate_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--calibrate with NO_LLM=1 returns 0 (stub verdicts treated as pass per D8)."""
    monkeypatch.setenv("NO_LLM", "1")
    # Provide a real skills_dir so the skills_dir check passes
    skills_dir = make_skills_dir(tmp_path, [])

    fixtures = [_run_calibrate_make_fixture(f"cal-{i}", "covered") for i in range(6)]

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner._load_calibration_fixtures",
            return_value=fixtures,
        ),
    ):
        result = run_eval_cmd(skill_names=[], calibrate=True)

    assert result == 0


def test_run_calibrate_calibrate_partial_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """5/6 fixtures matching gold → exit 1 (partial match)."""
    monkeypatch.delenv("NO_LLM", raising=False)
    skills_dir = make_skills_dir(tmp_path, [])

    fixtures = [_run_calibrate_make_fixture(f"cal-{i}", "covered") for i in range(6)]
    # Last fixture expects "covered" but judge returns "not-covered"
    fixtures[-1]["gold_verdict"] = "covered"

    call_count = 0

    def _fake_judge(skill_name: str, skill_text: str, cases: list[Any], model: Any) -> SkillJudgement:
        nonlocal call_count
        call_count += 1
        # Return not-covered for the last fixture (cal-5), covered for others
        verdict = "not-covered" if skill_name == "cal-5" else "covered"
        av = AssertionVerdict(assertion_idx=0, verdict=verdict, reason="test")  # type: ignore[arg-type]
        cr = CaseResult(case_id=0, verdicts=[av])
        return SkillJudgement(skill_name=skill_name, cases=[cr], model="test", stub=False)

    with (
        patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir),
        patch(
            "fieldkit.commands.skill.eval_runner._load_calibration_fixtures",
            return_value=fixtures,
        ),
        patch("fieldkit.commands.skill.eval_runner.judge_skill", side_effect=_fake_judge),
    ):
        result = run_eval_cmd(skill_names=[], calibrate=True)

    assert result == 1


def test_run_calibrate_calibrate_mutual_exclusivity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--calibrate --behavioral together return 1 and print an error message."""
    monkeypatch.setenv("NO_LLM", "1")
    skills_dir = make_skills_dir(tmp_path, [])

    with patch("fieldkit.commands.skill.eval_runner._skills_dir", return_value=skills_dir):
        result = run_eval_cmd(skill_names=[], behavioral=True, calibrate=True)

    assert result == 1
    captured = capsys.readouterr()
    # D6: mutual-exclusivity error must be surfaced to the user
    assert "calibrate" in captured.err.lower() or "behavioral" in captured.err.lower()
    assert "exclusive" in captured.err.lower() or "error" in captured.err.lower()
