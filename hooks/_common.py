#!/usr/bin/env python3
"""Shared helpers for PreToolUse hooks.

Every PreToolUse hook reads a Claude Code event from stdin as JSON, decides,
and signals the result through its exit code:

    exit 0  -> allow (stay silent)
    exit 2  -> block; whatever is written to stderr is fed back to the model

The helpers here keep each hook short and consistent.
Parsing fails open (empty dict) on bad input so a glitch never wedges the
agent — the narrow guards in each hook decide on content, not on the absence
of it.
"""

import json
import sys


def read_payload() -> dict:  # type: ignore[type-arg]
    """Parse the hook event from stdin. Fails open on bad/missing input."""
    try:
        return json.load(sys.stdin)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, ValueError):
        return {}


def tool_name(payload: dict) -> str:  # type: ignore[type-arg]
    """Return the tool_name field, guaranteed non-None string."""
    return payload.get("tool_name", "") or ""


def tool_input(payload: dict) -> dict:  # type: ignore[type-arg]
    """Return the tool_input field, guaranteed dict."""
    value = payload.get("tool_input")
    return value if isinstance(value, dict) else {}


def block(msg: str) -> int:
    """Print a single-line BLOCKED message to stderr and return exit code 2."""
    print(f"BLOCKED: {msg}", file=sys.stderr)
    return 2
