"""LLM-based skill documentation coverage judge.

Grades skill SKILL.md workflow text against per-case assertions using a single
batched LLM call per skill. Verdicts are typed, machine-readable, and safe for
downstream exit-code computation.

Design decisions:
- D1: Grades skill-text coverage, not live agent transcripts (stable, no fixtures needed).
- D2: One batched judge call per skill; expected_behavior excluded to prevent answer-key bias.
- D3: Frozen dataclasses end-to-end; no raw dicts cross the module boundary.
- D4: JSON parse failures retried once with a corrective user message; second failure → LLMError.
- D5: Stub mode (NO_LLM=1) is a first-class code path; all verdicts become "stub".
"""

import dataclasses
import json
from typing import Any, Literal

from fieldkit.config import llm_disabled
from fieldkit.errors import LLMError
from fieldkit.llm import (
    _NO_LLM_STUB,
    LLM_SYNTHESIS_TIMEOUT,
    UNTRUSTED_DATA_PREAMBLE,
    set_skill_context,
    synthesize,
    wrap_user_data,
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

VerdictLiteral = Literal["covered", "not-covered", "unclear", "stub"]

_VALID_LIVE_VERDICTS: frozenset[str] = frozenset({"covered", "not-covered", "unclear"})

# ---------------------------------------------------------------------------
# Dataclasses (D3 — frozen, typed, no raw dicts cross the boundary)
# ---------------------------------------------------------------------------


class JudgeResponseError(LLMError):
    """Judge output remained invalid after the bounded repair attempt."""

    def __init__(self, message: str) -> None:
        super().__init__(message, category="general")


@dataclasses.dataclass(frozen=True)
class AssertionVerdict:
    """Verdict for a single assertion within a case.

    Attributes:
        assertion_idx: 0-based index of the assertion within its case.
        verdict: Coverage verdict — one of "covered", "not-covered", "unclear", "stub".
        reason: One-sentence explanation of the verdict.
    """

    assertion_idx: int
    verdict: VerdictLiteral
    reason: str


@dataclasses.dataclass(frozen=True)
class CaseResult:
    """Aggregated verdicts for a single eval case.

    Attributes:
        case_id: The numeric ID of the case (from evals.json).
        verdicts: Ordered list of per-assertion verdicts.
    """

    case_id: int
    verdicts: list[AssertionVerdict]


@dataclasses.dataclass(frozen=True)
class SkillJudgement:
    """Complete judge output for one skill.

    Attributes:
        skill_name: Name of the skill that was judged.
        cases: Per-case results with per-assertion verdicts.
        model: LiteLLM model string used for this judgement.
        stub: True when NO_LLM=1 was active; all verdicts are "stub".
    """

    skill_name: str
    cases: list[CaseResult]
    model: str
    stub: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize the judgement to a plain dict suitable for JSON output.

        Returns:
            A dict with skill_name, cases (nested), model, and stub fields.
        """
        return {
            "skill_name": self.skill_name,
            "model": self.model,
            "stub": self.stub,
            "cases": [
                {
                    "case_id": case.case_id,
                    "verdicts": [
                        {
                            "assertion_idx": v.assertion_idx,
                            "verdict": v.verdict,
                            "reason": v.reason,
                        }
                        for v in case.verdicts
                    ],
                }
                for case in self.cases
            ],
        }


# ---------------------------------------------------------------------------
# Judge prompt (D2 — module constant; UNTRUSTED_DATA_PREAMBLE prefix required)
# ---------------------------------------------------------------------------

# The system prompt is static, caller-controlled instruction text — safe to pass
# as the `system=` argument to synthesize() per the docstring contract.
# UNTRUSTED_DATA_PREAMBLE is prepended so the model treats <user_data> blocks
# (skill text, prompts, assertions) as data, not instructions (historic regression pattern).
_JUDGE_PROMPT = f"""{UNTRUSTED_DATA_PREAMBLE}

You are grading skill documentation coverage. Judge ONLY from the workflow text provided. Do not assume unstated behavior.

Verdict rules — three-way decision:

"covered": Following the workflow steps NECESSARILY produces the asserted behavior. Entailment is enough — the wording need not match verbatim. If the steps deterministically lead to the result, it is covered.

"unclear": The workflow text engages with the topic of the assertion but does NOT provide a concrete step that guarantees the specific asserted behavior. The domain is present; the specific mechanism is absent or undefined. Use "unclear" when you can see why the topic is relevant but cannot confirm the behavior from the text alone.

"not-covered": The assertion topic is COMPLETELY ABSENT from the workflow text, or a step EXPLICITLY CONTRADICTS it. "not-covered" is for silence or contradiction — not for vagueness.

Calibration examples (internalize these boundary cases):
- Workflow: "Review the deal with MEDDPICC principles in mind and note anything weak." Assertion: "Scores all 8 MEDDPICC elements on the 0-3 scale." → "unclear" (MEDDPICC is engaged; scoring on a specific scale is unspecified — topic present, mechanism absent)
- Workflow: "Render the standard pursuit summary for each result." Assertion: "Output includes each opportunity's Salesforce URL." → "unclear" ("standard pursuit summary" is undefined in the text; whether it includes URLs depends on runtime output — behavior undefined, not absent)
- Workflow: "Render a table sorted by amount." Assertion: "Excludes archived pursuits from the forecast." → "not-covered" (archiving/status filtering is completely absent from the text)
- Workflow: "Write to Salesforce immediately. Show the user what was written." Assertion: "Asks the user to confirm before writing." → "not-covered" (writing happens before showing — direct contradiction)

Decision tree:
1. Is the assertion topic COMPLETELY absent from the text? → "not-covered"
2. Does a step DIRECTLY CONTRADICT the assertion? → "not-covered"
3. Are steps present that NECESSARILY produce the result? → "covered"
4. Is the topic present but the specific behavior unspecified/vague/undefined? → "unclear"
5. When in doubt between "covered" and "unclear": → "unclear"
6. When in doubt between "unclear" and "not-covered" (topic partially engaged): → "unclear"

Grade ONLY the skill text provided. Do not infer behavior from CLI names or flags.

Required output: a single JSON object {{"cases": [{{"id": 0, "verdicts": [{{"assertion": 0, "verdict": "covered", "reason": "<one sentence>"}}]}}]}}
No prose outside the JSON."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    """Strip ```json / ``` code-fence wrappers from LLM output.

    The judge prompt requests bare JSON, but some models wrap it in fences
    anyway. This strips the outermost fence pair if present.

    Args:
        text: Raw LLM reply string.

    Returns:
        The text with leading ```json or ``` and trailing ``` removed.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove opening fence line (may be ```json or just ```)
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        # Remove closing fence
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
    return stripped.strip()


def _build_judge_prompt(skill_text: str, cases: list[dict[str, Any]]) -> str:
    """Build the user-turn prompt for the judge LLM call.

    Wraps all user-controlled content (skill text, case prompts, assertion strings)
    with wrap_user_data() to prevent prompt injection (historic regression / D2-sanitization).

    IMPORTANT: expected_behavior, gold_verdict, and gold_reason MUST NOT appear
    in the constructed prompt — they are the answer key and would bias verdicts.

    Args:
        skill_text: Full SKILL.md body text.
        cases: List of eval case dicts. Each must have "id", "prompt", and
            "assertions" (list[str]). The "expected_behavior" key is intentionally
            ignored here even if present in the dict.

    Returns:
        A formatted prompt string ready to pass to synthesize().
    """
    parts: list[str] = []

    # Wrap the skill text as untrusted user data
    parts.append("## Skill Workflow\n")
    parts.append(wrap_user_data(skill_text, "skill_text"))
    parts.append("")

    parts.append("## Eval Cases\n")
    for case in cases:
        case_id = case["id"]
        prompt_text: str = case["prompt"]
        assertions: list[str] = case["assertions"]

        parts.append(f"### Case {case_id}")
        parts.append("**Prompt:**")
        parts.append(wrap_user_data(prompt_text, f"case_{case_id}_prompt"))
        parts.append("")
        parts.append("**Assertions to grade:**")
        for idx, assertion in enumerate(assertions):
            parts.append(f"{idx}. {wrap_user_data(assertion, f'case_{case_id}_assertion_{idx}')}")
        parts.append("")

    parts.append("Grade each assertion for each case. Return only the JSON object as specified in the system prompt.")

    return "\n".join(parts)


def _validate_judge_response(data: object, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate the parsed judge JSON against the expected schema.

    Raises JudgeResponseError on every schema violation so a multi-skill caller
    can contain the failure at the current skill boundary (D4, historic regression).

    Checks:
    1. cases array is non-empty (empty array → false-green exit 0 risk).
    2. Every case_id in the response exists in the original cases (hallucinated IDs rejected).
    3. Every case has a non-empty verdicts array (empty verdicts → assertions silently ungraded).
    4. Every verdict value is in {"covered", "not-covered", "unclear"}.
    5. Every assertion index is within bounds for its case's assertions list.
    6. No duplicate assertion indices within a case (duplicate → some assertions silently ungraded).
    7. Every reason field is a non-empty string.

    Args:
        data: Parsed JSON dict from the judge reply.
        cases: Original eval cases list (used for bounds checking and case ID validation).

    Raises:
        JudgeResponseError: On any schema violation.

    Returns:
        The response case dictionaries after validating every container level.
    """
    if not isinstance(data, dict):
        raise JudgeResponseError("Judge returned a non-object response")
    raw_cases = data.get("cases", [])
    if not isinstance(raw_cases, list) or not all(isinstance(entry, dict) for entry in raw_cases):
        raise JudgeResponseError("Judge returned an invalid cases array")
    if not raw_cases:
        raise JudgeResponseError("Judge returned empty cases array — cannot compute verdicts")
    case_entries: list[dict[str, Any]] = raw_cases

    # Build a lookup from case id → assertion count for bounds checking
    case_assertion_counts: dict[int, int] = {c["id"]: len(c["assertions"]) for c in cases}

    for case_entry in case_entries:
        case_id = case_entry.get("id")

        if type(case_id) is not int:
            raise JudgeResponseError(f"Judge returned non-integer case_id {case_id!r} — expected int")

        # Reject hallucinated case IDs — unknown ID with empty verdicts would silently produce
        # a phantom CaseResult contributing nothing to the not-covered count (false-green risk).
        if case_id not in case_assertion_counts:
            raise JudgeResponseError(f"Judge returned unknown case_id {case_id!r} — not in original cases")

        assertion_count = case_assertion_counts[case_id]
        verdicts_list = case_entry.get("verdicts", [])

        # Empty verdicts per case is a false-green: the top-level cases array is non-empty
        # (passes check 1), but inner verdicts are never iterated → not_covered_count stays 0.
        if not isinstance(verdicts_list, list) or not all(isinstance(entry, dict) for entry in verdicts_list):
            raise JudgeResponseError(f"Judge returned an invalid verdicts array for case {case_id}")
        if not verdicts_list:
            raise JudgeResponseError(
                f"Judge returned empty verdicts for case {case_id} — {assertion_count} assertion(s) ungraded"
            )
        verdict_entries: list[dict[str, Any]] = verdicts_list

        seen_assertion_indices: set[int] = set()

        for verdict_entry in verdict_entries:
            verdict_val = verdict_entry.get("verdict")
            if not isinstance(verdict_val, str) or verdict_val not in _VALID_LIVE_VERDICTS:
                raise JudgeResponseError(
                    f"Judge returned unknown verdict {verdict_val!r} for case {case_id} — "
                    f"expected one of {sorted(_VALID_LIVE_VERDICTS)}"
                )

            assertion_idx = verdict_entry.get("assertion")
            if type(assertion_idx) is not int or assertion_idx < 0 or assertion_idx >= assertion_count:
                raise JudgeResponseError(
                    f"Judge returned assertion index {assertion_idx!r} out of bounds "
                    f"for case {case_id} (case has {assertion_count} assertions)"
                )

            # Duplicate assertion index: first occurrence is graded, subsequent occurrences silently
            # overwrite or are ignored, leaving some assertion indices ungraded → false-green risk.
            if assertion_idx in seen_assertion_indices:
                raise JudgeResponseError(
                    f"Judge returned duplicate assertion index {assertion_idx!r} for case {case_id}"
                )
            seen_assertion_indices.add(assertion_idx)

            reason = verdict_entry.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise JudgeResponseError(
                    f"Judge returned missing or empty reason for case {case_id} assertion {assertion_idx}"
                )

    return case_entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def judge_skill(
    skill_name: str,
    skill_text: str,
    cases: list[dict[str, Any]],
    model: str | None = None,
) -> SkillJudgement:
    """Grade skill documentation coverage against eval case assertions.

    Sends a single batched LLM call with the full SKILL.md body and all cases.
    On NO_LLM=1, returns a stub judgement immediately without building a prompt
    or making any API call (D5).

    Args:
        skill_name: Name of the skill (used for observability tagging and output).
        skill_text: Full SKILL.md body text to grade.
        cases: List of eval case dicts. Each must have "id" (int), "prompt" (str),
            and "assertions" (list[str]). "expected_behavior" is intentionally
            ignored — it is the answer key and must not influence the judge.
        model: Optional LiteLLM model override. Falls back to the normal resolution
            chain (env vars, config.yaml, hardcoded default).

    Returns:
        A SkillJudgement with per-case, per-assertion verdicts.

    Raises:
        LLMError: category="general" on JSON parse failure after retry, or schema
            validation failure.
        LLMError: category="auth" if Vertex AI authentication fails (propagates
            from synthesize() → exit 2 via cli_main()).
        LLMError: category="rate-limit" on rate-limit exhaustion (propagates
            from synthesize() → exit 1 via cli_main()).
    """
    # D5: Stub path — checked before building the prompt or making any API call.
    if llm_disabled():
        stub_cases = [
            CaseResult(
                case_id=case["id"],
                verdicts=[
                    AssertionVerdict(assertion_idx=idx, verdict="stub", reason="NO_LLM=1 — stub mode active")
                    for idx in range(len(case["assertions"]))
                ],
            )
            for case in cases
        ]
        return SkillJudgement(
            skill_name=skill_name,
            cases=stub_cases,
            model=_NO_LLM_STUB,
            stub=True,
        )

    # D6: Tag llm-calls.db rows for cost queries by account (live path only — no
    # side effects in stub mode per D5).
    set_skill_context(skill=skill_name, account="skill-eval-harness")

    # D2: Build the judge prompt with all user-controlled content wrapped.
    prompt = _build_judge_prompt(skill_text, cases)

    # D2: First attempt — single batched call.
    raw = synthesize(prompt, model=model, timeout=LLM_SYNTHESIS_TIMEOUT, system=_JUDGE_PROMPT)

    try:
        data = json.loads(_strip_code_fences(raw))
    except json.JSONDecodeError:
        # D4: Retry once with a corrective suffix appended to the same user-turn string.
        # NOTE: synthesize() is single-turn; this is NOT a multi-turn conversation.
        # The corrective line is appended to the same prompt string (not a new turn),
        # so all user-controlled <user_data> blocks from the first attempt remain present.
        # The wrap_user_data() sanitization is still applied. Do not weaken this assumption
        # by removing the sanitization from the original prompt before retrying.
        corrective_prompt = prompt + "\n\nYour previous reply was not valid JSON. Reply with only the JSON object."
        raw2 = synthesize(corrective_prompt, model=model, timeout=LLM_SYNTHESIS_TIMEOUT, system=_JUDGE_PROMPT)
        try:
            data = json.loads(_strip_code_fences(raw2))
        except json.JSONDecodeError as exc:
            raise JudgeResponseError("Judge output unparseable after retry — model did not return valid JSON") from exc

    # D4: Schema validation before dataclass construction.
    validated_cases = _validate_judge_response(data, cases)

    # Build typed result from validated data.
    result_cases: list[CaseResult] = []
    for case_entry in validated_cases:
        case_id: int = case_entry["id"]
        verdicts: list[AssertionVerdict] = [
            AssertionVerdict(
                assertion_idx=v["assertion"],
                verdict=v["verdict"],
                reason=v["reason"],
            )
            for v in case_entry["verdicts"]
        ]
        result_cases.append(CaseResult(case_id=case_id, verdicts=verdicts))

    # Resolve the model string that was actually used.
    # synthesize() resolves the model internally; we surface the override or a
    # sentinel so callers can log which model produced this judgement.
    resolved_model = model or "default"

    return SkillJudgement(
        skill_name=skill_name,
        cases=result_cases,
        model=resolved_model,
        stub=False,
    )
