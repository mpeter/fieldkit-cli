"""fieldkit.web.events — Server-Sent Events stream over watcher alerts.

Change detection is a plain mtime snapshot diff, deliberately: it needs
no extra dependency, works on every platform the compose stack targets,
and the alert-file write rate (a handful per day) makes inotify-grade
latency pointless. The poll interval and iteration cap are injectable so
tests can drive the generator deterministically.
"""

import asyncio
import json
from collections.abc import AsyncGenerator

from fieldkit.web.data import DataSource

DEFAULT_POLL_SECONDS = 3.0


def _sse(event: str, data: dict[str, object]) -> str:
    """Format one SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def alert_event_stream(
    source: DataSource,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_polls: int | None = None,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames when watcher alert files appear or change.

    Args:
        source: The data source whose watchers directory is observed.
        poll_seconds: Sleep between snapshot polls.
        max_polls: Stop after this many polls (None = run until the
            client disconnects). Used by tests.

    Yields:
        SSE-formatted strings: an initial ``hello`` event, then one
        ``alert`` event per new/changed alert file, with ``ping``
        heartbeats on quiet polls so proxies keep the connection open.
    """
    snapshot = await asyncio.to_thread(source.alert_mtimes)
    yield _sse("hello", {"alert_files": len(snapshot)})

    polls = 0
    while max_polls is None or polls < max_polls:
        await asyncio.sleep(poll_seconds)
        polls += 1
        current = await asyncio.to_thread(source.alert_mtimes)
        changed = [name for name, mtime in current.items() if snapshot.get(name) != mtime]
        snapshot = current
        if changed:
            for name in sorted(changed):
                yield _sse("alert", {"name": name, "mtime": current[name]})
        else:
            yield ": ping\n\n"
