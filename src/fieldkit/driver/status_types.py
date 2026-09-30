"""Shared persisted driver outcome contract."""

from typing import Literal, get_args

RunOutcome = Literal["ok", "failed", "skipped", "dry-run"]
RUN_OUTCOMES: tuple[str, ...] = get_args(RunOutcome)
