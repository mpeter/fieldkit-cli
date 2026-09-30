"""Shared source availability and publication results for the morning brief."""

from dataclasses import dataclass

from fieldkit.watch.status import WatcherRunResult


@dataclass(frozen=True)
class BriefWriteResult:
    """Whether this invocation published a nonempty artifact, and its overall status."""

    written: bool
    run: WatcherRunResult
    records_checked: int
    alerts_generated: int
    failures: int


@dataclass(frozen=True)
class SourceNotReady:
    """Returned when an optional watcher source has no data yet.

    This is NOT a failure — the watcher simply hasn't run yet.
    Distinguished from plain str errors so source_failures is not inflated.
    """

    message: str
