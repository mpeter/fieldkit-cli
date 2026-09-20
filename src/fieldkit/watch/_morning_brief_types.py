"""Shared sentinel types for the morning brief pipeline.

Moved from ``commands/watch/_morning_brief_types.py`` (watch-domain-migration,
implementation change slice 2.4). No Click or config dependencies — stdlib only.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceNotReady:
    """Returned when an optional watcher source has no data yet.

    This is NOT a failure — the watcher simply hasn't run yet.
    Distinguished from plain str errors so source_failures is not inflated.
    """

    message: str
