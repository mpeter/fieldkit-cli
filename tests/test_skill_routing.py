"""Domain tests for full-corpus skill routing evaluations."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.errors import LLMError
from fieldkit.skill.routing import (
    RoutingCase,
    SkillCandidate,
    _build_routing_prompt,
    judge_routing_case,
    load_routing_cases,
    validate_skill_corpus,
)

pytestmark = pytest.mark.unit

_CANDIDATES = (
    SkillCandidate("alpha", "Use for pipeline summaries."),
    SkillCandidate("bravo", "Use for individual pursuit audits."),
)


def _write_fixtures(tmp_path: Path, cases: list[dict[str, str]], *, version: int = 1) -> None:
    (tmp_path / "skill-routing-evals.json").write_text(
        json.dumps({"version": version, "cases": cases}), encoding="utf-8"
    )


def test_validate_skill_corpus_sorts_and_rejects_duplicates() -> None:
    result = validate_skill_corpus(tuple(reversed(_CANDIDATES)))
    assert result == _CANDIDATES
    with pytest.raises(LLMError, match="duplicate name"):
        validate_skill_corpus((_CANDIDATES[0], _CANDIDATES[0]))


def test_load_routing_cases_preserves_order(tmp_path: Path) -> None:
    fixtures = [
        {"id": "first-case", "utterance": "Summarize my pipeline", "expected_skill": "alpha"},
        {"id": "second-case", "utterance": "Audit this deal", "expected_skill": "bravo"},
    ]
    _write_fixtures(tmp_path, fixtures)
    with patch("fieldkit.skill.routing.importlib.resources.files", return_value=tmp_path):
        result = load_routing_cases(_CANDIDATES)
    assert [case.case_id for case in result] == ["first-case", "second-case"]


@pytest.mark.parametrize("version", [True, 2, "1"])
def test_load_routing_cases_rejects_unsupported_version(tmp_path: Path, version: object) -> None:
    (tmp_path / "skill-routing-evals.json").write_text(json.dumps({"version": version, "cases": []}), encoding="utf-8")
    with (
        patch("fieldkit.skill.routing.importlib.resources.files", return_value=tmp_path),
        pytest.raises(LLMError, match="version-1 schema"),
    ):
        load_routing_cases(_CANDIDATES)


@pytest.mark.parametrize(
    ("cases", "message"),
    [
        ([{"id": "bad id", "utterance": "x", "expected_skill": "alpha"}], "Invalid routing fixture id"),
        ([{"id": "same", "utterance": "x", "expected_skill": "alpha"}] * 2, "Duplicate routing fixture"),
        ([{"id": "missing", "utterance": "x", "expected_skill": "retired"}], "expects unknown skill"),
        ([{"id": "empty", "utterance": "", "expected_skill": "alpha"}], "empty utterance"),
    ],
)
def test_load_routing_cases_rejects_invalid_cases(tmp_path: Path, cases: list[dict[str, str]], message: str) -> None:
    _write_fixtures(tmp_path, cases)
    with (
        patch("fieldkit.skill.routing.importlib.resources.files", return_value=tmp_path),
        pytest.raises(LLMError, match=message),
    ):
        load_routing_cases(_CANDIDATES)


def test_build_prompt_uses_full_corpus_and_does_not_depend_on_answer_key() -> None:
    alpha_case = RoutingCase("same-request", "Route this request", "alpha")
    bravo_case = RoutingCase("same-request", "Route this request", "bravo")
    alpha_prompt = _build_routing_prompt(alpha_case, _CANDIDATES)
    bravo_prompt = _build_routing_prompt(bravo_case, _CANDIDATES)
    assert alpha_prompt == bravo_prompt
    assert all(candidate.name in alpha_prompt and candidate.description in alpha_prompt for candidate in _CANDIDATES)
    assert "<user_data" in alpha_prompt


def test_judge_routing_case_stub_exercises_typed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_LLM", "1")
    result = judge_routing_case(RoutingCase("stub-case", "route me", "alpha"), _CANDIDATES)
    assert result.passed is True
    assert result.stub is True
    assert result.corpus_size == 2


def test_judge_routing_case_parses_live_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_LLM", raising=False)
    with patch(
        "fieldkit.skill.routing.synthesize",
        return_value='{"selected_skill":"bravo","reason":"Best trigger match."}',
    ) as synthesize_mock:
        result = judge_routing_case(RoutingCase("audit", "audit this", "bravo"), _CANDIDATES, "vertex_ai/test-model")
    assert result.selected_skill == "bravo"
    assert result.passed is True
    assert result.model == "vertex_ai/test-model"
    assert synthesize_mock.call_count == 1


def test_judge_routing_case_retries_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_LLM", raising=False)
    with patch(
        "fieldkit.skill.routing.synthesize",
        side_effect=["not-json", '{"selected_skill":"alpha","reason":"Recovered."}'],
    ) as synthesize_mock:
        result = judge_routing_case(RoutingCase("retry", "route this", "alpha"), _CANDIDATES)
    assert result.passed is True
    assert synthesize_mock.call_count == 2
    assert "previous reply was invalid" in synthesize_mock.call_args_list[1].args[0]


def test_judge_routing_case_records_environment_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.setenv("FIELDKIT_LLM_MODEL", "vertex_ai/environment-model")
    with patch(
        "fieldkit.skill.routing.synthesize",
        return_value='{"selected_skill":"alpha","reason":"Best match."}',
    ) as synthesize_mock:
        result = judge_routing_case(RoutingCase("environment", "route this", "alpha"), _CANDIDATES)
    assert result.model == "vertex_ai/environment-model"
    assert synthesize_mock.call_args.kwargs["model"] == "vertex_ai/environment-model"


def test_judge_routing_case_rejects_unknown_selection_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_LLM", raising=False)
    reply = '{"selected_skill":"unknown","reason":"No match."}'
    with (
        patch("fieldkit.skill.routing.synthesize", return_value=reply),
        pytest.raises(LLMError, match="invalid after retry"),
    ):
        judge_routing_case(RoutingCase("unknown", "route this", "alpha"), _CANDIDATES)
