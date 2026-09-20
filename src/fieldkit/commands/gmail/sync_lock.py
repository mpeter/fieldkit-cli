"""Single-writer locking for the Gmail sync command."""

import contextlib
import fcntl
from collections.abc import Iterator
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_PARTIAL


@contextlib.contextmanager
def gmail_sync_lock(db_path: Path) -> Iterator[None]:
    lock_path = db_path.with_suffix(f"{db_path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            click.echo("Gmail sync already running for the selected database.", err=True)
            raise SystemExit(EXIT_PARTIAL) from None
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
