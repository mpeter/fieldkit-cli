"""Tests for concurrent watcher-state persistence."""

import json
from pathlib import Path

import pytest

from fieldkit.watch.state import merge_state

pytestmark = pytest.mark.unit


def test_merge_state_preserves_updates_from_distinct_stale_snapshots(tmp_path: Path) -> None:
    """Separate scans must not overwrite each other's unrelated state entries."""
    state_file = tmp_path / "watcher-state.json"
    state_file.write_text('{"existing": {"value": 0}}', encoding="utf-8")
    prior_state = {"existing": {"value": 0}}

    first_result = merge_state(state_file, {"existing": {"value": 0}, "alpha": {"value": 1}}, prior_state)
    second_result = merge_state(state_file, {"existing": {"value": 0}, "bravo": {"value": 2}}, prior_state)

    assert first_result is None
    assert second_result is None

    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "alpha": {"value": 1},
        "bravo": {"value": 2},
        "existing": {"value": 0},
    }
