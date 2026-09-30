"""Shared ownership boundary for transcript ingestion and reprocessing."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fieldkit.util.atomic import exclusive_path_lock

TRANSCRIPT_RUN_LOCK_TIMEOUT_SECONDS = 0


@contextmanager
def transcript_run_lock(db_path: Path) -> Iterator[None]:
    """Serialize transcript mutations using the canonical database identity."""
    canonical = db_path.resolve()
    target = canonical.with_name(canonical.name + ".transcript-run")
    with exclusive_path_lock(target, timeout_seconds=TRANSCRIPT_RUN_LOCK_TIMEOUT_SECONDS):
        yield
