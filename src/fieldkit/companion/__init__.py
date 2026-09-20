"""fieldkit.companion — attention feed + action gate for an agent running beside the operator.

The companion is the nervous system, not the brain (design D1): it turns
watcher artifacts, alert files, and TASKS.md into a cursored JSON feed
(``companion feed``), answers permission questions per the declared tier
(``companion allowed``), and executes gated actions through one choke
point that journals every outcome (``companion run``). No LLM calls, no
listeners, no long-lived processes — any agent runtime drives it via
plain subprocess calls.

Spec: openspec/changes/agent-companion-loop/ (ratified 2026-07-07).
"""

from fieldkit.companion.feed import AttentionItem as AttentionItem
from fieldkit.companion.feed import get_feed as get_feed
from fieldkit.companion.gate import is_allowed as is_allowed
