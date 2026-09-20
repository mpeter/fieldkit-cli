"""fieldkit ingest run — execute or dry-run an ingestion pipeline."""

import json
import logging
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click

from fieldkit.cli_registry import declare_write
from fieldkit.commands.ingest._output import BatchOutcomes, human_echo, json_output
from fieldkit.config import ConfigError
from fieldkit.config.optional_dependencies import GOOGLE_IMPORT_ROOTS, require_optional_profile
from fieldkit.ingest.constants import (
    AMBIENT_TRANSCRIPT_PIPELINE,
    GEMINI_TRANSCRIPT_PIPELINE,
    SOURCE_STATUS_FAILED,
    SOURCE_STATUS_PENDING,
    SOURCE_STATUS_PROCESSED,
)
from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.pipeline import TranscriptMeta, primary_account
from fieldkit.ingest.router import RouteResult
from fieldkit.ingest.sources import SourceRecord, insert_vault_note_artifact, mark_source_status
from fieldkit.ingest.writeback import MeetingWriteback, apply_meeting_writebacks

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fieldkit.ingest.ambient_pipeline import AmbientOutcome


@dataclass(frozen=True)
class _CleanResult:
    cleaned: str
    meta: TranscriptMeta
    used_fallback: bool


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
    type=int,
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
        human_echo(
            f"Error: unknown pipeline '{pipeline_id}'. Available pipelines: {available}",
            err=True,
        )
        return 1

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
        human_echo("Error: --interactive is not supported for ambient-transcript-ingest.", err=True)
        return 1

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


_FETCH_DOC_TIMEOUT = 30  # historic regression: cap each Drive API call to 30 seconds


def _fetch_doc_for_run(service: object, source_id: str, conn: sqlite3.Connection) -> GeminiDocContent | None:
    """Fetch a Gemini doc from Drive and persist a recoverable source outcome.

    Returns the doc content object, or None if the source should be skipped.
    Times out after _FETCH_DOC_TIMEOUT seconds (historic regression).
    """
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from fieldkit.ingest.docs import (
        DocAccessDeniedError,
        DocNotFoundError,
    )
    from fieldkit.ingest.docs import (
        fetch_gemini_doc as _fetch,
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_fetch, service, source_id)
            try:
                return future.result(timeout=_FETCH_DOC_TIMEOUT)
            except FuturesTimeoutError:
                human_echo(
                    f"  Error: doc {source_id} timed out after {_FETCH_DOC_TIMEOUT}s; marking failed.",
                    err=True,
                )
                mark_source_status(conn, source_id, SOURCE_STATUS_FAILED)
                return None
    except DocNotFoundError:
        human_echo(f"  Error: doc {source_id} not found (404); marking failed.", err=True)
        mark_source_status(conn, source_id, SOURCE_STATUS_FAILED)
        return None
    except DocAccessDeniedError:
        human_echo(f"  Error: doc {source_id} access denied (403); marking failed.", err=True)
        mark_source_status(conn, source_id, SOURCE_STATUS_FAILED)
        return None
    except Exception as exc:  # noqa: BLE001 — pipeline must not abort on single-item failure
        human_echo(f"  Error fetching {source_id}: {exc}", err=True)
        mark_source_status(conn, source_id, SOURCE_STATUS_PENDING)
        return None


def _route_source(src: SourceRecord, doc_content: GeminiDocContent) -> RouteResult:
    """Return a RouteResult for *src* using domain-then-title routing with pursuit matching.

    Routing priority:
    1. Email domains present -> route_with_pursuits (domain + pursuit keyword matching).
    2. No domains / unknown  -> route_by_title (title keyword matching).
    3. Either path that resolves an account -> match_pursuits_for_account with the
       meeting title as an additional keyword hint, so pursuits are linked even when
       no invitee email addresses appear in the Gemini doc.
    """
    from fieldkit.ingest.router import (
        match_pursuits_for_account,
        route_by_title,
        route_with_pursuits,
    )

    title = src.meeting_title or doc_content.doc_title or ""

    domains = [email.split("@")[-1].lower() for email in doc_content.invited_emails if "@" in email]

    route = route_with_pursuits(domains, keywords=[title] if title else None)

    if route.accounts == ["unknown"] and route.confidence.value == "none":
        route = route_by_title(title)

    # If we resolved an account, run pursuit matching with the title as a keyword hint.
    # route_with_pursuits only fires when confidence is HIGH (requires domain match);
    # match_pursuits_for_account works regardless of how the account was resolved.
    if route.accounts != ["unknown"]:
        account_name = primary_account(route)
        pursuits = match_pursuits_for_account(account_name, keywords=[title] if title else [])
        route = RouteResult(
            accounts=route.accounts,
            confidence=route.confidence,
            is_internal=route.is_internal,
            pursuits=pursuits,
        )

    return route


