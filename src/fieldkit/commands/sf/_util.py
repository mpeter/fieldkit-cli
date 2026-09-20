"""Shared utilities for sf command modules."""

import sys


def stdin_is_interactive() -> bool:
    """True when stdin is a real TTY (a human at a keyboard).

    Gate interactive prompts (e.g. click.prompt) behind this check so that
    agent/CI callers with --confirm proceed to the write without blocking on
    keyboard input.  Kept as a named function (rather than inline) to allow
    deterministic unit-test patching.
    """
    return sys.stdin.isatty()
