#!/usr/bin/env python3
"""PreToolUse hook: blocks autonomous outbound sends and Salesforce writes."""

import sys
from pathlib import Path

# Claude Code invokes this as `python3 hooks/outbound_gate.py` (a direct script
# run, not `python3 -m hooks.outbound_gate`), so sys.path[0] is hooks/ itself,
# not its parent -- the `hooks` package is unresolvable without this. Same
# pattern already used by hooks/pii_guard.py for its fieldkit import.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fieldkit.publication_policy import evaluate_publication_command  # noqa: E402
from hooks._common import block, read_payload, tool_input, tool_name  # noqa: E402


def main() -> int:
    payload = read_payload()
    if not isinstance(payload, dict):
        return 0

    name = tool_name(payload)
    inputs = tool_input(payload)

    # Block Gmail send attempts (Google Workspace MCP should only draft, never send)
    if name in (
        "mcp__fieldkit-mail__google_workspace__send_gmail_message",
        "mcp__mcpjungle__google_workspace__send_gmail_message",
    ):
        return block("Autonomous email sends are not allowed. Use draft_gmail_message instead.")

    # Block autonomous calendar invites: manage_event create/update/rsvp whenever
    # attendees would actually be notified. Creating an event with no attendees, or
    # with send_updates="none", never reaches anyone else and is fine; anything that
    # would notify an attendee needs the human to say so explicitly first.
    if name in (
        "mcp__fieldkit-calendar__google_workspace__manage_event",
        "mcp__mcpjungle__google_workspace__manage_event",
    ):
        action = inputs.get("action", "")
        attendees = inputs.get("attendees") or []
        send_updates = inputs.get("send_updates")
        send_updates_str = send_updates if isinstance(send_updates, str) else str(send_updates or "")
        if action in ("create", "update", "rsvp") and attendees and send_updates_str.lower() != "none":
            return block(
                "Autonomous calendar invites are not allowed, especially to "
                "customers/external attendees. Propose the specifics (time, attendees, "
                "agenda) in chat and get explicit human go-ahead before calling "
                "manage_event with attendees and send_updates other than 'none'."
            )

    # Block slackcli send commands via Bash tool
    if name == "Bash":
        command = inputs.get("command", "")
        if isinstance(command, str) and "slackcli messages send" in command:
            return block(
                "Autonomous Slack sends are not allowed. Draft the message and present it for human approval before sending."
            )

    # Block autonomous Salesforce field writes via Bash tool.
    # `fieldkit sf set-next-steps` requires --confirm, but defence-in-depth:
    # block any invocation that includes --confirm unless it is being run
    # interactively (i.e. we see it in a Bash tool call, which means an agent
    # is driving it, not a human typing in a terminal).
    if name == "Bash":
        command = inputs.get("command", "")
        if (
            isinstance(command, str)
            and "--confirm" in command
            and ("set-next-steps" in command or "set-field" in command)
        ):
            return block(
                "Autonomous Salesforce field writes are not allowed. "
                "Present the proposed change to the human and ask them to run "
                "the command directly: fieldkit sf set-next-steps --confirm <opp_id> <text>"
            )

    if name == "Bash":
        command = inputs.get("command", "")
        if isinstance(command, str):
            decision = evaluate_publication_command(command, cwd=Path.cwd())
            if not decision.allowed:
                return block(f"GitHub publication blocked: category={decision.category} source={decision.source}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
