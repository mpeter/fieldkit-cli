#!/usr/bin/env python3
"""PreToolUse hook: blocks calls to explicitly banned MCP tools across groups.

Converts CLAUDE.md prose rules into hard mechanical enforcement points.

Reads Claude Code tool invocation JSON from stdin.
Exit 0 = allow, exit 2 = block with clear reason.

CLAUDE.md rules enforced:
  - Backstory opportunity-level tools: banned — unreliable attribution
    (opportunity data is attributed to the wrong deals; account-level only)
  - Any future tool bans added to BANNED_TOOLS are enforced automatically.
"""

import sys
from pathlib import Path

# Claude Code invokes this as `python3 hooks/tool_scope_guard.py` (a direct
# script run, not `python3 -m hooks...`), so sys.path[0] is hooks/ itself,
# not its parent -- the `hooks` package is unresolvable without this. Same
# pattern already used by hooks/pii_guard.py for its fieldkit import.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hooks._common import is_mcp_tool, read_payload, tool_name  # noqa: E402

# Qualified tool names to block, mapped to their block reason. The group prefix
# can change without changing the underlying tool or its policy.
BANNED_TOOLS: dict[str, str] = {
    # Backstory opportunity-level tools — unreliable attribution per CLAUDE.md.
    # Opportunity tools misattribute communication signals to wrong deals.
    # Use account-level tools (find_account, get_account_status,
    # get_recent_account_activity, account_company_news) instead.
    "backstory__get_opportunity_status": (
        "opportunity-level Backstory tools have unreliable attribution "
        "(signals get misattributed across deals). Use account-level tools instead."
    ),
    "backstory__get_recent_opportunity_activity": (
        "opportunity-level Backstory tools have unreliable attribution "
        "(signals get misattributed across deals). Use account-level tools instead."
    ),
}


def main() -> int:
    payload = read_payload()

    name = tool_name(payload)
    if not name:
        return 0

    reason = next((reason for tool, reason in BANNED_TOOLS.items() if is_mcp_tool(name, tool)), None)
    if reason is None:
        return 0

    print(
        f"BLOCKED: {name} is banned per project policy.",
        file=sys.stderr,
    )
    print(f"Reason: {reason}", file=sys.stderr)
    print("See CLAUDE.md > Tool Routing > Key Tool Rules.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
