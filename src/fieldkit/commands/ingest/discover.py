"""fieldkit ingest discover — scan Gmail for Gemini sources and register them.

For the ``transcript-ingest`` pipeline, this command connects to gmail.db,
extracts Google Doc IDs from Gemini meeting-notes emails, and registers each
Doc as a pending source in pipeline.db.

Dry-run mode reports what *would* be registered without writing to pipeline.db.
"""

import json
from pathlib import Path

import click

from fieldkit.cli_registry import declare_write

# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@declare_write("workspace")
@click.command("discover")
@click.option(
    "--pipeline",
    required=False,
    default="transcript-ingest",
    show_default=True,
    metavar="PIPELINE_ID",
    help="Pipeline ID to inspect.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview what would be registered without modifying pipeline.db.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    metavar="N",
    help="Maximum number of Gmail messages to scan (most-recent first).",
)
@click.option(
    "--include-latest",
    is_flag=True,
    default=False,
    help="Include the newest ambient session, for use after the recorder has stopped.",
)
@click.option("--account", "-a", default=None, help="Limit discovery to a single account slug.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the discovery result as JSON.")
def cli(
    pipeline: str,
    dry_run: bool,
    limit: int | None,
    include_latest: bool,
    account: str | None,
    as_json: bool,
) -> None:
    """Discover pipeline sources and register them."""
    from fieldkit.commands._account_guard import validate_account_slug

    validate_account_slug(account)
    rc = _run_discover(
        pipeline_id=pipeline,
        dry_run=dry_run,
        limit=limit,
        include_latest=include_latest,
        account=account,
        as_json=as_json,
    )
    if rc:
        raise SystemExit(rc)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _run_discover(
    *,
    pipeline_id: str,
    dry_run: bool,
    limit: int | None,
    include_latest: bool = False,
    account: str | None = None,
    as_json: bool = False,
) -> int:
    """Register new Gemini-notes sources from gmail.db into pipeline.db.

    Behaviour varies by pipeline:
    - ``transcript-ingest``: performs real Gmail discovery via
      :func:`fieldkit.ingest.sources.discover_gemini_sources`.
    - Other pipelines (stubs): reports existing source counts only.

    Returns:
        0 on success, 1 on validation error.
    """
    from fieldkit.commands.ingest.registry import PIPELINE_MAP

    # --- pipeline validation ---
    if pipeline_id not in PIPELINE_MAP:
        available = ", ".join(sorted(PIPELINE_MAP))
        click.echo(
            f"Error: unknown pipeline '{pipeline_id}'. Available pipelines: {available}",
            err=True,
        )
        return 1

    from fieldkit.ingest.constants import AMBIENT_TRANSCRIPT_PIPELINE

    dry_run_flag: bool = dry_run
    prefix = "[dry-run] " if dry_run_flag else ""

    handlers = {
        "transcript-ingest": lambda: _discover_gemini_or_reject_latest(
            pipeline_id,
            dry_run_flag,
            prefix,
            limit,
            include_latest=include_latest,
            account=account,
            as_json=as_json,
        ),
        AMBIENT_TRANSCRIPT_PIPELINE: lambda: _discover_ambient_transcript_ingest(
            dry_run=dry_run_flag, include_latest=include_latest, limit=limit, as_json=as_json
        ),
    }
    return handlers.get(pipeline_id, lambda: _report_stub_pipeline(pipeline_id, prefix, as_json=as_json))()


def _discover_gemini_or_reject_latest(
    pipeline_id: str,
    dry_run: bool,
    prefix: str,
    limit: int | None,
    *,
    include_latest: bool,
    account: str | None,
    as_json: bool,
) -> int:
    if include_latest:
        click.echo("Error: --include-latest is only valid for ambient-transcript-ingest.", err=True)
        return 1
    return _discover_transcript_ingest(pipeline_id, dry_run, prefix, limit, account=account, as_json=as_json)


