"""fieldkit.web.chat — fieldkit-context chat over the two-layer LLM model.

The chat handler is Layer 2 only: it assembles deterministic Layer-1
context (latest brief, pipeline health, quota) and hands one prompt to
``fieldkit.llm.core.synthesize``. No conversation state is kept server-
side — the client resends recent turns, which keeps the server stateless
and the endpoint trivially restartable.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from fieldkit.errors import LLMError, WebDataError
from fieldkit.llm.sanitize import UNTRUSTED_DATA_PREAMBLE, wrap_user_data
from fieldkit.web.data import DataSource

log = logging.getLogger(__name__)

# Static system instruction — never receives user content (see synthesize()).
_SYSTEM_PROMPT = (
    f"{UNTRUSTED_DATA_PREAMBLE}\n\n"
    "You are the fieldkit assistant: a terse, factual copilot for an account "
    "executive's local sales-operations data. Answer only from the context "
    "provided. When the context lacks the answer, say so and name the fieldkit "
    "command that would produce it. Never fabricate deal data, metrics, names, "
    "or dates. Prefer short answers with concrete next actions."
)

# Budgets keep the assembled prompt well under the synthesize() size guard.
_BRIEF_BUDGET_CHARS = 12_000
_HEALTH_BUDGET_CHARS = 8_000
_HISTORY_MAX_TURNS = 6
_HISTORY_TURN_BUDGET_CHARS = 2_000
_QUESTION_MAX_CHARS = 4_000


def _clip(text: str, budget: int) -> str:
    """Truncate *text* to *budget* chars with an explicit truncation marker."""
    if len(text) <= budget:
        return text
    return text[:budget] + "\n[... truncated ...]"


def build_context(source: DataSource) -> str:
    """Assemble the deterministic context block for a chat turn.

    Each section degrades independently: a failing provider contributes
    an ``unavailable`` line instead of failing the whole chat request.
    """
    sections: list[str] = []

    brief = source.latest_brief()
    if brief is not None:
        sections.append(
            f"## Latest morning brief ({brief.name})\n"
            f"{wrap_user_data(_clip(brief.markdown, _BRIEF_BUDGET_CHARS), 'latest_morning_brief')}"
        )
    else:
        sections.append("## Latest morning brief\nunavailable — no saved brief found")

    try:
        health = source.pipeline_health()
        sections.append(
            "## Pipeline health (JSON)\n"
            f"{wrap_user_data(_clip(json.dumps(health, default=str), _HEALTH_BUDGET_CHARS), 'pipeline_health')}"
        )
    except WebDataError as exc:
        log.warning("chat context: pipeline health unavailable: %s", exc)
        sections.append("## Pipeline health\nunavailable")

    try:
        quota = source.quota()
        sections.append(f"## Quota gap (JSON)\n{wrap_user_data(json.dumps(quota, default=str), 'quota_gap')}")
    except WebDataError as exc:
        log.warning("chat context: quota unavailable: %s", exc)
        sections.append("## Quota gap\nunavailable")

    return "\n\n".join(sections)


def answer(
    question: str,
    source: DataSource,
    *,
    history: list[dict[str, str]] | None = None,
    synthesize_fn: Callable[..., str] | None = None,
) -> str:
    """Answer one chat question with fieldkit context.

    Args:
        question: The user's message.
        source: Data source for context assembly.
        history: Optional prior turns as ``[{"role": ..., "content": ...}]``;
            only the last few are included.
        synthesize_fn: Injectable LLM call (defaults to
            ``fieldkit.llm.core.synthesize``).

    Returns:
        The assistant reply.

    Raises:
        LLMError: When the LLM call fails (mapped to HTTP 503 by the server).
        WebDataError: When the question is empty or oversized.
    """
    question = question.strip()
    if not question:
        raise WebDataError("empty chat message")
    if len(question) > _QUESTION_MAX_CHARS:
        raise WebDataError(f"chat message exceeds {_QUESTION_MAX_CHARS} chars")

    if synthesize_fn is None:
        from fieldkit.llm.core import synthesize

        synthesize_fn = synthesize

    parts = [f"# fieldkit context\n\n{build_context(source)}"]
    for turn in (history or [])[-_HISTORY_MAX_TURNS:]:
        role = "Operator" if turn.get("role") == "user" else "Assistant"
        # Client-supplied turns are untrusted input — clip each one so six
        # oversized turns cannot bomb the prompt (cost) or trip the size guard.
        parts.append(
            f"{role}: {wrap_user_data(_clip(turn.get('content', ''), _HISTORY_TURN_BUDGET_CHARS), 'chat_history')}"
        )
    parts.append(f"Operator question: {wrap_user_data(question, 'operator_question')}")

    reply: Any = synthesize_fn("\n\n".join(parts), system=_SYSTEM_PROMPT)
    if not isinstance(reply, str):
        raise LLMError("synthesize returned a non-string reply")
    return reply
