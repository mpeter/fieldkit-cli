"""fieldkit doctor — CLI-tool-on-PATH checks, folded in from the old `setup verify`.

These aren't per-service credential checks, so they only appear in the bare
`fieldkit doctor` (all-services) view, not as a `doctor <service>` leaf.
"""

import os
import shutil
from dataclasses import dataclass

_REQUIRED_TOOLS = ("claude", "mcpjungle", "slackcli")


@dataclass(frozen=True)
class ToolCheck:
    """Availability result for one external command-line dependency."""

    name: str
    found: bool


def check_tools() -> list[ToolCheck]:
    """Return PATH availability for each CLI tool fieldkit shells out to."""
    return [ToolCheck(name=tool, found=shutil.which(tool) is not None) for tool in _REQUIRED_TOOLS]


def check_slack_tokens_dead_config() -> str | None:
    """Warn if Slack MCP tokens are set but unused (historic regression: dead config).

    Returns a warning message, or None if there's nothing to warn about.
    """
    xoxc = os.environ.get("SLACK_MCP_XOXC_TOKEN", "")
    xoxd = os.environ.get("SLACK_MCP_XOXD_TOKEN", "")
    if xoxc or xoxd:
        return (
            "SLACK_MCP_XOXC_TOKEN / SLACK_MCP_XOXD_TOKEN set but no Slack MCP server is "
            "registered in mcpjungle — tokens are not actively used by fieldkit"
        )
    return None
