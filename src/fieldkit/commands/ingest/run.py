"""fieldkit ingest run — execute or dry-run an ingestion pipeline."""

import json
import logging
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, closing
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import click

from fieldkit.cli_registry import declare_write
from fieldkit.commands.ingest._output import BatchOutcomes, human_echo, json_output
from fieldkit.config import ConfigError
from fieldkit.config.optional_dependencies import GOOGLE_IMPORT_ROOTS, require_optional_profile
from fieldkit.errors import AuthError, LLMError, MissingOptionalDependencyError, RoutingReadRetryableError
from fieldkit.ingest.constants import (
    AMBIENT_TRANSCRIPT_PIPELINE,
    GEMINI_TRANSCRIPT_PIPELINE,
    SOURCE_STATUS_FAILED,
    SOURCE_STATUS_PENDING,
)
from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.preparation import prepare_meeting
from fieldkit.ingest.prepared import PreparedMeeting, load_prepared, save_prepared
from fieldkit.ingest.replay import recover_interrupted_sources, replay_prepared
from fieldkit.ingest.run_lock import transcript_run_lock
from fieldkit.ingest.sources import SourceRecord, mark_source_status
from fieldkit.util.atomic import PathLockTimeoutError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fieldkit.gmail.discover import GmailCandidate
    from fieldkit.ingest.ambient_pipeline import AmbientOutcome


@dataclass(frozen=True)
class _ProcessResult:
    completed: bool
    degraded: bool


# ---------------------------------------------------------------------------
# Concurrency constants
# ---------------------------------------------------------------------------

# Maximum workers: keeps Vertex AI LLM calls well under the 60 RPM limit.
# Each transcript makes 2 sequential LLM calls; 8 workers = 16 concurrent
# calls in flight at peak, leaving headroom for retries and bursts.
_MAX_WORKERS = 8

# Target wall-clock minutes for a full queue run (used to auto-size workers).
_TARGET_MINUTES = 15

# Estimated minutes per transcript (two LLM calls, Drive fetch, file I/O).
_MINUTES_PER_TRANSCRIPT = 3.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@declare_write("workspace")
@click.command("run")
@click.option(
    "--pipeline",
    required=True,
    metavar="PIPELINE_ID",
    help="Pipeline ID to run (e.g. transcript-ingest).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview what would run without writing anything.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Maximum number of sources to process.",
)
@click.option(
    "--interactive",
    is_flag=True,
    default=False,
    help="Prompt y/n/q for each source before processing.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit ordered batch outcomes as JSON.")
def cli(pipeline: str, dry_run: bool, limit: int | None, interactive: bool, as_json: bool) -> None:
    """Execute an ingestion pipeline."""
    if as_json and interactive:
        raise click.UsageError("--json cannot be combined with --interactive")
    with json_output(as_json):
        rc = _run_run(pipeline_id=pipeline, dry_run=dry_run, limit=limit, interactive=interactive, as_json=as_json)
    if rc:
        raise SystemExit(rc)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _run_run(
    *,
    pipeline_id: str,
    dry_run: bool,
    limit: int | None,
    interactive: bool,
    as_json: bool = False,
) -> int:
    """Execute or dry-run an ingestion pipeline. Returns exit code."""
    from fieldkit.commands.ingest.registry import PIPELINE_MAP

    # --- pipeline validation ---
    if pipeline_id not in PIPELINE_MAP:
        available = ", ".join(sorted(PIPELINE_MAP))
        raise click.UsageError(f"Unknown pipeline. Available pipelines: {available}")

    spec = PIPELINE_MAP[pipeline_id]

    # --- stub pipeline path ---
    if spec.status == "stub":
        if dry_run:
            human_echo(f"Pipeline '{pipeline_id}' is a stub (status: stub). No sources available to process.")
        else:
            human_echo(f"Pipeline '{pipeline_id}' is a stub and cannot be executed. Use --dry-run to preview.")
        if as_json:
            BatchOutcomes().emit(pipeline=pipeline_id, dry_run=dry_run)
        return 0

    handlers = {
        GEMINI_TRANSCRIPT_PIPELINE: _run_transcript_ingest,
        AMBIENT_TRANSCRIPT_PIPELINE: _run_ambient_transcript_ingest,
    }
    handler = handlers.get(pipeline_id, _report_unimplemented_pipeline)
    return handler(spec=spec, dry_run=dry_run, limit=limit, interactive=interactive, as_json=as_json)


