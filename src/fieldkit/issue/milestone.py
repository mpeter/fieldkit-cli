"""Provider-independent milestone transition planning and partial outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from fieldkit.errors import AuthError, GitHubDataError, GitHubRequestError
from fieldkit.issue.model import GHIssue, IssueStatus

MilestoneState = Literal["queued", "completed"]
AdvanceFailure = Literal["authentication", "data", "missing", "retryable"]


class IssueMilestoneStore(Protocol):
    """Operations required to execute a milestone transition."""

    def update_status(
        self,
        issue_id: str,
        status: IssueStatus,
        *,
        note: str | None = None,
    ) -> GHIssue | None: ...

    def mark_fixed(
        self,
        issue_id: str,
        *,
        commit: str | None = None,
        note: str | None = None,
    ) -> GHIssue | None: ...


@dataclass(frozen=True)
class MilestonePlan:
    """Issues eligible to advance for one milestone state observation."""

    target_status: IssueStatus
    action_label: str
    candidates: list[GHIssue]
    linked: int
    skipped: int


@dataclass(frozen=True)
class AdvanceOutcome:
    """Bounded result for one attempted transition."""

    issue_id: str
    updated: GHIssue | None
    failure: AdvanceFailure | None = None


def plan_milestone_sync(issues: list[GHIssue], state: MilestoneState) -> MilestonePlan:
    """Select only transitions defined for the observed milestone state."""
    if state == "queued":
        candidates = [issue for issue in issues if issue.status == "open"]
        target_status: IssueStatus = "planned"
        action_label = "open → planned"
    else:
        candidates = [issue for issue in issues if issue.status == "planned"]
        target_status = "fixed"
        action_label = "planned → fixed"
    return MilestonePlan(
        target_status=target_status,
        action_label=action_label,
        candidates=candidates,
        linked=len(issues),
        skipped=len(issues) - len(candidates),
    )


def advance_milestone_candidates(
    store: IssueMilestoneStore,
    plan: MilestonePlan,
    *,
    commit: str | None,
) -> list[AdvanceOutcome]:
    """Attempt every candidate and retain a safe failure class per issue."""
    outcomes: list[AdvanceOutcome] = []
    for issue in plan.candidates:
        try:
            if plan.target_status == "fixed":
                updated = store.mark_fixed(issue.id, commit=commit)
            else:
                updated = store.update_status(issue.id, plan.target_status)
        except AuthError:
            outcomes.append(AdvanceOutcome(issue.id, None, "authentication"))
        except GitHubDataError:
            outcomes.append(AdvanceOutcome(issue.id, None, "data"))
        except GitHubRequestError:
            outcomes.append(AdvanceOutcome(issue.id, None, "retryable"))
        else:
            outcomes.append(AdvanceOutcome(issue.id, updated, None if updated is not None else "missing"))
    return outcomes


def milestone_exit_code(outcomes: list[AdvanceOutcome]) -> int:
    """Return the canonical non-success status for a batch, or zero."""
    failures = {outcome.failure for outcome in outcomes if outcome.failure is not None}
    if "authentication" in failures:
        return 2
    if failures & {"data", "missing"}:
        return 3
    if "retryable" in failures:
        return 1
    return 0