def _clean_and_extract_transcript(source_id: str, doc_content: GeminiDocContent) -> _CleanResult:
    """Run Stage 1 (clean) and Stage 2 (extract) with graceful degradation.

    Returns (cleaned_text, TranscriptMeta).
    """
    from fieldkit.ingest.pipeline import Stage1Result, stage1_clean, stage2_extract

    raw_text = doc_content.transcript_text or doc_content.notes_text
    stage1_result: Stage1Result | str
    used_fallback = False
    try:
        stage1_result = stage1_clean(raw_text)
    except Exception as exc:  # noqa: BLE001
        human_echo(f"  Warning: stage1_clean failed for {source_id}: {exc}", err=True)
        stage1_result = raw_text  # fallback: plain str (backwards compat path)
        used_fallback = True

    # Extract cleaned text for vault note body (Stage1Result.text or plain str).
    cleaned = stage1_result.text if isinstance(stage1_result, Stage1Result) else stage1_result

    try:
        meta = stage2_extract(stage1_result)
    except Exception as exc:  # noqa: BLE001
        human_echo(f"  Warning: stage2_extract failed for {source_id}: {exc}", err=True)
        meta = TranscriptMeta(confidence="low")
        used_fallback = True

    return _CleanResult(cleaned=cleaned, meta=meta, used_fallback=used_fallback)


def _process_one_source(
    *,
    src: SourceRecord,
    service: object,
    conn: sqlite3.Connection,
    data_root: Path,
    pipeline_version: str,
    file_lock: "threading.Lock | None" = None,
) -> _ProcessResult:
    """Process a single pending source end-to-end.

    file_lock, when provided, serialises writes to shared files (TASKS.md and
    pursuit activity logs) so parallel workers don't corrupt them.

    Returns completion and degradation state.
    """
    _lock = file_lock or threading.Lock()  # fallback for single-threaded callers
    from fieldkit.ingest.pipeline import compute_vault_path, infer_meeting_date, render_vault_note

    source_id: str = src.source_id
    doc_content = _fetch_doc_for_run(service, source_id, conn)
    if doc_content is None:
        return _ProcessResult(completed=False, degraded=False)

    route = _route_source(src, doc_content)
    account = primary_account(route)

    transcript = _clean_and_extract_transcript(source_id, doc_content)
    cleaned = transcript.cleaned
    meta = transcript.meta
    meta.accounts = route.accounts

    # Re-run pursuit matching now that we have LLM-extracted topics and decisions,
    # which are richer signals than the meeting title alone.
    if account != "unknown":
        from fieldkit.ingest.router import match_pursuits_for_account

        topic_keywords = meta.key_topics + meta.key_decisions
        enriched_pursuits = match_pursuits_for_account(account, keywords=topic_keywords)
        # Merge: keep any title-matched pursuits, add topic-matched ones
        merged = list(dict.fromkeys(route.pursuits + enriched_pursuits))
        meta.pursuits = merged
    else:
        meta.pursuits = route.pursuits

    doc_url = f"https://docs.google.com/document/d/{source_id}"
    if src.meeting_date:
        meeting_date_str = src.meeting_date.strftime("%Y-%m-%d")
    else:
        # Try to parse meeting date from the title (Gemini format: "Topic - YYYY/MM/DD HH:MM TZ")
        _title_for_date = src.meeting_title or doc_content.doc_title or ""
        # None means the title carries no date; render_vault_note warns and uses today.
        meeting_date_str = infer_meeting_date(_title_for_date) or datetime.now(UTC).strftime("%Y-%m-%d")

    note_content = render_vault_note(
        doc_content=doc_content,
        route=route,
        meta=meta,
        cleaned_body=cleaned,
        pipeline_version=pipeline_version,
        doc_url=doc_url,
        meeting_date=meeting_date_str,
    )
    vault_path = compute_vault_path(
        data_root=data_root,
        account=account,
        meeting_date=meeting_date_str,
        meeting_title=src.meeting_title or doc_content.doc_title or source_id,
    )

    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_text(note_content, encoding="utf-8")

    meeting_title_str: str = src.meeting_title or doc_content.doc_title or source_id

    # Shared file mutations — serialised with _lock so parallel workers
    # don't interleave writes to pursuit activity logs or TASKS.md.
    with _lock:
        notices = apply_meeting_writebacks(
            MeetingWriteback(
                pursuits=tuple(meta.pursuits),
                action_items=tuple(meta.action_items),
                account=account,
                meeting_date=meeting_date_str,
                meeting_title=meeting_title_str,
                data_root=data_root,
                vault_path=vault_path,
            )
        )
        for notice in notices:
            human_echo(notice.message, err=notice.err)

    mark_source_status(conn, source_id, SOURCE_STATUS_PROCESSED)
    insert_vault_note_artifact(conn, source_id, pipeline_version, str(vault_path))

    human_echo(f"  Wrote: {vault_path} (account: {account})")
    return _ProcessResult(completed=True, degraded=transcript.used_fallback or meta.confidence == "low")