def _report_unimplemented_pipeline(
    *, spec: object, dry_run: bool, limit: int | None, interactive: bool, as_json: bool
) -> int:
    del dry_run, limit, interactive, as_json
    pipeline_id = str(getattr(spec, "pipeline_id", "unknown"))
    human_echo(f"Pipeline '{pipeline_id}' run not yet implemented. Use --dry-run to preview.")
    return 0


def _run_ambient_transcript_ingest(
    *,
    spec: object,
    dry_run: bool,
    limit: int | None,
    interactive: bool,
    as_json: bool,
) -> int:
    """Process registered ambient sources in discovery order."""
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.config import get_fieldkit_home
    from fieldkit.ingest.ambient_pipeline import process_ambient_source
    from fieldkit.ingest.db import get_db_path, init_db

    if interactive:
        raise click.UsageError("--interactive is not supported for ambient-transcript-ingest")

    conn = init_db(get_db_path(), pipelines=PIPELINES)
    try:
        source_ids = _ambient_pending_ids(conn, limit)

        if dry_run:
            human_echo(f"Dry run: {len(source_ids)} pending ambient source(s).")
            if as_json:
                BatchOutcomes(pending=source_ids).emit(pipeline=AMBIENT_TRANSCRIPT_PIPELINE, dry_run=True)
            return 0

        outcomes = BatchOutcomes()
        for source_id in source_ids:
            result = process_ambient_source(
                conn,
                source_id=source_id,
                fieldkit_home=get_fieldkit_home(),
                pipeline_version=str(getattr(spec, "version", "0.1.0")),
            )
            getattr(outcomes, result.status if result.status != "processed" else "completed").append(source_id)
            _emit_ambient_outcome(result, as_json=as_json)

        if as_json:
            _emit_ambient_json(outcomes)
        return 1 if outcomes.failed or outcomes.deferred else 0
    finally:
        conn.close()


def _ambient_pending_ids(conn: sqlite3.Connection, limit: int | None) -> list[str]:
    query = (
        "SELECT source_id FROM sources "
        "WHERE pipeline_id = ? AND status = 'pending' ORDER BY discovered_at ASC, source_id ASC"
    )
    params: list[object] = [AMBIENT_TRANSCRIPT_PIPELINE]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return [str(row["source_id"]) for row in conn.execute(query, params).fetchall()]


def _emit_ambient_outcome(result: "AmbientOutcome", *, as_json: bool) -> None:
    if as_json:
        return
    detail = f": {result.reason}" if result.reason else ""
    human_echo(f"{result.status.capitalize()} {result.source_id}{detail}.", err=result.status in {"deferred", "failed"})


def _emit_ambient_json(outcomes: BatchOutcomes) -> None:
    click.echo(
        json.dumps(
            {
                "deferred": outcomes.deferred,
                "dry_run": False,
                "failed": outcomes.failed,
                "pending": outcomes.pending,
                "pipeline": AMBIENT_TRANSCRIPT_PIPELINE,
                "processed": outcomes.completed,
                "skipped": outcomes.skipped,
            },
            sort_keys=True,
        )
    )


# ---------------------------------------------------------------------------
# transcript-ingest implementation
# ---------------------------------------------------------------------------


def _prompt_process_choice(date_str: str, title: str, source_id: str) -> str:
    """Prompt the user whether to process this source.

    Returns 'y', 'n', or 'q'.  Returns 'q' on EOFError (non-interactive stdin).
    """
    human_echo(f"\n[{date_str}] {title} ({source_id})")
    try:
        return input("Process? [y/n/q] ").strip().lower()
    except EOFError:
        return "q"


