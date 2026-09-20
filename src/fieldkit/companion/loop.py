"""fieldkit.companion.loop — the single-pass headless brain (design, this change).

``run_once`` performs one poll→decide→gate→act→journal pass over the attention
feed and returns a typed ``LoopResult``. It is the in-repo replacement for the
"external agent brain" design D1 left open — no Claude Code, no OpenCode, no
harness ``/loop``; a systemd ``--user`` timer paces it (autonomy-roadmap Stage 4).

Tier ladder (honored, never widened — Decision 3):

* ``read``    — run each item's read-only context command; journal. No outbox.
* ``propose`` — optionally refine the deterministic action through the shared
  LLM boundary, then write one ``companion-outbox`` proposal per item without
  executing its candidate command.
* ``act``     — additionally execute a validated preview candidate only when it
  passes the existing runner gate and configured exact-command allowlist.

Evidence vs. control: the journal records what happened on **every** path and
never withholds anything from the feed. What the feed withholds lives in
``fieldkit.companion.suppress`` — a TTL'd cooldown written only on success, plus
the presence of a pending outbox proposal. Keeping these separate is what lets a
failure be recorded without costing the item its place in the feed, and lets a
wrongly-retired item return without editing the audit trail.

Ordering & failure discipline (proctor C1/C2):

* The proposal file is written **before** the item is journaled or retired, so a
  ``write_proposal`` failure leaves the item un-retired and therefore
  re-attempted on the next pass — a detected item can never silently vanish.
* A routed command runs as a fieldkit subprocess, so an auth failure surfaces
  as the child's exit code 2, not as an in-process exception. ``run_once``
  therefore inspects the child exit code and reports auth failures distinctly
  (``auth_failures`` → the CLI exits 2), never folding them into the
  best-effort ``enrich_failures`` partial bucket — a dead credential stops the
  timer instead of being retried forever.
"""

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fieldkit.companion.decide import ProposedAction, propose_for
from fieldkit.companion.feed import AttentionItem, get_feed
from fieldkit.companion.gate import TIER_ORDER, Tier, is_allowed
from fieldkit.companion.journal import append_journal
from fieldkit.companion.llm_decide import DecisionResult
from fieldkit.companion.outbox import proposal_item_ids, write_proposal
from fieldkit.companion.runner import EXIT_DENIED, ActionResult, run_action
from fieldkit.companion.suppress import add_cooldown, retired_item_ids

log = logging.getLogger(__name__)

# Seven worst-case enrichment calls plus seven decision calls fit the unit's
# 20-minute whole-pass backstop while preserving the existing fairness cap.
_MAX_ITEMS_PER_PASS = 7

# Exit code a routed fieldkit child process uses for an auth failure (the shared
# 0/1/2/3 taxonomy). Kept local to mirror runner.EXIT_DENIED rather than importing
# cli_exit into the domain layer.
_CHILD_EXIT_AUTH = 2


def _select_pass(items: list[AttentionItem], cap: int) -> list[AttentionItem]:
    """Select up to *cap* items for this pass, round-robin across severity tiers.

    ``items`` MUST already be severity-first sorted (get_feed's contract), so
    same-severity items are contiguous. Without this, a single overflowing
    severity tier consumes the whole cap and permanently starves every lower
    tier — a chronically-failing critical never cools down (suppress.py), so
    the identical head repeats every pass forever (proctor F3). Round-robin
    gives every severity tier PRESENT a slot each round before any tier gets a
    second one, so a coexisting lower tier is never fully excluded.

    Within-tier fairness among an overflowing tier's own excess items is out
    of scope here — that tier's own tail can still be excluded pass after
    pass; only cross-tier starvation is addressed.

    The result preserves the original severity-first relative order, so
    processing/journal order is unaffected by the rotation — only pass
    membership changes.
    """
    if len(items) <= cap:
        return items

    groups: dict[str, list[AttentionItem]] = {}
    tiers: list[str] = []
    for item in items:
        if item.severity not in groups:
            groups[item.severity] = []
            tiers.append(item.severity)
        groups[item.severity].append(item)

    selected: list[AttentionItem] = []
    cursors = dict.fromkeys(tiers, 0)
    while len(selected) < cap and any(cursors[tier] < len(groups[tier]) for tier in tiers):
        for tier in tiers:
            if len(selected) >= cap:
                break
            cursor = cursors[tier]
            if cursor < len(groups[tier]):
                selected.append(groups[tier][cursor])
                cursors[tier] = cursor + 1

    selected_ids = {item.item_id for item in selected}
    return [item for item in items if item.item_id in selected_ids]


def _enrichment_succeeded(argv: tuple[str, ...], result: ActionResult) -> bool:
    """Return whether a read-only command produced usable enrichment."""
    return result.exit_code == 0