def _discover_ambient_transcript_ingest(
    *,
    dry_run: bool,
    include_latest: bool,
    limit: int | None,
    as_json: bool,
) -> int:
    """Discover completed ambient JSONL sessions without exposing their text."""
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.config import get_fieldkit_home
    from fieldkit.ingest.ambient import (
        AmbientSourceError,
        get_ambient_root,
        register_ambient_snapshots,
        scan_ambient_snapshots,
    )
    from fieldkit.ingest.constants import AMBIENT_TRANSCRIPT_PIPELINE
    from fieldkit.ingest.db import get_db_path, init_db

    ambient_root = get_ambient_root(get_fieldkit_home())
    try:
        snapshots, errors = scan_ambient_snapshots(ambient_root, include_latest=include_latest)
    except (AmbientSourceError, OSError) as exc:
        click.echo(f"Error reading ambient sessions: {exc}", err=True)
        return 1
    if limit is not None:
        snapshots = snapshots[:limit]

    registered = snapshots
    if not dry_run:
        conn = init_db(get_db_path(), pipelines=PIPELINES)
        try:
            registered = register_ambient_snapshots(conn, snapshots)
            total = conn.execute(
                "SELECT COUNT(*) FROM sources WHERE pipeline_id = ?",
                (AMBIENT_TRANSCRIPT_PIPELINE,),
            ).fetchone()[0]
        finally:
            conn.close()
    else:
        total = None

    items = [
        {
            "source_id": snapshot.source_id,
            "file": snapshot.relative_path.as_posix(),
            "meeting_date": snapshot.meeting_date.isoformat(),
        }
        for snapshot in registered
    ]
    if as_json:
        click.echo(
            json.dumps(
                {
                    "pipeline_id": AMBIENT_TRANSCRIPT_PIPELINE,
                    "dry_run": dry_run,
                    "include_latest": include_latest,
                    "items": items,
                    "count": len(items),
                    "total_sources": total,
                    "failed": [{"file": path.name, "error": error} for path, error in errors],
                },
                indent=2,
            )
        )
    else:
        action = "would register" if dry_run else "registered"
        click.echo(f"Pipeline '{AMBIENT_TRANSCRIPT_PIPELINE}': {len(items)} source(s) {action}.")
        for item in items:
            click.echo(f"  {item['source_id']}  {item['file']}  ({item['meeting_date']})")
        for path, error in errors:
            click.echo(f"  Failed {path.name}: {error}", err=True)
    return 1 if errors else 0


def _dry_run_transcript_ingest(
    pipeline_id: str,
    prefix: str,
    gmail_db_path: Path,
    limit: int | None,
    *,
    account: str | None = None,
    as_json: bool = False,
) -> int:
    """Dry-run: scan gmail.db and report what would be registered."""
    try:
        from fieldkit.gmail.discover import scan_gemini_candidates
        from fieldkit.ingest.sources import filter_gemini_candidates_for_account

        candidates = filter_gemini_candidates_for_account(
            scan_gemini_candidates(
                gmail_db_path,
                limit,
                max_age_days=None,
                default_limit=None,
                require_positive_limit=False,
            ),
            account,
        )
    except Exception as exc:  # noqa: BLE001 — surface read errors, return 1
        click.echo(f"Error reading gmail.db: {exc}", err=True)
        return 1

    found = [(candidate.source_id, candidate.subject) for candidate in candidates]

    if as_json:
        click.echo(
            json.dumps(
                {
                    "pipeline_id": pipeline_id,
                    "dry_run": True,
                    "items": [{"source_id": doc_id, "subject": subject} for doc_id, subject in found],
                    "count": len(found),
                    "would_register": len(found),
                    "filters": {"account": account, "limit": limit},
                },
                indent=2,
                default=str,
            )
        )
        return 0

    click.echo(f"{prefix}Pipeline '{pipeline_id}': {len(found)} source(s) would be registered from gmail.db.")
    for doc_id, subject in found[:10]:
        click.echo(f"  {prefix}{doc_id}  ({subject[:70]})")
    if len(found) > 10:
        click.echo(f"  {prefix}... and {len(found) - 10} more")
    return 0


