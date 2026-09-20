"""Tests for fieldkit.pursuit.gate_criteria."""

import pytest

from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.gate_criteria import (
    ALLOWED_GATE_STATUSES,
    NATIVE_QUALIFICATION_PENDING_REASON,
    PENDING_NATIVE_QUALIFICATION_TRANSITIONS,
    evaluate_gate_policy,
)


@pytest.mark.unit
def test_pending_policy_contains_only_former_score_dependent_transitions() -> None:
    expected_transitions = frozenset(
        {
            (Stage.DISCOVER, Stage.VALIDATE),
            (Stage.VALIDATE, Stage.PROPOSE),
            (Stage.PROPOSE, Stage.NEGOTIATE),
        }
    )
    assert expected_transitions == PENDING_NATIVE_QUALIFICATION_TRANSITIONS


@pytest.mark.unit
@pytest.mark.parametrize("transition", sorted(PENDING_NATIVE_QUALIFICATION_TRANSITIONS))
def test_former_score_gate_is_pending_without_native_policy(transition: tuple[Stage, Stage]) -> None:
    decision = evaluate_gate_policy(*transition)
    assert decision.status == "pending"
    assert decision.passed is False
    assert decision.reasons == (NATIVE_QUALIFICATION_PENDING_REASON,)


@pytest.mark.unit
@pytest.mark.parametrize(
    "transition",
    [
        (Stage.PRE_PIPELINE, Stage.PROSPECT),
        (Stage.PROSPECT, Stage.QUALIFY),
        (Stage.QUALIFY, Stage.DISCOVER),
        (Stage.NEGOTIATE, Stage.CLOSED_WON),
        (Stage.DISCOVER, Stage.CLOSED_LOST),
    ],
)
def test_qualification_independent_transition_still_passes(transition: tuple[Stage, Stage]) -> None:
    decision = evaluate_gate_policy(*transition)
    assert decision.status == "pass"
    assert decision.passed is True
    assert decision.reasons == ()


@pytest.mark.unit
def test_allowed_gate_statuses_contains_expected_values():
    """ALLOWED_GATE_STATUSES contains the four valid status strings."""
    assert frozenset({"pending", "pass", "fail", "override"}) == ALLOWED_GATE_STATUSES


@pytest.mark.unit
def test_allowed_gate_statuses_is_frozenset():
    """ALLOWED_GATE_STATUSES is immutable (frozenset)."""
    assert isinstance(ALLOWED_GATE_STATUSES, frozenset)


@pytest.mark.unit
def test_stage_enum_values_are_hyphenated_strings():
    """Stage enum values use hyphenated lowercase strings matching frontmatter convention."""
    assert Stage.DISCOVER == "discover"
    assert Stage.VALIDATE == "validate"
    assert Stage.PROPOSE == "propose"
    assert Stage.NEGOTIATE == "negotiate"
    assert Stage.CLOSED_WON == "closed-won"