def _fetch_doc_for_run(service: object, source_id: str, conn: sqlite3.Connection) -> GeminiDocContent | None:
    """Fetch a Gemini doc from Drive and persist a recoverable source outcome.

    Returns the doc content object, or None if the source should be skipped.
    The service owns its transport timeout; no background fetch outlives this call.
    """
    from fieldkit.ingest.docs import (
        DocAccessDeniedError,
        DocNotFoundError,
    )
    from fieldkit.ingest.docs import (
        fetch_gemini_doc as _fetch,
    )

    try:
        return _fetch(service, source_id)
    except AuthError:
        raise
    except DocNotFoundError:
        human_echo(f"  Error: doc {source_id} not found (404); marking failed.", err=True)
        mark_source_status(conn, source_id, SOURCE_STATUS_FAILED)
        return None
    except DocAccessDeniedError:
        human_echo(f"  Error: doc {source_id} access denied (403); marking failed.", err=True)
        mark_source_status(conn, source_id, SOURCE_STATUS_FAILED)
        return None
    except Exception:  # noqa: BLE001 — retain retryable state without exposing provider payloads
        human_echo("Document fetch failed; check connectivity and retry. Source remains pending.", err=True)
        mark_source_status(conn, source_id, SOURCE_STATUS_PENDING)
        return None


def _replay_source(conn: sqlite3.Connection, prepared: PreparedMeeting, data_root: Path) -> _ProcessResult:
    """Use one payload-free failure boundary for initial and resumed replay."""
    try:
        path = replay_prepared(conn, prepared.source_id, data_root)
    except AuthError:
        raise
    except Exception:  # noqa: BLE001
        human_echo(
            "Required meeting writeback failed; check note metadata and pursuit/task storage. Source not completed.",
            err=True,
        )
        return _ProcessResult(completed=False, degraded=False)
    human_echo(f"  Wrote: {path} (account: {prepared.account})")
    return _ProcessResult(completed=True, degraded=prepared.degraded)


def _process_one_source(
    *,
    src: SourceRecord,
    service: object | None,
    conn: sqlite3.Connection,
    data_root: Path,
    pipeline_version: str,
) -> _ProcessResult:
    """Process a single pending source end-to-end.

    Effect publishers serialize shared file updates with target-specific locks.

    Returns completion and degradation state.
    """
    source_id: str = src.source_id
    prepared = load_prepared(conn, source_id)
    if prepared is not None:
        return _replay_source(conn, prepared, data_root)
    with ExitStack() as provider_stack:
        if service is None:
            require_optional_profile("ingest run --pipeline transcript-ingest", "google", GOOGLE_IMPORT_ROOTS)
            from fieldkit.ingest.docs import get_docs_service

            service = provider_stack.enter_context(closing(get_docs_service()))
        doc_content = _fetch_doc_for_run(service, source_id, conn)
    if doc_content is None:
        return _ProcessResult(completed=False, degraded=False)

    try:
        prepared = prepare_meeting(
            src=src,
            doc_content=doc_content,
            data_root=data_root,
            pipeline_version=pipeline_version,
            report_warning=partial(human_echo, err=True),
        )
    except ConfigError:
        mark_source_status(conn, source_id, SOURCE_STATUS_FAILED)
        raise
    except RoutingReadRetryableError:
        mark_source_status(conn, source_id, SOURCE_STATUS_PENDING)
        raise
    save_prepared(conn, prepared)
    return _replay_source(conn, prepared, data_root)


def _run_transcript_ingest(
    *,
    spec: object,
    dry_run: bool,
    limit: int | None,
    interactive: bool,
    as_json: bool = False,
) -> int:
    """Own the canonical database's transcript run lock through worker shutdown."""
    from fieldkit.ingest.db import get_db_path

    db_path = get_db_path().resolve()
    if dry_run:
        return _preview_transcript_ingest(db_path=db_path, limit=limit, as_json=as_json)
    with ExitStack() as stack:
        try:
            stack.enter_context(transcript_run_lock(db_path))
        except PathLockTimeoutError:
            human_echo("Transcript ingest is already running; retry after it finishes.", err=True)
            if as_json:
                click.echo(json.dumps({"pipeline": GEMINI_TRANSCRIPT_PIPELINE, "error": "ingest_busy"}))
            return 1
        return _run_locked_transcript_ingest(
            db_path=db_path,
            spec=spec,
            limit=limit,
            interactive=interactive,
            as_json=as_json,
        )