def _live_run_transcript_ingest(
    pipeline_id: str,
    gmail_db_path: Path,
    limit: int | None,
    *,
    account: str | None = None,
    as_json: bool = False,
) -> int:
    """Live run: init or open pipeline.db and perform real discovery."""
    from fieldkit.gmail.discover import scan_gemini_candidates
    from fieldkit.ingest.db import get_db, init_db
    from fieldkit.ingest.sources import discover_gemini_sources

    try:
        from fieldkit.commands.ingest.registry import PIPELINES
        from fieldkit.ingest.db import get_db_path

        db_path = get_db_path()
        conn = get_db(db_path) if db_path.exists() else init_db(db_path, pipelines=PIPELINES)
    except Exception as exc:  # noqa: BLE001 — surface as user-visible error, return 1
        click.echo(f"Error opening pipeline.db: {exc}", err=True)
        return 1

    try:
        existing_count: int = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE pipeline_id = ?",
            (pipeline_id,),
        ).fetchone()[0]

        if account is not None:
            # Account routing for Gemini sources is determined during 'ingest run'
            # (when doc content is fetched and attendee emails are extracted).
            # Discover registers all sources; run-time filtering respects account.
            click.echo(
                f"Note: --account={account} scopes output reporting. "
                "Sources are registered globally; account routing applies during 'ingest run'.",
                err=True,
            )

        candidates = scan_gemini_candidates(gmail_db_path, limit)
        newly_discovered = discover_gemini_sources(conn, candidates)

        total_count: int = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE pipeline_id = ?",
            (pipeline_id,),
        ).fetchone()[0]

        new_count = len(newly_discovered)
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "pipeline_id": pipeline_id,
                        "dry_run": False,
                        "items": [
                            {"source_id": rec.source_id, "title": rec.meeting_title or rec.subject}
                            for rec in newly_discovered
                        ],
                        "count": new_count,
                        "newly_discovered": new_count,
                        "previously_registered": existing_count,
                        "total_sources": total_count,
                        "filters": {"account": account, "limit": limit},
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            click.echo(
                f"Pipeline '{pipeline_id}': "
                f"{total_count} total source(s) "
                f"({new_count} newly discovered, {existing_count} previously registered)."
            )
            if newly_discovered:
                click.echo("  New sources (showing up to 5):")
                for rec in newly_discovered[:5]:
                    title = rec.meeting_title or rec.subject[:60]
                    click.echo(f"    {rec.source_id}  {title}")
                if new_count > 5:
                    click.echo(f"    ... and {new_count - 5} more")

    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface discovery errors, return 1
        click.echo(f"Error during discovery: {exc}", err=True)
        return 1
    finally:
        conn.close()

    return 0


def _discover_transcript_ingest(
    pipeline_id: str,
    dry_run: bool,
    prefix: str,
    limit: int | None,
    *,
    account: str | None = None,
    as_json: bool = False,
) -> int:
    """Scan gmail.db for Gemini sources and register them in pipeline.db."""
    from fieldkit.gmail.discover import get_gmail_db_path

    gmail_db_path = get_gmail_db_path()
    if not gmail_db_path.exists():
        click.echo(
            f"Error: gmail.db not found at {gmail_db_path}. Run the gmail-cache sync first.",
            err=True,
        )
        return 1

    if dry_run:
        return _dry_run_transcript_ingest(pipeline_id, prefix, gmail_db_path, limit, account=account, as_json=as_json)
    return _live_run_transcript_ingest(pipeline_id, gmail_db_path, limit, account=account, as_json=as_json)


def _report_stub_pipeline(pipeline_id: str, prefix: str, as_json: bool = False) -> int:
    """Report source count for a stub pipeline (no active discovery)."""
    try:
        from fieldkit.ingest.db import get_db

        conn = get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sources WHERE pipeline_id = ?",
                (pipeline_id,),
            ).fetchone()
            count: int = row["n"] if row else 0
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "pipeline_id": pipeline_id,
                            "stub": True,
                            "items": [],
                            "count": 0,
                            "registered_sources": count,
                        },
                        indent=2,
                        default=str,
                    )
                )
            else:
                click.echo(f"{prefix}Pipeline '{pipeline_id}': {count} registered source(s).")
        finally:
            conn.close()
    except FileNotFoundError:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "pipeline_id": pipeline_id,
                        "stub": True,
                        "items": [],
                        "count": 0,
                        "registered_sources": None,
                        "note": "pipeline.db not initialized. Run: fieldkit ingest status",
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            click.echo("pipeline.db not initialized. Run: fieldkit ingest status (initializes on first run)")

    return 0
