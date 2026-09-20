"""Concurrent persistence helpers for watcher alert-suppression state."""

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fieldkit.util.atomic import locked_json_update


def merge_state(path: Path, updated_state: Mapping[str, Any], previous_state: Mapping[str, Any]) -> None:
    """Persist the delta from *previous_state* to *updated_state* under a file lock.

    Watcher scans happen outside the lock because they can call remote systems.
    Applying only the resulting delta while holding ``locked_json_update`` avoids a
    later scan replacing unrelated keys written by a concurrent scan.
    """
    changed = {key: value for key, value in updated_state.items() if previous_state.get(key) != value}
    removed = previous_state.keys() - updated_state.keys()

    with locked_json_update(path) as persisted:
        for key in removed:
            persisted.pop(key, None)
        persisted.update(changed)


def state_write_failed(*, dry_run: bool, write: Callable[[], None], logger: logging.Logger, message: str) -> bool:
    """Attempt a state write and report whether storage made the run fatal."""
    if dry_run:
        return False
    try:
        write()
    except OSError:
        logger.error(message, exc_info=True)
        return True
    return False