def _preview_record(candidate: "GmailCandidate", *, discovered_at: str) -> SourceRecord:
    """Return the in-memory source record a live discovery would register."""
    return SourceRecord(
        source_id=candidate.source_id,
        pipeline_id=GEMINI_TRANSCRIPT_PIPELINE,
        subject=candidate.subject,
        meeting_title=candidate.meeting_title,
        meeting_date=candidate.meeting_date,
        doc_url=candidate.doc_url,
        email_message_id=candidate.email_message_id,
        discovered_at=discovered_at,
    )


def _emit_transcript_preview(pending: list[SourceRecord], *, limit: int | None, as_json: bool) -> int:
    """Render one already-bounded, read-only transcript preview."""
    if not pending:
        human_echo("No pending sources to process.")
        human_echo("NOTE: Unregistered files may exist — run 'fieldkit ingest backfill' to check.")
        if as_json:
            BatchOutcomes().emit(pipeline=GEMINI_TRANSCRIPT_PIPELINE, dry_run=True)
        return 0

    limit_str = f"up to {limit}" if limit is not None else "all"
    human_echo(
        f"Dry run: pipeline={GEMINI_TRANSCRIPT_PIPELINE}, {len(pending)} pending source(s) ({limit_str} requested):"
    )
    for src in pending:
        date_str = src.meeting_date.strftime("%Y-%m-%d") if src.meeting_date else "unknown date"
        title = src.meeting_title or src.source_id
        human_echo(f"  [{date_str}] {title} ({src.source_id})")
    if as_json:
        BatchOutcomes(pending=[src.source_id for src in pending]).emit(
            pipeline=GEMINI_TRANSCRIPT_PIPELINE, dry_run=True
        )
    return 0


def _preview_transcript_ingest(*, db_path: Path, limit: int | None, as_json: bool) -> int:
    """Preview registered and newly discovered sources without filesystem writes."""
    from fieldkit.gmail.discover import get_gmail_db_path, scan_gemini_candidates
    from fieldkit.ingest.db import get_db_read_only
    from fieldkit.ingest.sources import get_pending_sources

    candidates = scan_gemini_candidates(get_gmail_db_path(), limit=None)
    registered_ids: set[str] = set()
    pending: list[SourceRecord] = []
    try:
        conn = get_db_read_only(db_path)
    except FileNotFoundError:
        conn = None
    if conn is not None:
        try:
            registered_ids = {
                str(row["source_id"])
                for row in conn.execute(
                    "SELECT source_id FROM sources WHERE pipeline_id = ?",
                    (GEMINI_TRANSCRIPT_PIPELINE,),
                ).fetchall()
            }
            pending = get_pending_sources(conn, GEMINI_TRANSCRIPT_PIPELINE)
        except sqlite3.DatabaseError:
            from fieldkit.errors import SQLiteSnapshotError

            raise SQLiteSnapshotError("pipeline database is unverified", reason="unverified") from None
        finally:
            conn.close()

    new_candidates: dict[str, GmailCandidate] = {}
    for candidate in candidates:
        if candidate.source_id not in registered_ids:
            new_candidates.setdefault(candidate.source_id, candidate)
    discovered_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pending.extend(
        _preview_record(new_candidates[source_id], discovered_at=discovered_at) for source_id in sorted(new_candidates)
    )
    pending.sort(key=lambda source: (source.discovered_at, source.source_id))
    if limit is not None:
        pending = pending[:limit]
    return _emit_transcript_preview(pending, limit=limit, as_json=as_json)


def _run_locked_transcript_ingest(
    *,
    db_path: Path,
    spec: object,
    limit: int | None,
    interactive: bool,
    as_json: bool,
) -> int:
    """Execute the transcript-ingest pipeline.

    Discovery runs first (idempotent), then pending sources are processed.
    Handles --dry-run, --limit, --interactive, and SIGINT gracefully.

    Returns:
        1 when setup or source processing fails; otherwise 0 (including interruption without errors).
    """
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.gmail.discover import get_gmail_db_path, scan_gemini_candidates
    from fieldkit.ingest.db import init_db
    from fieldkit.ingest.sources import discover_gemini_sources, get_pending_sources

    pipeline_version: str = getattr(spec, "version", "0.1.0")
    candidates = scan_gemini_candidates(get_gmail_db_path(), limit=None)
    conn = init_db(db_path, pipelines=PIPELINES)
    try:
        recover_interrupted_sources(conn)
        new_sources = discover_gemini_sources(conn, candidates)
        if new_sources:
            human_echo(f"Discovered {len(new_sources)} new source(s).")

        pending = get_pending_sources(conn, GEMINI_TRANSCRIPT_PIPELINE, limit=limit)
        if not pending:
            human_echo("No pending sources to process.")
            if as_json:
                BatchOutcomes().emit(pipeline=GEMINI_TRANSCRIPT_PIPELINE, dry_run=False)
            return 0

        return _run_processing_loop(
            pending, conn=conn, pipeline_version=pipeline_version, interactive=interactive, as_json=as_json
        )
    finally:
        # historic regression: always close the main orchestrating connection
        conn.close()


