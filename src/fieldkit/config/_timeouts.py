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

TIMEOUT_HEALTH_GIT: int = 120
"""Nightly health sensor: git fetch / worktree add / remove (implementation change)."""

TIMEOUT_COMPANION_ACTION: int = 120
"""Companion loop: per-command ceiling for a routed fieldkit subprocess.
_MAX_ITEMS_PER_PASS (companion/loop.py) x this value must stay under the
companion systemd unit's whole-pass TimeoutStartSec alongside decision retries
and act previews (historic regression)."""

TIMEOUT_COMPANION_DECISION: int = 30
"""Companion loop: one bounded LLM judgment per selected proposal item."""

TIMEOUT_HEALTH_GATE: int = 1800
"""Nightly health sensor: per-gate ceiling (pytest+coverage is the longest gate).
One wedged tool degrades that check, not the whole run; the systemd unit's
RuntimeMaxSec is the whole-run backstop."""

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

TIMEOUT_RATE_LIMIT_POLL: int = 30
"""Driver: interval between stderr checks while an OpenCode session runs.
Also the granularity of sustained-rate-limit detection — abort requires new
rate-limit errors across _RATE_LIMIT_SUSTAINED_POLLS consecutive intervals,
so this value sets the minimum wall time before a run can be killed."""

TIMEOUT_PROCESS_KILL_GRACE: int = 10
"""Grace period to reap a killed child process before giving up on it."""

TIMEOUT_DRIVER_CHECK: int = 90
"""Driver: ordinary independent completion-check ceiling."""

TIMEOUT_DRIVER_RUN: int = 7200
"""Driver: cumulative service-run allowance shared by every attempt."""

TIMEOUT_DRIVER_FINALIZE_RESERVE: int = 600
"""Driver: portion of the cumulative allowance reserved for evidence and cleanup."""

TIMEOUT_DRIVER_GIT: int = 30
"""Driver verifier: bounded Git and detached-worktree operations."""

TIMEOUT_DRIVER_OUTPUT_DRAIN: int = 10
"""Driver verifier: final stream-drain allowance after process termination."""

DRIVER_CHECK_OUTPUT_BYTES: int = 8 * 1024 * 1024
"""Maximum combined stdout and stderr bytes captured for one completion check."""

DRIVER_ATTEMPT_OUTPUT_BYTES: int = 64 * 1024 * 1024
"""Maximum combined output bytes across one verification attempt."""

DRIVER_OUTPUT_TAIL_BYTES: int = 8 * 1024
"""Maximum diagnostic bytes retained in memory for each output stream."""
