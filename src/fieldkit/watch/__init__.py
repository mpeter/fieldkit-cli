"""fieldkit.watch — Watcher infrastructure: status, logging, and deduplication."""

from fieldkit.watch.dedup import (
    alert_block_exists as alert_block_exists,
)
from fieldkit.watch.logging import (
    list_recent_logs as list_recent_logs,
)
from fieldkit.watch.logging import (
    setup_watcher_logging as setup_watcher_logging,
)
from fieldkit.watch.logging import (
    teardown_watcher_logging as teardown_watcher_logging,
)
from fieldkit.watch.logging import (
    watcher_logging as watcher_logging,
)
from fieldkit.watch.status import (
    WatcherOutcome as WatcherOutcome,
)
from fieldkit.watch.status import (
    was_run_today as was_run_today,
)
from fieldkit.watch.status import (
    write_run_status as write_run_status,
)

__all__ = [
    "WatcherOutcome",
    "alert_block_exists",
    "list_recent_logs",
    "setup_watcher_logging",
    "teardown_watcher_logging",
    "was_run_today",
    "watcher_logging",
    "write_run_status",
]