def _dynamic_worker_count(queue_size: int) -> int:
    """Compute worker count based on queue depth and target wall-clock time.

    Targets _TARGET_MINUTES total run time given _MINUTES_PER_TRANSCRIPT per
    item, capped at _MAX_WORKERS to stay under Vertex AI rate limits.

    Examples (with defaults TARGET=15, PER=3, MAX=8):
      1-5   items →  1 worker  (already fast)
      6-10  items →  2 workers
      11-15 items →  3 workers
      26-30 items →  6 workers
      40+   items →  8 workers (cap)
    """
    needed = math.ceil(queue_size * _MINUTES_PER_TRANSCRIPT / _TARGET_MINUTES)
    return max(1, min(_MAX_WORKERS, needed))


def _run_interactive_loop(
    pending: list[SourceRecord],
    *,
    service: object | None,
    conn: sqlite3.Connection,
    data_root: Path,
    pipeline_version: str,
) -> tuple[int, int, int, int]:
    """Process pending sources interactively (single-threaded).

    Returns (n_processed, n_degraded, n_skipped, n_errors).
    """
    from fieldkit.ingest.sources import claim_pending_source

    n_processed = n_degraded = n_skipped = n_errors = 0
    try:
        for src in pending:
            date_str = src.meeting_date.strftime("%Y-%m-%d") if src.meeting_date else "unknown date"
            title = src.meeting_title or src.source_id
            choice = _prompt_process_choice(date_str, title, src.source_id)
            if choice == "q":
                human_echo("Stopping. Pending sources remain in pipeline.db for resume.")
                break
            if choice == "y" and load_prepared(conn, src.source_id) is None:
                require_optional_profile("ingest run --pipeline transcript-ingest", "google", GOOGLE_IMPORT_ROOTS)
            if choice != "y" or not claim_pending_source(conn, src.source_id):
                human_echo(f"  Skipped: {src.source_id}")
                n_skipped += 1
                continue
            human_echo(f"Processing [{date_str}] {title} ({src.source_id}) …")
            result = _process_one_source(
                src=src,
                service=service,
                conn=conn,
                data_root=data_root,
                pipeline_version=pipeline_version,
            )
            if result.completed:
                n_processed += 1
                n_degraded += int(result.degraded)
            else:
                n_errors += 1
    except KeyboardInterrupt:
        n_errors += 1
        human_echo("\nInterrupted.", err=True)
    return n_processed, n_degraded, n_skipped, n_errors


