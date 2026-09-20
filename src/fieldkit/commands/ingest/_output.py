"""Structured batch results and output routing for ingest commands."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import click

_JSON_OUTPUT = ContextVar("ingest_json_output", default=False)


@dataclass
class BatchOutcomes:
    """Ordered item outcomes for an ingest batch."""

    completed: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    def emit(
        self,
        *,
        pipeline: str,
        dry_run: bool,
        force: bool | None = None,
        include_degraded: bool = False,
        include_deferred: bool = False,
    ) -> None:
        payload: dict[str, object] = {
            "completed": self.completed,
            "dry_run": dry_run,
            "failed": self.failed,
            "pending": self.pending,
            "pipeline": pipeline,
            "skipped": self.skipped,
        }
        if force is not None:
            payload["force"] = force
        if include_degraded:
            payload["degraded"] = self.degraded
        if include_deferred:
            payload["deferred"] = self.deferred
        click.echo(json.dumps(payload, sort_keys=True))


def human_echo(message: object = None, *, err: bool = False, **kwargs: Any) -> None:
    """Render human stdout outside JSON mode; retain errors on stderr."""
    if err or not _JSON_OUTPUT.get():
        click.echo(message, err=err, **kwargs)


@contextmanager
def json_output(enabled: bool) -> Iterator[None]:
    token = _JSON_OUTPUT.set(enabled)
    try:
        yield
    finally:
        _JSON_OUTPUT.reset(token)
