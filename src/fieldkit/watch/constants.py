"""Named constants for all known fieldkit watcher names.

Derived from ``_WATCHER_ORDER`` in ``src/fieldkit/commands/watch/cli.py``.
Import ``KNOWN_WATCHERS`` to validate watcher names at runtime.
"""

from typing import Final

WATCHER_MORNING_BRIEF: Final = "morning-brief"
WATCHER_PURSUIT_STALLS: Final = "pursuit-stalls"
WATCHER_ACCOUNT_HEALTH: Final = "account-health"
WATCHER_CONTRACT_EXPIRY: Final = "contract-expiry"
WATCHER_CLOSE_DATE_COUNTDOWN: Final = "close-date-countdown"
WATCHER_SLACK_THREADS: Final = "slack-threads"
WATCHER_BACKSTORY_HEALTH: Final = "backstory-health"
WATCHER_WAITING_ON_TRACKER: Final = "waiting-on-tracker"
WATCHER_DRAFT_QUEUE: Final = "draft-queue"
WATCHER_RUN_ALL: Final = "run-all"

KNOWN_WATCHERS: Final = frozenset(
    {
        WATCHER_MORNING_BRIEF,
        WATCHER_PURSUIT_STALLS,
        WATCHER_ACCOUNT_HEALTH,
        WATCHER_CONTRACT_EXPIRY,
        WATCHER_CLOSE_DATE_COUNTDOWN,
        WATCHER_SLACK_THREADS,
        WATCHER_BACKSTORY_HEALTH,
        WATCHER_WAITING_ON_TRACKER,
        WATCHER_DRAFT_QUEUE,
        WATCHER_RUN_ALL,
    }
)
