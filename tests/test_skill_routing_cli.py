"""CLI tests for ``fieldkit skill eval --routing``."""

import json
from unittest.mock import patch

import pytest

from fieldkit.__main__ import main
from fieldkit.commands.skill.routing_eval import run_routing_eval
from fieldkit.errors import LLMError
from fieldkit.skill.routing import RoutingCase, RoutingResult, SkillCandidate

pytestmark = pytest.mark.unit


def test_routing_stub_json_uses_complete_loaded_corpus(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NO_LLM", "1")
    skills = [
        {"name": "alpha", "description": "Alpha work"},
        {"name": "bravo", "description": "Bravo work"},
    ]
    cases = (RoutingCase("alpha-case", "do alpha", "alpha"),)
    with (
        patch("fieldkit.commands.skill.routing_eval._load_all_skills", return_value=skills),
        patch("fieldkit.commands.skill.routing_eval.load_routing_cases", return_value=cases),
    ):
        result = run_routing_eval(json_output=True, model=None)
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["corpus_size"] == 2
    assert payload["results"][0]["stub"] is True


def test_routing_mismatch_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    skills = [{"name": "alpha", "description": "Alpha work"}, {"name": "bravo", "description": "Bravo work"}]
    cases = (RoutingCase("wrong", "do alpha", "alpha"),)
    mismatch = RoutingResult("wrong", "alpha", "bravo", "Closer match.", "test", False, 2)
    with (
        patch("fieldkit.commands.skill.routing_eval._load_all_skills", return_value=skills),
        patch("fieldkit.commands.skill.routing_eval.load_routing_cases", return_value=cases),
        patch("fieldkit.commands.skill.routing_eval.judge_routing_case", return_value=mismatch),
    ):
        result = run_routing_eval(json_output=False, model=None)
    output = capsys.readouterr().out
    assert result == 1
    assert "fieldkit Skill Eval — Routing" in output
    assert "expected=alpha selected=bravo" in output


@pytest.mark.parametrize("conflicting_flag", ["--all", "--behavioral", "--calibrate", "--failed", "--limit=1"])
def test_routing_conflicts_exit_three_through_dispatcher(
    conflicting_flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result = main(["skill", "eval", "--routing", conflicting_flag])
    assert result == 3
    assert "cannot be combined" in capsys.readouterr().err


def test_routing_missing_corpus_exits_three_through_dispatcher(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("fieldkit.commands.skill.routing_eval._load_all_skills", return_value=[]):
        result = main(["skill", "eval", "--routing"])
    assert result == 3
    assert "Routing skill corpus is empty" in capsys.readouterr().err


def test_load_candidates_rejects_untyped_metadata() -> None:
    with (
        patch("fieldkit.commands.skill.routing_eval._load_all_skills", return_value=[{"name": 4, "description": "x"}]),
        pytest.raises(LLMError, match="invalid name or description"),
    ):
        from fieldkit.commands.skill.routing_eval import _load_candidates

        _load_candidates()


def test_routing_passes_every_candidate_to_each_case() -> None:
    skills = [{"name": "alpha", "description": "Alpha work"}, {"name": "bravo", "description": "Bravo work"}]
    cases = (RoutingCase("one", "first", "alpha"), RoutingCase("two", "second", "bravo"))
    passed = RoutingResult("one", "alpha", "alpha", "match", "test", False, 2)

    def judge(case: RoutingCase, candidates: tuple[SkillCandidate, ...], model: str | None) -> RoutingResult:
        assert len(candidates) == 2
        return passed if case.case_id == "one" else RoutingResult("two", "bravo", "bravo", "match", "test", False, 2)

    with (
        patch("fieldkit.commands.skill.routing_eval._load_all_skills", return_value=skills),
        patch("fieldkit.commands.skill.routing_eval.load_routing_cases", return_value=cases),
        patch("fieldkit.commands.skill.routing_eval.judge_routing_case", side_effect=judge) as judge_mock,
    ):
        result = run_routing_eval(json_output=True, model=None)
    assert result == 0
    assert judge_mock.call_count == 2