def _run_parallel_loop(
    pending: list[SourceRecord],
    *,
    data_root: Path,
    db_path: Path | None,
    pipeline_version: str,
    outcomes: BatchOutcomes | None = None,
) -> tuple[int, int, int, int]:
    """Process pending sources in parallel using a thread pool.

    Returns (n_processed, n_degraded, n_skipped, n_errors).
    """
    from fieldkit.ingest.db import get_db

    workers = _dynamic_worker_count(len(pending))
    if workers > 1:
        human_echo(f"Using {workers} workers for {len(pending)} pending sources (target ≤{_TARGET_MINUTES} min).")

    def _worker(src: SourceRecord) -> tuple[str, str]:
        """Process one source in a worker thread. Returns (source_id, ok)."""
        from fieldkit.ingest.sources import claim_pending_source as _claim

        date_str = src.meeting_date.strftime("%Y-%m-%d") if src.meeting_date else "unknown date"
        title = src.meeting_title or src.source_id
        worker_conn = get_db(db_path)
        try:
            if load_prepared(worker_conn, src.source_id) is None:
                require_optional_profile("ingest run --pipeline transcript-ingest", "google", GOOGLE_IMPORT_ROOTS)
            # implementation note: atomically claim the source before processing to prevent
            # two concurrent workers from both processing the same source.
            if not _claim(worker_conn, src.source_id):
                return src.source_id, "skipped"
            human_echo(f"Processing [{date_str}] {title} ({src.source_id}) …")
            result = _process_one_source(
                src=src,
                service=None,
                conn=worker_conn,
                data_root=data_root,
                pipeline_version=pipeline_version,
            )
        finally:
            worker_conn.close()
        if not result.completed:
            return src.source_id, "failed"
        return src.source_id, "degraded" if result.degraded else "completed"

    n_processed = n_degraded = n_errors = 0
    statuses: dict[str, str] = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(copy_context().run, _worker, src): src for src in pending}
            for fut in as_completed(futures):
                try:
                    source_id, status = fut.result()
                    statuses[source_id] = status
                    if status in {"completed", "degraded"}:
                        n_processed += 1
                        n_degraded += int(status == "degraded")
                    elif status == "failed":
                        n_errors += 1
                except (AuthError, ConfigError, LLMError, MissingOptionalDependencyError):
                    raise
                except RoutingReadRetryableError:
                    src = futures[fut]
                    statuses[src.source_id] = "pending"
                    human_echo("Routing inputs could not be inspected; source remains pending for retry.", err=True)
                    n_errors += 1
                except Exception:  # noqa: BLE001
                    src = futures[fut]
                    statuses[src.source_id] = "failed"
                    human_echo(
                        "Source processing failed; check source metadata and output storage. Source not completed.",
                        err=True,
                    )
                    n_errors += 1
    except KeyboardInterrupt:
        n_errors += 1
        human_echo("\nInterrupted. Pending sources remain in pipeline.db for resume.", err=True)

    n_skipped = sum(status == "skipped" for status in statuses.values())
    if outcomes is not None:
        for src in pending:
            ordered_status = statuses.get(src.source_id)
            if ordered_status in {"completed", "degraded"}:
                outcomes.completed.append(src.source_id)
                if ordered_status == "degraded":
                    outcomes.degraded.append(src.source_id)
            elif ordered_status == "skipped":
                outcomes.skipped.append(src.source_id)
            elif ordered_status == "failed":
                outcomes.failed.append(src.source_id)
            else:
                outcomes.pending.append(src.source_id)
    return n_processed, n_degraded, n_skipped, n_errors


def _run_processing_loop(
    pending: list[SourceRecord],
    *,
    conn: sqlite3.Connection,
    pipeline_version: str,
    interactive: bool,
    as_json: bool = False,
) -> int:
    """Process pending sources, using a thread pool for parallelism.

    Interactive mode falls back to single-threaded processing (prompts require
    synchronous input).  Parallel mode gives each worker its own DB connection
    and Google Docs service instance; shared file mutations (TASKS.md, pursuit
    activity logs) are serialized by their target-specific effect publishers.

    Returns 1 when setup or source processing fails, regardless of output format;
    otherwise 0.
    """
    from fieldkit.config import get_fieldkit_home as _get_fieldkit_home
    from fieldkit.ingest.db import get_db_path

    data_root = _get_fieldkit_home()
    db_path = get_db_path()

    outcomes = BatchOutcomes()
    if interactive:
        n_processed, n_degraded, n_skipped, n_errors = _run_interactive_loop(
            pending,
            service=None,
            conn=conn,
            data_root=data_root,
            pipeline_version=pipeline_version,
        )
    else:
        n_processed, n_degraded, n_skipped, n_errors = _run_parallel_loop(
            pending,
            data_root=data_root,
            db_path=db_path,
            pipeline_version=pipeline_version,
            outcomes=outcomes,
        )

    human_echo(f"\nSummary: {n_processed} processed ({n_degraded} degraded), {n_skipped} skipped, {n_errors} error(s).")
    if as_json:
        outcomes.emit(pipeline=GEMINI_TRANSCRIPT_PIPELINE, dry_run=False, include_degraded=True)
    return 1 if n_errors else 0
