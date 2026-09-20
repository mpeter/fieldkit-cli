"""fieldkit.health.filing — dedup predicate and issue filing for sensed regressions.

Filing goes through an injected :class:`IssueFiler` — the CLI adapter backs it
with the same ``GHIssueStore`` that ``fieldkit issue create`` uses, so PII-guard,
ID allocation, and label behavior stay identical (design Decision 2). The
sensor files **unlabeled** issues: only the operator applies ``agent-ready``
(design Decision 1 — the consent bit stays human).

Green runs never touch the filer at all: with zero failing checks this module
returns without a single filer call (spec: "the issue-raise path is not
invoked").
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from fieldkit.health.checks import CheckResult

log = logging.getLogger(__name__)

#: Stable title marker — the dedup key (design Decision 4). Changing this
#: orphans every open sensor-filed issue; treat as a contract.
HEALTH_TITLE_MARKER = "nightly health:"

#: Machine marker embedded in issue bodies for tooling.
BODY_MARKER_TEMPLATE = "<!-- fieldkit-health-check: {check_id} -->"


class IssueFiler(Protocol):
    """Minimal filing surface the health domain needs (implemented over GHIssueStore)."""

    def open_titles(self) -> list[str]:
        """Return titles of issues still open (fieldkit status open or planned)."""
        ...

    def file_regression(self, *, title: str, body: str) -> str:
        """File one issue (no ``agent-ready`` label) and return its reference."""
        ...


def issue_title_for(check_id: str) -> str:
    """Return the stable, dedupable issue title for a failing check."""
    return f"{HEALTH_TITLE_MARKER} {check_id} gate failing on main"


def issue_body_for(result: CheckResult) -> str:
    """Render the issue body: machine marker, provenance, and the failing output.

    The output tail is untrusted text (T1): the code fence is sized to exceed
    the longest backtick run in the tail so tool output cannot break out of
    the fence and inject markdown into the issue body.
    """
    marker = BODY_MARKER_TEMPLATE.format(check_id=result.check_id)
    output = result.output.strip()
    longest_backtick_run = max((len(m) for m in re.findall(r"`+", output)), default=0)
    fence = "`" * max(3, longest_backtick_run + 1)
    return (
        f"{marker}\n\n"
        f"Filed automatically by the nightly health sensor (`fieldkit health run`, implementation change).\n"
        f"The `{result.check_id}` gate failed against a clean checkout of `origin/main`.\n\n"
        "Sensors sense; they never move a gate — fix the regression, not the threshold.\n"
        "Apply `agent-ready` only after triage (operator consent bit).\n\n"
        f"### Failing output (tail)\n\n{fence}\n{output}\n{fence}\n"
    )


@dataclass(frozen=True)
class FilingOutcome:
    """What the filing pass did for one run."""

    filed: tuple[str, ...]  # check_ids newly filed this run
    deduped: tuple[str, ...]  # check_ids with a matching still-open issue


def file_regressions(results: Sequence[CheckResult], filer: IssueFiler, *, dry_run: bool = False) -> FilingOutcome:
    """File one issue per *new* failing check; dedup against still-open issues.

    Args:
        results: All check results from the run.
        filer:   Injected issue-filing backend.
        dry_run: Log intended filings without calling ``file_regression``.

    Returns:
        A :class:`FilingOutcome` — which check_ids were filed vs. deduped.

    Raises:
        RuntimeError: propagated from the filer (``gh`` failures) — the caller
            classifies this as a runner problem (``partial``), never silence.
    """
    failing = [r for r in results if r.status == "fail"]
    if not failing:
        return FilingOutcome(filed=(), deduped=())

    open_titles = [t.lower() for t in filer.open_titles()]
    filed: list[str] = []
    deduped: list[str] = []
    for result in failing:
        title = issue_title_for(result.check_id)
        # Dedup on marker + check-id (not the full title) so an operator who
        # appends context to an open issue's title does not cause a nightly
        # duplicate (design Decision 4).
        dedup_key = f"{HEALTH_TITLE_MARKER} {result.check_id}".lower()
        if any(dedup_key in open_title for open_title in open_titles):
            log.info("health: %s still failing — matching open issue exists, not refiling", result.check_id)
            deduped.append(result.check_id)
            continue
        if dry_run:
            log.info("health: [dry-run] would file %r", title)
            filed.append(result.check_id)
            continue
        # Sequential, awaited filing — see design Decision 4 (the #1436 next-id
        # race needs concurrent raisers; one-at-a-time filing sidesteps it).
        ref = filer.file_regression(title=title, body=issue_body_for(result))
        log.info("health: filed %s for %s", ref, result.check_id)
        filed.append(result.check_id)
    return FilingOutcome(filed=tuple(filed), deduped=tuple(deduped))
