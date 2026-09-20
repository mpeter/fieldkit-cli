"""Qualification-policy state for pursuit stage advancement.

Salesforce ClosePlan owns current qualification. The former local 0--3
thresholds are historical evidence only and cannot authorize a transition.
Until a native policy is ratified, transitions that depended on those
thresholds remain pending and require an explicit override.
"""

from dataclasses import dataclass
from typing import Literal

from fieldkit.pursuit.enums import Stage

GatePolicyStatus = Literal["pass", "pending"]

NATIVE_QUALIFICATION_PENDING_REASON = "Salesforce-native qualification policy is not yet ratified for this transition"

PENDING_NATIVE_QUALIFICATION_TRANSITIONS: frozenset[tuple[Stage, Stage]] = frozenset(
    {
        (Stage.DISCOVER, Stage.VALIDATE),
        (Stage.VALIDATE, Stage.PROPOSE),
        (Stage.PROPOSE, Stage.NEGOTIATE),
    }
)

# Valid gate_status values for pursuit frontmatter.
ALLOWED_GATE_STATUSES: frozenset[str] = frozenset({"pending", "pass", "fail", "override"})


@dataclass(frozen=True)
class GatePolicyDecision:
    """Current policy decision for one stage transition."""

    status: GatePolicyStatus
    reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Return whether the transition can proceed without an override."""
        return self.status == "pass"


def evaluate_gate_policy(from_stage: str, to_stage: str) -> GatePolicyDecision:
    """Evaluate only ratified current policy, never historical local scores."""
    transition = (from_stage, to_stage)
    if transition in PENDING_NATIVE_QUALIFICATION_TRANSITIONS:
        return GatePolicyDecision(status="pending", reasons=(NATIVE_QUALIFICATION_PENDING_REASON,))
    return GatePolicyDecision(status="pass")
