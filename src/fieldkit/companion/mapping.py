"""fieldkit.companion.mapping — static severity, skill, and read-only tables.

These tables ARE the documented operational judgment (design D3) — the
same philosophy as ``pursuit/stage_weights.py``. They are string
contracts, so each is pinned by a characterization test that diffs it
against its upstream source (``watch/constants.py:KNOWN_WATCHERS``,
``__main__.py:_COMMANDS``) — the Pre-Mortem 4 lesson applied on day one.
"""

from typing import Final

# ---------------------------------------------------------------------------
# Watcher source → severity floor.
# Keys MUST cover watch/constants.py:KNOWN_WATCHERS (test_companion_mapping).
# Severity vocabulary reuses the watcher outcome idea: how urgently should
# an agent look at items from this source.
# ---------------------------------------------------------------------------

WATCHER_SEVERITY_MAP: Final[dict[str, str]] = {
    "morning-brief": "info",
    "pursuit-stalls": "warning",
    "account-health": "warning",
    "contract-expiry": "critical",
    "close-date-countdown": "critical",
    "slack-threads": "warning",
    "backstory-health": "warning",
    "waiting-on-tracker": "warning",
    "draft-queue": "info",
    "run-all": "info",
}

# ---------------------------------------------------------------------------
# Alert-file stem (the *-alerts.md prefix) → suggested skill.
# suggested_skill is advice for the agent, not routing (D3).
# ---------------------------------------------------------------------------

ALERT_SKILL_MAP: Final[dict[str, str]] = {
    "pursuit-stall": "grill",
    "account-health": "account-pulse",
    "contract-expiry": "engagement-health",
    "close-date-countdown": "grill",
    "slack-thread": "account-pulse",
    "backstory-health": "account-pulse",
    "waiting-on": "task-management",
    "draft-queue": "followup-draft",
}

# ---------------------------------------------------------------------------
# read-tier allowed commands: fieldkit invocations that write nothing.
# Keyed by (group, subcommand); a None subcommand allows the bare group.
# This is the third string contract — diffed against _COMMANDS in tests.
# ---------------------------------------------------------------------------

READ_ONLY_COMMANDS: Final[frozenset[tuple[str, str | None]]] = frozenset(
    {
        ("brief", "open"),
        ("companion", "feed"),
        ("companion", "allowed"),
        ("contact", "find"),
        ("contact", "list"),
        ("driver", "list"),
        ("driver", "status"),
        ("gmail", "query"),
        ("gmail", "decay"),
        ("gmail", "doctor"),
        ("gmail", "backstory-gap"),
        ("ingest", "status"),
        ("ingest", "backfill"),
        ("issue", "list"),
        ("issue", "show"),
        ("issue", "board"),
        ("meeting", "list"),
        ("pipeline", "quota"),
        ("pipeline", "open"),
        ("pursuit", "health"),
        ("pursuit", "forecast"),
        ("pursuit", "projects"),
        ("sf", "session-check"),
        ("skill", "list"),
        ("skill", "show"),
        ("skill", "variables"),
        ("version", None),
        ("watch", "logs"),
    }
)


# ---------------------------------------------------------------------------
# act-tier previewability: (group, subcommand) pairs whose CLI carries a
# literal --dry-run option. Consulted by gate.validate_allowlist (design D4 —
# act-tier commands must be previewable). Keyed the same way as
# READ_ONLY_COMMANDS but stored as "group subcommand" strings because that is
# the shape validate_allowlist's caller already builds from allowlist entries.
#
# sf set-next-steps is deliberately absent: it previews by default and writes
# only behind --confirm, not --dry-run. Folding that opposite polarity into
# this table is a second design decision, not a mechanical listing (implementation note).
# data-sync is absent because it is a single-token command (no subcommand) —
# validate_allowlist already rejects single-token entries on its own.
# ---------------------------------------------------------------------------

DRY_RUN_CAPABLE: Final[frozenset[str]] = frozenset(
    {
        "brief generate",
        "driver run",
        "health run",
        "gtask complete",
        "gtask create",
        "ingest backfill",
        "ingest discover",
        "ingest reprocess",
        "ingest route",
        "ingest run",
        "issue sync-milestone",
        "pursuit advance",
        "pursuit archive",
        "pursuit create",
        "pursuit rename",
        "pursuit repair-dates",
        "sf frontmatter",
        "sf reconcile",
        "skill install",
        "watch backstory-health",
        "watch close-date-countdown",
        "watch contract-expiry",
        "watch draft-queue",
        "watch pursuit-stalls",
        "watch slack-threads",
        "watch waiting-on-tracker",
    }
)


def suggested_skill_for(alert_stem: str) -> str | None:
    """Return the suggested skill for an alert-file stem, or None when unmapped.

    Matches on the longest mapped prefix so ``pursuit-stall-alerts.md``
    (stem ``pursuit-stall``) and future variants resolve consistently.
    """
    for prefix in sorted(ALERT_SKILL_MAP, key=len, reverse=True):
        if alert_stem.startswith(prefix):
            return ALERT_SKILL_MAP[prefix]
    return None
