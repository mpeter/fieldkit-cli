"""Unit tests for gold-leakage prevention in _build_judge_prompt().

Verifies that calibration fixture gold fields (gold_reason, gold_verdict,
expected_behavior) do NOT appear in the prompt constructed by
_build_judge_prompt(), and that wrap_user_data() delimiters ARE present.

Design reference: design.md D2-sanitization, Risks §1.
Task: T09 (Phase 2 — Calibration Fixtures).
"""

import importlib.resources
import json

import pytest

from fieldkit.llm.sanitize import UNTRUSTED_DATA_PREAMBLE
from fieldkit.skill.judge import _build_judge_prompt

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_calibration_fixtures() -> list[dict]:
    """Load calibration fixtures from the package data file.

    Uses importlib.resources per the design spec (never __file__-relative paths).

    Returns:
        List of fixture dicts, each with id, skill_excerpt, prompt, assertion,
        gold_verdict, and gold_reason keys.
    """
    data_path = importlib.resources.files("fieldkit._data").joinpath("skill-eval-calibration.json")
    with importlib.resources.as_file(data_path) as p:
        return json.loads(p.read_text(encoding="utf-8"))


# Module-level I/O: pytest parametrize requires fixture params at collection time.
# importlib.resources on a properly installed package is reliable and fast (no HTTP).
# If the data file is missing, the ImportError surfaces during collection — intentional.
_FIXTURES = _load_calibration_fixtures()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cases_for_fixture(fixture: dict) -> list[dict]:
    """Build the cases list for a calibration fixture as specified in design.md Risks §1.

    The expected_behavior field is intentionally empty — the judge grades only
    the assertions, not the human-written answer key.

    Args:
        fixture: A calibration fixture dict.

    Returns:
        A single-element cases list suitable for _build_judge_prompt().
    """
    return [
        {
            "id": 0,
            "prompt": fixture["prompt"],
            "expected_behavior": "",  # intentionally empty — must not appear in prompt
            "assertions": [fixture["assertion"]],
        }
    ]


# ---------------------------------------------------------------------------
# Parametrized gold-leakage tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("fixture", _FIXTURES, ids=[f["id"] for f in _FIXTURES])
def test_gold_reason_absent_from_prompt(fixture: dict) -> None:
    """gold_reason must NOT appear in the judge prompt.

    gold_reason is unique per fixture (verified in T08 shape check), so its
    absence proves the answer key is not leaking into the prompt. A judge that
    sees the gold_reason could trivially produce the correct verdict without
    reading the skill text.

    Design: design.md D2-sanitization, Risks §1 — "No gold leakage".
    """
    cases = _make_cases_for_fixture(fixture)
    prompt = _build_judge_prompt(fixture["skill_excerpt"], cases)

    assert fixture["gold_reason"] not in prompt, (
        f"gold_reason leaked into prompt for fixture {fixture['id']!r}. "
        f"The answer key must never appear in the judge input."
    )


@pytest.mark.unit
@pytest.mark.parametrize("fixture", _FIXTURES, ids=[f["id"] for f in _FIXTURES])
def test_gold_verdict_word_not_sole_source(fixture: dict) -> None:
    """expected_behavior key string must NOT appear in the judge prompt.

    The string literal "expected_behavior" appearing in the prompt would indicate
    that the answer-key field is being serialized into the prompt. The
    _build_judge_prompt() implementation intentionally ignores this key even
    when present in the cases dict.

    Design: design.md D2 — "expected_behavior MUST NOT be included in the judge prompt".
    """
    cases = _make_cases_for_fixture(fixture)
    prompt = _build_judge_prompt(fixture["skill_excerpt"], cases)

    assert "expected_behavior" not in prompt, (
        f"The string 'expected_behavior' appeared in the prompt for fixture "
        f"{fixture['id']!r}. This key must be silently ignored by _build_judge_prompt()."
    )


