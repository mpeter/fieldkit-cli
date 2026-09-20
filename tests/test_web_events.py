"""Focused concurrency contracts for the web alert event stream."""

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from fieldkit.web.data import DataSource
from fieldkit.web.events import alert_event_stream

pytestmark = pytest.mark.unit

_BLOCK_SECONDS = 0.5
_RESPONSIVE_SECONDS = 0.2


@pytest.fixture
def source(tmp_path: Path) -> DataSource:
    watchers = tmp_path / "watchers"
    watchers.mkdir()
    return DataSource(briefs_dir=tmp_path / "briefs", watchers_dir=watchers)


async def _assert_loop_responsive_while_snapshot_waits(
    next_frame: asyncio.Future[str],
    release_snapshot: threading.Event,
) -> str:
    started = time.monotonic()
    try:
        await asyncio.sleep(0.01)
        elapsed = time.monotonic() - started
        assert elapsed < _RESPONSIVE_SECONDS
        assert not next_frame.done()
    finally:
        release_snapshot.set()
    return await asyncio.wait_for(next_frame, timeout=1.0)


def _blocking_snapshot(release_snapshot: threading.Event) -> Callable[[], dict[str, float]]:
    def snapshot() -> dict[str, float]:
        release_snapshot.wait(timeout=_BLOCK_SECONDS)
        return {}

    return snapshot


def test_alert_event_stream_leaves_loop_responsive_during_initial_snapshot(
    source: DataSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> str:
        release_snapshot = threading.Event()
        monkeypatch.setattr(source, "alert_mtimes", _blocking_snapshot(release_snapshot))
        stream = alert_event_stream(source, max_polls=0)
        next_frame = asyncio.ensure_future(anext(stream))
        try:
            return await _assert_loop_responsive_while_snapshot_waits(next_frame, release_snapshot)
        finally:
            await stream.aclose()

    frame = asyncio.run(run())

    assert json.loads(frame.split("data: ", 1)[1]) == {"alert_files": 0}


def test_alert_event_stream_leaves_loop_responsive_during_poll_snapshot(
    source: DataSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> tuple[str, str]:
        release_snapshot = threading.Event()
        calls = 0

        def snapshot() -> dict[str, float]:
            nonlocal calls
            calls += 1
            if calls == 2:
                release_snapshot.wait(timeout=_BLOCK_SECONDS)
            return {}

        monkeypatch.setattr(source, "alert_mtimes", snapshot)
        stream = alert_event_stream(source, poll_seconds=0, max_polls=1)
        hello = await anext(stream)
        next_frame = asyncio.ensure_future(anext(stream))
        try:
            poll = await _assert_loop_responsive_while_snapshot_waits(next_frame, release_snapshot)
            return hello, poll
        finally:
            await stream.aclose()

    hello, poll = asyncio.run(run())

    assert json.loads(hello.split("data: ", 1)[1]) == {"alert_files": 0}
    assert poll == ": ping\n\n"
