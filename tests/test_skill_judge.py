"""Tests for fieldkit.skill.judge — LLM-based skill documentation coverage judge.

Covers: stub path, live path (parse success, retry success, retry failure),
schema validation, prompt sanitization, and gold-leakage prevention.

All tests are @pytest.mark.unit — no external deps, no filesystem isolation needed.
"""

import importlib.resources
import json
from unittest.mock import patch

import pytest

from fieldkit.errors import LLMError
from fieldkit.llm import UNTRUSTED_DATA_PREAMBLE
from fieldkit.skill.judge import (
    _JUDGE_PROMPT,
    AssertionVerdict,
    CaseResult,
    JudgeResponseError,
    SkillJudgement,
    _build_judge_prompt,
    judge_skill,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_SINGLE_CASE = [
    {
        "id": 0,
        "prompt": "test prompt",
        "expected_behavior": "should do something",
        "assertions": ["assertion one"],
    }
]

# Two-assertion case used in duplicate-index validation tests.
_TWO_ASSERTION_CASE = [
    {
        "id": 0,
        "prompt": "test prompt",
        "expected_behavior": "should do two things",
        "assertions": ["assertion one", "assertion two"],
    }
]

_VALID_JSON_REPLY = json.dumps(
    {
        "cases": [
            {
                "id": 0,
                "verdicts": [
                    {
                        "assertion": 0,
                        "verdict": "covered",
                        "reason": "The step is explicit",
                    }
                ],
            }
        ]
    }
)


def _load_calibration_fixtures() -> list[dict]:  # type: ignore[type-arg]
    """Load all calibration fixtures via importlib.resources (AP-004 — never __file__-relative)."""
    data_ref = importlib.resources.files("fieldkit._data").joinpath("skill-eval-calibration.json")
    with importlib.resources.as_file(data_ref) as p:
        return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Stub path
# ---------------------------------------------------------------------------


# ── TestStubPath (flattened) ────────────────────────────────────────────────


def test_stub_path_stub_path_returns_stub_judgement(monkeypatch: pytest.MonkeyPatch) -> None:
    """With NO_LLM=1, judge_skill returns a SkillJudgement with stub=True and all verdicts 'stub'."""
    monkeypatch.setenv("NO_LLM", "1")

    result = judge_skill("test", "skill text", _SINGLE_CASE)

    assert isinstance(result, SkillJudgement)
    assert result.stub is True
    assert result.skill_name == "test"
    # All verdicts must be "stub"
    assert len(result.cases) == 1
    case = result.cases[0]
    assert isinstance(case, CaseResult)
    assert len(case.verdicts) == 1
    verdict = case.verdicts[0]
    assert isinstance(verdict, AssertionVerdict)
    assert verdict.verdict == "stub"


def test_stub_path_stub_path_exit_code_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub judgement signals exit 0 via to_dict()['stub'] == True.

    The runner checks SkillJudgement.stub to skip not_covered_count accumulation.
    This test verifies the signal the runner relies on is correctly set.
    """
    monkeypatch.setenv("NO_LLM", "1")

    result = judge_skill("test", "skill text", _SINGLE_CASE)
    d = result.to_dict()

    assert d["stub"] is True
    # Confirm the structure the runner iterates over is present and correct
    assert d["skill_name"] == "test"
    assert isinstance(d["cases"], list)
    assert len(d["cases"]) == 1
    assert d["cases"][0]["verdicts"][0]["verdict"] == "stub"


def test_stub_path_stub_path_via_patch() -> None:
    """Stub path also triggered by patching llm_disabled to return True."""
    with patch("fieldkit.skill.judge.llm_disabled", return_value=True):
        result = judge_skill("patched", "skill text", _SINGLE_CASE)

    assert result.stub is True
    assert all(v.verdict == "stub" for case in result.cases for v in case.verdicts)


# ---------------------------------------------------------------------------
# Live path
# ---------------------------------------------------------------------------


# ── TestLivePath (flattened) ────────────────────────────────────────────────


def test_live_path_live_path_parse_success() -> None:
    """Mock synthesize returns valid JSON; correct AssertionVerdict objects are built."""
    with patch("fieldkit.skill.judge.synthesize", return_value=_VALID_JSON_REPLY) as mock_synth:
        result = judge_skill("test-skill", "skill text", _SINGLE_CASE)

    mock_synth.assert_called_once()
    assert result.stub is False
    assert result.skill_name == "test-skill"
    assert len(result.cases) == 1
    case = result.cases[0]
    assert case.case_id == 0
    assert len(case.verdicts) == 1
    v = case.verdicts[0]
    assert isinstance(v, AssertionVerdict)
    assert v.assertion_idx == 0
    assert v.verdict == "covered"
    assert v.reason == "The step is explicit"


def test_live_path_live_path_retry_success() -> None:
    """First synthesize call returns invalid JSON; second returns valid JSON.

    Asserts the second call's prompt contains the corrective line (D4).
    """
    valid_reply = _VALID_JSON_REPLY
    with patch(
        "fieldkit.skill.judge.synthesize",
        side_effect=["not valid json", valid_reply],
    ) as mock_synth:
        result = judge_skill("test-skill", "skill text", _SINGLE_CASE)

    assert mock_synth.call_count == 2
    # The second call's prompt must contain the corrective line (D4)
    second_call_prompt: str = mock_synth.call_args_list[1][0][0]
    assert "Your previous reply was not valid JSON. Reply with only the JSON object." in second_call_prompt
    # Result should be valid
    assert result.stub is False
    assert result.cases[0].verdicts[0].verdict == "covered"


def test_live_path_live_path_retry_failure() -> None:
    """Both synthesize calls return invalid JSON → LLMError(category='general') raised (D4)."""
    with (
        patch(
            "fieldkit.skill.judge.synthesize",
            side_effect=["not valid json", "also not valid json"],
        ),
        pytest.raises(LLMError, match="unparseable after retry"),
    ):
        judge_skill("test-skill", "skill text", _SINGLE_CASE)


def test_live_path_live_path_model_override_passed_through() -> None:
    """model= kwarg is forwarded to synthesize() calls."""
    with patch("fieldkit.skill.judge.synthesize", return_value=_VALID_JSON_REPLY) as mock_synth:
        result = judge_skill("test-skill", "skill text", _SINGLE_CASE, model="vertex_ai/claude-haiku-4-5")

    # model kwarg must be forwarded
    _, kwargs = mock_synth.call_args
    assert kwargs.get("model") == "vertex_ai/claude-haiku-4-5"
    assert result.model == "vertex_ai/claude-haiku-4-5"


def test_live_path_live_path_code_fence_stripped() -> None:
    """synthesize returns JSON wrapped in ```json fences; fences are stripped before parse."""
    fenced = f"```json\n{_VALID_JSON_REPLY}\n```"
    with patch("fieldkit.skill.judge.synthesize", return_value=fenced):
        result = judge_skill("test-skill", "skill text", _SINGLE_CASE)

    assert result.stub is False
    assert result.cases[0].verdicts[0].verdict == "covered"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


# ── TestSchemaValidation (flattened) ────────────────────────────────────────


def test_schema_validation_schema_validation_unknown_verdict() -> None:
    """JSON with an unknown verdict value raises LLMError(category='general')."""
    bad_reply = json.dumps(
        {
            "cases": [
                {
                    "id": 0,
                    "verdicts": [
                        {
                            "assertion": 0,
                            "verdict": "ambiguous",  # not in allowed set
                            "reason": "some reason",
                        }
                    ],
                }
            ]
        }
    )
    with (
        patch("fieldkit.skill.judge.synthesize", return_value=bad_reply),
        pytest.raises(LLMError, match="unknown verdict") as exc_info,
    ):
        judge_skill("test-skill", "skill text", _SINGLE_CASE)

    assert exc_info.value.category == "general"


def test_schema_validation_schema_validation_empty_cases() -> None:
    """JSON with empty cases array raises LLMError(category='general') (false-green guard)."""
    empty_cases_reply = json.dumps({"cases": []})
    with (
        patch("fieldkit.skill.judge.synthesize", return_value=empty_cases_reply),
        pytest.raises(LLMError, match="empty cases array") as exc_info,
    ):
        judge_skill("test-skill", "skill text", _SINGLE_CASE)

    assert exc_info.value.category == "general"


def test_schema_validation_schema_validation_assertion_out_of_bounds() -> None:
    """Assertion index beyond the case's assertion count raises LLMError(category='general')."""
    bad_reply = json.dumps(
        {
            "cases": [
                {
                    "id": 0,
                    "verdicts": [
                        {
                            "assertion": 99,  # out of bounds — only 1 assertion
                            "verdict": "covered",
                            "reason": "some reason",
                        }
                    ],
                }
            ]
        }
    )
    with (
        patch("fieldkit.skill.judge.synthesize", return_value=bad_reply),
        pytest.raises(LLMError, match="out of bounds") as exc_info,
    ):
        judge_skill("test-skill", "skill text", _SINGLE_CASE)

    assert exc_info.value.category == "general"


def test_schema_validation_schema_validation_unknown_case_id() -> None:
    """Hallucinated case_id (not in original cases) raises LLMError — false-green guard.

    A hallucinated case_id with empty verdicts would silently pass all three stated
    checks (non-empty array, no unknown verdicts, no OOB indices) and produce a
    phantom CaseResult contributing nothing to not_covered_count → false-green exit 0.
    """
    bad_reply = json.dumps(
        {
            "cases": [
                {
                    "id": 99,  # hallucinated — original case has id=0
                    "verdicts": [
                        {
                            "assertion": 0,
                            "verdict": "covered",
                            "reason": "seems fine",
                        }
                    ],
                }
            ]
        }
    )
    with (
        patch("fieldkit.skill.judge.synthesize", return_value=bad_reply),
        pytest.raises(JudgeResponseError, match="unknown case_id") as exc_info,
    ):
        judge_skill("test-skill", "skill text", _SINGLE_CASE)

    assert exc_info.value.category == "general"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ([], "non-object response"),
        ({"cases": "bad"}, "invalid cases array"),
        ({"cases": [1]}, "invalid cases array"),
        ({"cases": [{"id": 0, "verdicts": [1]}]}, "invalid verdicts array"),
        (
            {"cases": [{"id": 0, "verdicts": [{"assertion": 0, "verdict": [], "reason": "bad"}]}]},
            "unknown verdict",
        ),
        (
            {"cases": [{"id": False, "verdicts": [{"assertion": 0, "verdict": "covered", "reason": "bad"}]}]},
            "non-integer case_id",
        ),
        (
            {"cases": [{"id": 0, "verdicts": [{"assertion": False, "verdict": "covered", "reason": "bad"}]}]},
            "assertion index",
        ),
    ],
)
def test_schema_validation_container_shapes_raise_judge_response_error(
    response: object,
    message: str,
) -> None:
    with (
        patch("fieldkit.skill.judge.synthesize", return_value=json.dumps(response)),
        pytest.raises(JudgeResponseError, match=message),
    ):
        judge_skill("test-skill", "skill text", _SINGLE_CASE)


def test_schema_validation_schema_validation_empty_reason() -> None:
    """Missing or whitespace-only reason field raises LLMError(category='general')."""
    bad_reply = json.dumps(
        {
            "cases": [
                {
                    "id": 0,
                    "verdicts": [
                        {
                            "assertion": 0,
                            "verdict": "covered",
                            "reason": "",  # empty string — not acceptable
                        }
                    ],
                }
            ]
        }
    )
    with (
        patch("fieldkit.skill.judge.synthesize", return_value=bad_reply),
        pytest.raises(LLMError, match="missing or empty reason") as exc_info,
    ):
        judge_skill("test-skill", "skill text", _SINGLE_CASE)

    assert exc_info.value.category == "general"


def test_schema_validation_schema_validation_empty_verdicts_per_case() -> None:
    """Empty verdicts array within a case raises LLMError — false-green guard.

    Top-level cases array is non-empty (passes the first guard), but empty
    inner verdicts means assertions are never iterated, so not_covered_count
    stays 0 and exit code is falsely 0.
    """
    bad_reply = json.dumps(
        {
            "cases": [
                {
                    "id": 0,
                    "verdicts": [],  # no verdicts — all assertions silently ungraded
                }
            ]
        }
    )
    with (
        patch("fieldkit.skill.judge.synthesize", return_value=bad_reply),
        pytest.raises(LLMError, match="empty verdicts for case") as exc_info,
    ):
        judge_skill("test-skill", "skill text", _SINGLE_CASE)

    assert exc_info.value.category == "general"


def test_schema_validation_schema_validation_duplicate_assertion_index() -> None:
    """Duplicate assertion index within a case raises LLMError — false-green guard.

    First occurrence is graded; subsequent duplicates silently overwrite or are
    ignored, leaving some assertion indices ungraded → not_covered_count is not
    incremented for the missing assertion → false-green exit 0.
    """
    bad_reply = json.dumps(
        {
            "cases": [
                {
                    "id": 0,
                    "verdicts": [
                        {"assertion": 0, "verdict": "covered", "reason": "explicit step"},
                        {
                            "assertion": 0,  # duplicate — assertion 1 is never graded
                            "verdict": "covered",
                            "reason": "second grade for same assertion",
                        },
                    ],
                }
            ]
        }
    )
    with (
        patch("fieldkit.skill.judge.synthesize", return_value=bad_reply),
        pytest.raises(LLMError, match="duplicate assertion index") as exc_info,
    ):
        judge_skill("test-skill", "skill text", _TWO_ASSERTION_CASE)

    assert exc_info.value.category == "general"


# ---------------------------------------------------------------------------
# Prompt sanitization
# ---------------------------------------------------------------------------


# ── TestSanitization (flattened) ────────────────────────────────────────────


def test_sanitization_wrap_user_data_applied() -> None:
    """_build_judge_prompt wraps user content in <user_data> tags."""
    prompt = _build_judge_prompt(
        "skill text",
        [
            {
                "id": 0,
                "prompt": "p",
                "expected_behavior": "eb",
                "assertions": ["a"],
            }
        ],
    )

    # wrap_user_data delimiters must be present
    assert "<user_data" in prompt
    assert "</user_data>" in prompt


def test_sanitization_expected_behavior_excluded_from_prompt() -> None:
    """expected_behavior value is NOT included in the prompt (answer-key bias prevention, D2)."""
    prompt = _build_judge_prompt(
        "skill text",
        [
            {
                "id": 0,
                "prompt": "p",
                "expected_behavior": "UNIQUE_EXPECTED_BEHAVIOR_SENTINEL",
                "assertions": ["a"],
            }
        ],
    )

    assert "UNIQUE_EXPECTED_BEHAVIOR_SENTINEL" not in prompt


def test_sanitization_untrusted_data_preamble_in_judge_prompt_constant() -> None:
    """_JUDGE_PROMPT module constant contains UNTRUSTED_DATA_PREAMBLE (D2-sanitization)."""
    assert UNTRUSTED_DATA_PREAMBLE in _JUDGE_PROMPT


def test_sanitization_skill_text_wrapped_in_user_data() -> None:
    """The skill text itself is wrapped in <user_data> tags in the prompt."""
    unique_skill_text = "UNIQUE_SKILL_TEXT_CONTENT_12345"
    prompt = _build_judge_prompt(
        unique_skill_text,
        [{"id": 0, "prompt": "p", "expected_behavior": "", "assertions": ["a"]}],
    )

    # The skill text must appear inside a user_data block
    assert unique_skill_text in prompt
    # Verify it's wrapped — the label tag must precede the content
    assert "<user_data label='skill_text'>" in prompt


def test_sanitization_assertion_text_wrapped_in_user_data() -> None:
    """Each assertion string is wrapped in <user_data> tags."""
    prompt = _build_judge_prompt(
        "skill text",
        [
            {
                "id": 0,
                "prompt": "p",
                "expected_behavior": "",
                "assertions": ["my assertion text"],
            }
        ],
    )

    assert "<user_data label='case_0_assertion_0'>" in prompt
    assert "my assertion text" in prompt


def test_sanitization_prompt_text_wrapped_in_user_data() -> None:
    """The case prompt text is wrapped in <user_data> tags."""
    prompt = _build_judge_prompt(
        "skill text",
        [
            {
                "id": 0,
                "prompt": "UNIQUE_PROMPT_TEXT_67890",
                "expected_behavior": "",
                "assertions": ["a"],
            }
        ],
    )

    assert "<user_data label='case_0_prompt'>" in prompt
    assert "UNIQUE_PROMPT_TEXT_67890" in prompt


# ---------------------------------------------------------------------------
# Gold leakage prevention
# ---------------------------------------------------------------------------


# ── TestGoldLeakage (flattened) ─────────────────────────────────────────────


@pytest.fixture(params=_load_calibration_fixtures(), ids=lambda f: f["id"])
def fixture(request: pytest.FixtureRequest) -> dict:  # type: ignore[type-arg]
    """Parametrize over all 6 calibration fixtures."""
    return request.param  # type: ignore[return-value]


def test_gold_leakage_gold_leakage(fixture: dict) -> None:  # type: ignore[type-arg]
    """gold_reason and 'expected_behavior' key string must not appear in the built prompt."""
    cases = [
        {
            "id": 0,
            "prompt": fixture["prompt"],
            "expected_behavior": "",  # intentionally empty — answer key excluded
            "assertions": [fixture["assertion"]],
        }
    ]

    prompt = _build_judge_prompt(fixture["skill_excerpt"], cases)

    # The fixture-specific gold reason must not leak into the prompt
    assert fixture["gold_reason"] not in prompt, (
        f"gold_reason leaked into prompt for fixture {fixture['id']!r}: {fixture['gold_reason']!r}"
    )

    # The string "expected_behavior" must not appear as a key in the prompt
    # (guards against accidentally serializing the full case dict)
    assert "expected_behavior" not in prompt, (
        f"'expected_behavior' key string leaked into prompt for fixture {fixture['id']!r}"
    )