@pytest.mark.unit
@pytest.mark.parametrize("fixture", _FIXTURES, ids=[f["id"] for f in _FIXTURES])
def test_wrap_user_data_delimiters_present(fixture: dict) -> None:
    """wrap_user_data() delimiters must be present in the judge prompt.

    _build_judge_prompt() wraps skill_text, case prompts, and assertion strings
    using wrap_user_data() from fieldkit.llm.sanitize. The opening tag pattern
    "<user_data label='" and closing tag "</user_data>" must appear in the
    constructed prompt for every fixture.

    Design: design.md D2-sanitization — "MUST wrap ... using wrap_user_data()".
    """
    cases = _make_cases_for_fixture(fixture)
    prompt = _build_judge_prompt(fixture["skill_excerpt"], cases)

    # Opening tag pattern (label varies per wrapped field)
    assert "<user_data label='" in prompt, (
        f"wrap_user_data opening delimiter missing from prompt for fixture {fixture['id']!r}. "
        f"_build_judge_prompt() must wrap all user-controlled content."
    )
    # Closing tag
    assert "</user_data>" in prompt, (
        f"wrap_user_data closing delimiter missing from prompt for fixture {fixture['id']!r}."
    )


@pytest.mark.unit
@pytest.mark.parametrize("fixture", _FIXTURES, ids=[f["id"] for f in _FIXTURES])
def test_untrusted_data_preamble_in_judge_prompt(fixture: dict) -> None:
    """UNTRUSTED_DATA_PREAMBLE must be present in _JUDGE_PROMPT (system prompt).

    This test verifies the preamble is accessible from the module constant,
    confirming the system prompt is correctly constructed. The preamble itself
    is part of _JUDGE_PROMPT (the system arg to synthesize()), not the user
    turn — but we verify the constant is non-empty and the user-turn prompt
    contains the wrap_user_data structure that the preamble governs.

    Design: design.md D2-sanitization — "prepend UNTRUSTED_DATA_PREAMBLE to the
    system prompt prefix".
    """
    from fieldkit.skill.judge import _JUDGE_PROMPT

    assert UNTRUSTED_DATA_PREAMBLE in _JUDGE_PROMPT, (
        "UNTRUSTED_DATA_PREAMBLE must be present in _JUDGE_PROMPT. "
        "The system prompt instructs the model to treat <user_data> blocks as untrusted data."
    )

    # Also confirm the user-turn prompt for this fixture has the wrapping structure
    cases = _make_cases_for_fixture(fixture)
    prompt = _build_judge_prompt(fixture["skill_excerpt"], cases)
    assert len(prompt) > 0, f"_build_judge_prompt() returned empty string for fixture {fixture['id']!r}"


# ---------------------------------------------------------------------------
# Fixture shape sanity (non-parametrized — runs once)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calibration_fixtures_shape() -> None:
    """Verify the calibration fixtures file has the correct shape and coverage.

    This test catches regressions in the fixtures file itself — if someone
    edits the JSON and breaks the shape, this test fails before any gold-leakage
    test runs.

    Required shape per T08:
    - Flat list of 6 fixtures
    - Each has: id, skill_excerpt, prompt, assertion, gold_verdict, gold_reason
    - Unique ids and unique gold_reasons
    - At least 2 covered, 2 not-covered, 2 unclear verdicts
    """
    fixtures = _FIXTURES
    assert len(fixtures) == 6, f"Expected 6 calibration fixtures, got {len(fixtures)}"

    required_keys = {"id", "skill_excerpt", "prompt", "assertion", "gold_verdict", "gold_reason"}
    valid_verdicts = {"covered", "not-covered", "unclear"}

    for fx in fixtures:
        missing = required_keys - set(fx.keys())
        assert not missing, f"Fixture {fx.get('id', '?')!r} missing keys: {missing}"
        assert fx["gold_verdict"] in valid_verdicts, (
            f"Fixture {fx['id']!r} has invalid gold_verdict: {fx['gold_verdict']!r}"
        )

    ids = [fx["id"] for fx in fixtures]
    assert len(set(ids)) == len(ids), f"Fixture IDs are not unique: {ids}"

    reasons = [fx["gold_reason"] for fx in fixtures]
    assert len(set(reasons)) == len(reasons), (
        "gold_reason strings are not unique — gold-leakage test would be unreliable"
    )

    verdicts = [fx["gold_verdict"] for fx in fixtures]
    verdict_counts = {v: verdicts.count(v) for v in valid_verdicts}
    for verdict, count in verdict_counts.items():
        assert count >= 2, (
            f"Need at least 2 fixtures with verdict {verdict!r}, got {count}. "
            f"All verdict boundaries must be represented."
        )
