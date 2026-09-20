"""Output routing for the skill installer."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal, TypedDict

import click

_JSON_OUTPUT = ContextVar("skill_install_json_output", default=False)


class InstallItemOutcome(TypedDict):
    """Machine-readable result for one tool and skill installation pair."""

    tool: str
    skill: str
    status: Literal["completed", "skipped", "failed", "pending"]


def human_echo(message: object = None, *, err: bool = False, force: bool = False, **kwargs: Any) -> None:
    """Render human output unless JSON mode is active; errors remain on stderr."""
    if force or err or not _JSON_OUTPUT.get():
        click.echo(message, err=err, **kwargs)


def json_enabled() -> bool:
    """Return whether the current command is emitting JSON output."""
    return _JSON_OUTPUT.get()


@contextmanager
def json_output(enabled: bool) -> Iterator[None]:
    """Suppress human stdout while an install command produces its JSON result."""
    token = _JSON_OUTPUT.set(enabled)
    try:
        yield
    finally:
        _JSON_OUTPUT.reset(token)
