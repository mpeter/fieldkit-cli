"""Named timeout constants for subprocess and HTTP calls (implementation note).

Centralises timeout values that were previously scattered as magic numbers
across 10+ files. All values are in seconds.

Usage:
    from fieldkit.config import TIMEOUT_HEALTH_CHECK, TIMEOUT_MCP_TOOL
"""

TIMEOUT_HEALTH_CHECK: int = 5
"""Quick liveness probes: wizard, completion checks."""

TIMEOUT_GH_CLI: int = 30
"""External CLI calls: gh issue list, gh pr create, etc."""

TIMEOUT_GWS_CLI: int = 30
"""Google Workspace CLI read and write calls."""

TIMEOUT_MCP_TOOL: int = 60
"""External tool / MCP calls: docs/cli, slack_threads."""

TIMEOUT_DATASYNC: int = 300
"""Long-running datasync operations."""

TIMEOUT_EVAL: int = 10
"""Skill eval runner subprocess calls."""

TIMEOUT_CRON: int = 10
"""Cron table read/write operations (watch/cli install-cron)."""

TIMEOUT_REPAIR: int = 30
"""Watch repair subprocess calls."""

TIMEOUT_COMPANION_ACTION: int = 120
"""Companion loop: per-command ceiling for a routed fieldkit subprocess.
_MAX_ITEMS_PER_PASS (companion/loop.py) x this value must stay under the
companion systemd unit's whole-pass TimeoutStartSec alongside decision retries
and act previews (historic regression)."""

TIMEOUT_COMPANION_DECISION: int = 30
"""Companion loop: one bounded LLM judgment per selected proposal item."""

TIMEOUT_HEALTH_GATE: int = 1800
"""Per-stage ceiling for full quality enforcement (pytest+coverage is the longest stage)."""

TIMEOUT_OIDC_HTTP: int = 30
"""ShadowBot OIDC/token-endpoint HTTP calls (auth.py refresh, silent auth, code exchange)."""

TIMEOUT_INTERACTIVE_AUTH: int = 900
"""MCPJungle OAuth: 600s for consent plus 300s for bootstrap and completion.
This ceiling bounds fieldkit's wait; it does not guarantee OAuth completion.
"""

TIMEOUT_SHADOWBOT_QUERY: int = 300
"""ShadowBot end-to-end query deadline, including thread creation and SSE streaming."""

TIMEOUT_SF_SESSION_CHECK: int = 10
"""Salesforce session liveness probe (session_check.py)."""

DRIVER_CHECK_OUTPUT_BYTES: int = 8 * 1024 * 1024
"""Maximum combined stdout and stderr bytes captured for one completion check."""

DRIVER_ATTEMPT_OUTPUT_BYTES: int = 64 * 1024 * 1024
"""Maximum combined output bytes across one verification attempt."""

DRIVER_OUTPUT_TAIL_BYTES: int = 8 * 1024
"""Maximum diagnostic bytes retained in memory for each output stream."""
