"""fieldkit.companion.decide — the deterministic brain (design D1, this change).

Maps one ``AttentionItem`` to a ``ProposedAction``: a candidate fieldkit
command, the read-only context command that makes the proposal reviewable, a
suggested skill, and a plain-language rationale. The mapping is a static table
(the D3 "the table IS the documented judgment" philosophy, same as
``mapping.py``) — fully deterministic, no LLM, testable under ``NO_LLM=1``.

``ProposedAction`` is the seam a future LLM brain plugs into: swap the table
lookup for an ``fieldkit.llm`` call that emits the same dataclass and nothing
downstream (outbox, gate, runner, journal) changes.

Safety invariant: the ``command_argv`` this module emits is always a read-only
fieldkit command (it passes ``gate.is_allowed`` at every tier). Mutating
previews are never emitted deterministically — an operator graduates a specific
``--dry-run`` command into ``companion.act_allowlist`` when they choose to.
"""

from dataclasses import dataclass

from fieldkit.companion.feed import AttentionItem
from fieldkit.companion.mapping import WATCHER_SEVERITY_MAP

_RUN_STATUS_PREFIX = "run-status/"

# Source-kind → the one-line "what to do" hint embedded in the proposal.
# Keyed by the AttentionItem.source prefix segment; falls back to a generic
# hint so an unmapped future source still produces a usable proposal.
_TASKS_SOURCE = "tasks/waiting-on"


@dataclass(frozen=True)
class ProposedAction:
    """What the loop proposes for one attention item.

    Attributes:
        skill: Suggested skill for a human/agent to apply, or None when the
            item has no skill mapping (e.g. a watcher run-status blip).
        rationale: Plain-language why-this-matters, embedded in the proposal.
        enrichment_argv: A read-only fieldkit command whose output is embedded
            in the proposal for standalone reviewability, or None when the item
            carries no account/context to enrich with.
        command_argv: The candidate fieldkit command this item suggests. The
            deterministic layer emits only read-only commands, which run at
            every tier without consulting the act allowlist. A future mutating
            action supplied through this seam must pass that exact-argv gate.
    """

    skill: str | None
    rationale: str
    enrichment_argv: tuple[str, ...] | None
    command_argv: tuple[str, ...] | None
    recommendation: str | None = None


def _account_read(account: str | None) -> tuple[str, ...] | None:
    """Return the read-only context command for *account*, or None."""
    if not account:
        return None
    # `pursuit health` takes the account as the -a/--account OPTION, not a
    # positional (historic regression — a positional errors with "unexpected extra argument").
    return ("pursuit", "health", "--account", account, "--json")


def _rationale(item: AttentionItem, hint: str) -> str:
    """Compose a one-line rationale from the item's severity, summary, and a hint."""
    account = f" [{item.account}]" if item.account else ""
    return f"{item.severity.upper()}{account}: {item.summary} — {hint} (evidence: {item.evidence_path})"


def _hint_for(item: AttentionItem) -> str:
    """Return the 'what to do' hint for a feed item based on its suggested skill."""
    if item.source == _TASKS_SOURCE:
        return "clear or update this Waiting-On item"
    if item.suggested_skill:
        return f"apply the {item.suggested_skill} skill"
    return "review this item"


def propose_for(item: AttentionItem) -> ProposedAction:
    """Map an attention item to a deterministic ProposedAction.

    Watcher run-status items point at the watcher's logs (read-only). All other
    items propose a read-only account-health pull as both the enrichment and the
    candidate command when an account is known, and carry the feed's suggested
    skill as human-facing advice.
    """
    if item.source.startswith(_RUN_STATUS_PREFIX):
        watcher = item.source[len(_RUN_STATUS_PREFIX) :]
        return ProposedAction(
            skill=None,
            rationale=_rationale(item, f"inspect the {watcher} watcher logs"),
            enrichment_argv=None,
            command_argv=("watch", "logs", watcher) if watcher in WATCHER_SEVERITY_MAP else None,
        )

    read = _account_read(item.account)
    return ProposedAction(
        skill=item.suggested_skill,
        rationale=_rationale(item, _hint_for(item)),
        enrichment_argv=read,
        command_argv=read,
    )