def _run_transcript_ingest(
    *,
    spec: object,
    dry_run: bool,
    limit: int | None,
    interactive: bool,
    as_json: bool = False,
) -> int:
    """Execute the transcript-ingest pipeline.

    Discovery runs first (idempotent), then pending sources are processed.
    Handles --dry-run, --limit, --interactive, and SIGINT gracefully.

    Returns:
        0 on success (including partial completion after SIGINT).
    """
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.gmail.discover import get_gmail_db_path, scan_gemini_candidates
    from fieldkit.ingest.db import get_db_path, init_db
    from fieldkit.ingest.sources import discover_gemini_sources, get_pending_sources

    pipeline_version: str = getattr(spec, "version", "0.1.0")
    db_path = get_db_path()
    conn = init_db(db_path, pipelines=PIPELINES)
    try:
        # Discovery (idempotent)
        try:
            gmail_db_path = get_gmail_db_path()
            candidates = scan_gemini_candidates(gmail_db_path, limit=None)
            new_sources = discover_gemini_sources(conn, candidates)
            if new_sources:
                human_echo(f"Discovered {len(new_sources)} new source(s).")
        except (FileNotFoundError, ConfigError) as exc:
            # gmail.db absent — non-fatal warning; ingest continues with sources
            # already registered in pipeline.db. ConfigError raised by implementation note path;
            # FileNotFoundError retained for backward compatibility.
            human_echo(f"Warning: {exc}", err=True)

        pending = get_pending_sources(conn, GEMINI_TRANSCRIPT_PIPELINE, limit=limit)
        if not pending:
            human_echo("No pending sources to process.")
            # historic regression: when dry_run, hint that unregistered files may exist on disk
            if dry_run:
                human_echo("NOTE: Unregistered files may exist — run 'fieldkit ingest backfill' to check.")
            if as_json:
                BatchOutcomes().emit(pipeline=GEMINI_TRANSCRIPT_PIPELINE, dry_run=dry_run)
            return 0

        if dry_run:
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
    service: object,
    conn: sqlite3.Connection,
    data_root: Path,
    pipeline_version: str,
) -> tuple[int, int, int, int]:
    """Process pending sources interactively (single-threaded).

    Returns (n_processed, n_degraded, n_skipped, n_errors).
    """
    n_processed = n_degraded = n_skipped = n_errors = 0
    try:
        for src in pending:
            date_str = src.meeting_date.strftime("%Y-%m-%d") if src.meeting_date else "unknown date"
            title = src.meeting_title or src.source_id
            choice = _prompt_process_choice(date_str, title, src.source_id)
            if choice == "q":
                human_echo("Stopping. Pending sources remain in pipeline.db for resume.")
                break
            if choice != "y":
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
    from fieldkit.ingest.docs import get_docs_service

    workers = _dynamic_worker_count(len(pending))
    if workers > 1:
        human_echo(f"Using {workers} workers for {len(pending)} pending sources (target ≤{_TARGET_MINUTES} min).")

    file_lock = threading.Lock()

    def _worker(src: SourceRecord) -> tuple[str, str]:
        """Process one source in a worker thread. Returns (source_id, ok)."""
        from fieldkit.ingest.sources import claim_pending_source as _claim

        date_str = src.meeting_date.strftime("%Y-%m-%d") if src.meeting_date else "unknown date"
        title = src.meeting_title or src.source_id
        worker_service = get_docs_service()
        worker_conn = get_db(db_path)
        try:
            # implementation note: atomically claim the source before processing to prevent
            # two concurrent workers from both processing the same source.
            if not _claim(worker_conn, src.source_id):
                return src.source_id, "skipped"
            human_echo(f"Processing [{date_str}] {title} ({src.source_id}) …")
            result = _process_one_source(
                src=src,
                service=worker_service,
                conn=worker_conn,
                data_root=data_root,
                pipeline_version=pipeline_version,
                file_lock=file_lock,
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
                except Exception as exc:  # noqa: BLE001
                    src = futures[fut]
                    statuses[src.source_id] = "failed"
                    human_echo(f"  Error processing {src.source_id}: {exc}", err=True)
                    n_errors += 1
    except KeyboardInterrupt:
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
    activity logs) are serialised with a threading.Lock.

    Returns 0 on success (including partial completion after Ctrl-C).
    """
    from fieldkit.config import get_fieldkit_home as _get_fieldkit_home
    from fieldkit.ingest.db import get_db_path

    require_optional_profile("ingest run --pipeline transcript-ingest", "google", GOOGLE_IMPORT_ROOTS)
    from fieldkit.ingest.docs import get_docs_service

    data_root = _get_fieldkit_home()
    db_path = get_db_path()

    # Validate Google auth before spawning workers
    try:
        _probe_service = get_docs_service()
    except FileNotFoundError as exc:
        human_echo(f"Error: {exc}", err=True)
        human_echo("Run the Google Workspace auth flow to generate the OAuth token.", err=True)
        return 1

    outcomes = BatchOutcomes()
    if interactive:
        n_processed, n_degraded, n_skipped, n_errors = _run_interactive_loop(
            pending,
            service=_probe_service,
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
    return 1 if as_json and n_errors else 0