@dataclass(frozen=True)
class LoopResult:
    """Machine-parseable outcome of one loop pass (Principle III / Decision 6).

    Attributes:
        tier: The effective tier the pass ran at.
        triaged: read-tier items handled (context run + journaled, no outbox).
        proposed: outbox proposals written (propose/act tiers).
        unresolved: items without a command route, left live for source-data repair.
        enriched: read-only context commands that ran successfully (any tier).
        acted: non-read-only (allowlisted) commands executed without denial.
        denied: commands the gate refused (journaled as refusals).
        enrich_failures: context commands that exited nonzero for a non-auth
            reason (best-effort — reported as a partial pass, exit 1).
        auth_failures: context/act commands that exited 2 (auth). Any auth
            failure makes the CLI exit 2 so a dead credential is not retried.
        dropped: feed items beyond `_MAX_ITEMS_PER_PASS` this pass did not
            reach (historic regression F4). Non-zero on a sustained pass overflow is a
            signal to investigate, not a failure — `_select_pass` round-robins
            across severity tiers (F3), so a dropped item is excess from
            whichever tier(s) overflowed the cap, not necessarily low severity.
        llm_attempts: provider-backed decision calls attempted.
        llm_decisions: structurally valid LLM recommendations retained.
        llm_fallbacks: deterministic or command-only fallbacks recorded.
    """

    tier: Tier
    triaged: int = 0
    proposed: int = 0
    unresolved: int = 0
    enriched: int = 0
    acted: int = 0
    denied: int = 0
    enrich_failures: int = 0
    auth_failures: int = 0
    dropped: int = 0
    llm_attempts: int = 0
    llm_decisions: int = 0
    llm_fallbacks: int = 0

    @property
    def handled(self) -> int:
        """Total feed items processed this pass."""
        return self.triaged + self.proposed + self.unresolved

    @property
    def partial(self) -> bool:
        """True when the pass completed but a best-effort context read failed."""
        return self.enrich_failures > 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation (with derived fields)."""
        data = asdict(self)
        data["handled"] = self.handled
        data["partial"] = self.partial
        return data


@dataclass
class _PassState:
    already_proposed: set[str]
    enrich_cache: dict[tuple[str, ...], ActionResult]
    triaged: int = 0
    proposed: int = 0
    unresolved: int = 0
    enriched: int = 0
    acted: int = 0
    denied: int = 0
    enrich_failures: int = 0
    auth_failures: int = 0
    llm_attempts: int = 0
    llm_decisions: int = 0
    llm_fallbacks: int = 0


def _enrichment_command(baseline: ProposedAction) -> tuple[str, ...] | None:
    command = baseline.enrichment_argv
    if command is None and baseline.command_argv is not None and is_allowed(list(baseline.command_argv), "read", []):
        return baseline.command_argv
    return command


def _run_enrichment(
    state: _PassState, command: tuple[str, ...] | None, data_path: Path, item_id: str
) -> tuple[ActionResult | None, str | None]:
    if command is None:
        return None, None
    result = state.enrich_cache.get(command)
    if result is None:
        result = run_action(
            list(command), tier="read", allowlist=[], data_path=data_path, item_id=item_id, journal=False
        )
        if result.denied or _enrichment_succeeded(command, result):
            state.enrich_cache[command] = result
    if result.denied:
        state.denied += 1
    elif result.exit_code == _CHILD_EXIT_AUTH:
        state.auth_failures += 1
    elif not _enrichment_succeeded(command, result):
        state.enrich_failures += 1
    else:
        state.enriched += 1
        return result, result.stdout or None
    return result, None


def _record_decision(state: _PassState, decision: DecisionResult) -> None:
    state.llm_attempts += int(decision.attempted)
    state.llm_decisions += int(decision.provenance == "llm")
    state.llm_fallbacks += int(decision.fallback is not None)


def _finish_read(
    state: _PassState,
    data_path: Path,
    item: AttentionItem,
    command: tuple[str, ...] | None,
    result: ActionResult | None,
) -> None:
    state.triaged += 1
    if command is None or result is None:
        return
    append_journal(data_path, item_id=item.item_id, action=" ".join(command), exit_code=result.exit_code)
    if _enrichment_succeeded(command, result):
        add_cooldown(data_path, item.item_id, reason=f"read:{item.source}")


def _execute_candidate(
    state: _PassState,
    data_path: Path,
    item: AttentionItem,
    candidate: tuple[str, ...],
    enrichment_command: tuple[str, ...] | None,
    enrichment_result: ActionResult | None,
    allowlist: list[str],
    decision: DecisionResult,
) -> None:
    reused = candidate == enrichment_command and enrichment_result is not None
    if not reused and enrichment_command is not None and enrichment_result is not None:
        append_journal(
            data_path,
            item_id=item.item_id,
            action=" ".join(enrichment_command),
            exit_code=enrichment_result.exit_code,
            decision_provenance=decision.provenance,
            fallback_category=decision.fallback,
        )
    result = (
        enrichment_result
        if reused
        else run_action(
            list(candidate), tier="act", allowlist=allowlist, data_path=data_path, item_id=item.item_id, journal=False
        )
    )
    assert result is not None
    if not reused:
        if result.denied:
            state.denied += 1
        elif result.exit_code == _CHILD_EXIT_AUTH:
            state.auth_failures += 1
        elif result.exit_code != 0:
            state.enrich_failures += 1
        elif not is_allowed(list(candidate), "read", []):
            state.acted += 1
    append_journal(
        data_path,
        item_id=item.item_id,
        action=" ".join(candidate),
        exit_code=EXIT_DENIED if result.denied else result.exit_code,
        decision_provenance=decision.provenance,
        fallback_category=decision.fallback,
    )


def _process_item(
    state: _PassState,
    item: AttentionItem,
    *,
    effective: Tier,
    allowlist: list[str],
    data_path: Path,
    dry_run: bool,
    decision_fn: Callable[[AttentionItem, ProposedAction, str | None], DecisionResult] | None,
) -> None:
    baseline = propose_for(item)
    enrichment_command = _enrichment_command(baseline)
    if enrichment_command is None and baseline.command_argv is None:
        state.unresolved += 1
        return
    if dry_run:
        if effective == "read":
            state.triaged += 1
            return
        decision = DecisionResult(baseline, "deterministic", "disabled")
        _record_decision(state, decision)
        state.proposed += 1
        return

    enrichment_result, enrichment_output = _run_enrichment(state, enrichment_command, data_path, item.item_id)
    if effective == "read":
        _finish_read(state, data_path, item, enrichment_command, enrichment_result)
        return
    decision = (
        decision_fn(item, baseline, enrichment_output)
        if decision_fn
        else DecisionResult(baseline, "deterministic", "disabled")
    )
    _record_decision(state, decision)
    if item.item_id not in state.already_proposed:
        write_proposal(
            data_path,
            item,
            decision.action,
            enrichment_output=enrichment_output,
            decision_provenance=decision.provenance,
            fallback_category=decision.fallback,
        )
    state.proposed += 1
    candidate = decision.action.command_argv
    if effective == "act" and candidate is not None:
        _execute_candidate(
            state, data_path, item, candidate, enrichment_command, enrichment_result, allowlist, decision
        )
    elif enrichment_command is not None and enrichment_result is not None:
        append_journal(
            data_path,
            item_id=item.item_id,
            action=" ".join(enrichment_command),
            exit_code=enrichment_result.exit_code,
            decision_provenance=decision.provenance,
            fallback_category=decision.fallback,
        )


def run_once(
    home: Path,
    data_path: Path,
    *,
    tier: Tier,
    allowlist: list[str],
    dry_run: bool = False,
    decision_fn: Callable[[AttentionItem, ProposedAction, str | None], DecisionResult] | None = None,
) -> LoopResult:
    """Run one companion loop pass over the attention feed.

    Args:
        home: fieldkit workspace root (feed inputs: ``watchers/``, ``TASKS.md``).
        data_path: fieldkit data root (outbox, journal).
        tier: ``read`` / ``propose`` / ``act``. An unknown value fails closed to
            ``read`` (matching the gate's fail-closed contract).
        allowlist: the act-tier exact-argv allowlist (consulted only at ``act``).
        dry_run: when True, compute what each item would produce but write no
            outbox file, run no command, and append no journal record.

    Returns:
        A ``LoopResult`` with per-tier counts.

    Raises:
        FeedParseError: when an upstream feed input is structurally unparseable
            (exit 3 at the CLI).
    """
    effective: Tier = tier if tier in TIER_ORDER else "read"
    items = get_feed(
        home,
        data_path,
        since_cursor=False,
        suppressed=retired_item_ids(data_path),
    )
    dropped = max(0, len(items) - _MAX_ITEMS_PER_PASS)
    if dropped:
        log.warning(
            "companion pass feed has %d items, keeping %d round-robin across severity tiers; dropping %d",
            len(items),
            _MAX_ITEMS_PER_PASS,
            dropped,
        )
    items = _select_pass(items, _MAX_ITEMS_PER_PASS)
    state = _PassState(proposal_item_ids(data_path), {})
    for item in items:
        _process_item(
            state,
            item,
            effective=effective,
            allowlist=allowlist,
            data_path=data_path,
            dry_run=dry_run,
            decision_fn=decision_fn,
        )

    return LoopResult(
        tier=effective,
        triaged=state.triaged,
        proposed=state.proposed,
        unresolved=state.unresolved,
        enriched=state.enriched,
        acted=state.acted,
        denied=state.denied,
        enrich_failures=state.enrich_failures,
        auth_failures=state.auth_failures,
        dropped=dropped,
        llm_attempts=state.llm_attempts,
        llm_decisions=state.llm_decisions,
        llm_fallbacks=state.llm_fallbacks,
    )
