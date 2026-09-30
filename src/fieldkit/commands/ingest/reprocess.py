"""fieldkit ingest reprocess — re-run artifacts through an updated pipeline."""

import json
import sqlite3
from contextlib import ExitStack, closing
from functools import partial
from pathlib import Path

import click

from fieldkit.cli_registry import declare_write
from fieldkit.commands._account_guard import validate_account_slug
from fieldkit.commands.ingest._output import BatchOutcomes, human_echo, json_output
from fieldkit.config.optional_dependencies import GOOGLE_IMPORT_ROOTS, require_optional_profile
from fieldkit.errors import AuthError, LLMError
from fieldkit.ingest.db import ArtifactRow
from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.run_lock import transcript_run_lock
from fieldkit.util.atomic import PathLockTimeoutError

# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@declare_write("workspace")
@click.command("reprocess")
@click.option(
    "--pipeline",
    required=True,
    metavar="PIPELINE_ID",
    help="Pipeline ID to reprocess (e.g. transcript-ingest).",
)
@click.option(
    "--from-version",
    default=None,
    metavar="VERSION",
    help="Reprocess artifacts produced by this pipeline version.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Reprocess matching artifacts regardless of stored pipeline version.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview what would be reprocessed without writing anything.",
)
@click.option(
    "--interactive",
    is_flag=True,
    default=False,
    help="Prompt y/n/q for each artifact before reprocessing.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    metavar="N",
    help="Maximum number of artifacts to reprocess.",
)
@click.option(
    "--account",
    "-a",
    default=None,
    metavar="SLUG",
    help="Limit to artifacts under accounts/<slug>/. Default: all accounts.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit ordered batch outcomes as JSON.")
def cli(
    pipeline: str,
    from_version: str | None,
    force: bool,
    dry_run: bool,
    interactive: bool,
    limit: int | None,
    account: str | None,
    as_json: bool,
) -> None:
    """Re-run artifacts through an updated pipeline version.

    Artifacts carry no account column, but each was written into its account's
    directory once routing decided, so --account recovers the scope from the
    stored content path.

    Exit codes: 0 success; 1 partial failure, interruption, or pipeline/version guard;
    3 invalid selectors or account slug.
    """
    if force and from_version is not None:
        raise click.UsageError("--force cannot be combined with --from-version")
    validate_account_slug(account)
    if as_json and interactive:
        raise click.UsageError("--json cannot be combined with --interactive")
    with json_output(as_json):
        rc = _run_reprocess(
            pipeline_id=pipeline,
            from_version=from_version,
            force=force,
            dry_run=dry_run,
            interactive=interactive,
            limit=limit,
            account=account,
            as_json=as_json,
        )
    if rc:
        raise SystemExit(rc)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _run_reprocess(
    *,
    pipeline_id: str,
    from_version: str | None,
    dry_run: bool,
    interactive: bool,
    limit: int | None,
    force: bool = False,
    account: str | None = None,
    as_json: bool = False,
) -> int:
    """Re-run existing artifacts through an updated pipeline version. Returns exit code."""
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
        human_echo(
            f"Pipeline '{pipeline_id}' is a stub and cannot be reprocessed.",
            err=True,
        )
        return 1

    if pipeline_id == "transcript-ingest":
        return _reprocess_transcript_ingest(
            spec=spec,
            from_version=from_version,
            force=force,
            dry_run=dry_run,
            interactive=interactive,
            limit=limit,
            account=account,
            as_json=as_json,
        )

    human_echo(
        f"Pipeline '{pipeline_id}' reprocessing not yet implemented.",
        err=True,
    )
    return 1


# ---------------------------------------------------------------------------
# transcript-ingest reprocessing
# ---------------------------------------------------------------------------


def _prompt_reprocess_choice(source_id: str, old_version: str, new_version: str, path: str) -> str:
    """Prompt the user whether to reprocess this artifact.

    Returns 'y', 'n', or 'q'.  Returns 'q' on EOFError (non-interactive stdin).
    """
    human_echo(f"\n{source_id}  v{old_version} → v{new_version}  {path}")
    try:
        return input("Reprocess? [y/n/q] ").strip().lower()
    except EOFError:
        return "q"


def _fetch_doc_for_reprocess(service: object, source_id: str) -> GeminiDocContent | None:
    """Fetch a Gemini doc from Drive for reprocessing. Returns None on error."""
    from fieldkit.ingest.docs import (
        DocAccessDeniedError,
        DocNotFoundError,
    )
    from fieldkit.ingest.docs import (
        fetch_gemini_doc as _fetch,
    )

    try:
        return _fetch(service, source_id)
    except (AuthError, LLMError):
        raise
    except DocNotFoundError:
        human_echo(f"  Error: doc {source_id} not found (404); skipping.", err=True)
        return None
    except DocAccessDeniedError:
        human_echo(f"  Error: doc {source_id} access denied (403); skipping.", err=True)
        return None
    except Exception:  # noqa: BLE001 — report failed fetch without exposing provider payloads
        human_echo("Document fetch failed; check connectivity and retry. Note left unchanged.", err=True)
        return None


def _reprocess_one_artifact(
    *,
    art: ArtifactRow,
    service: object,
    conn: sqlite3.Connection,
    pipeline_version: str,
) -> bool:
    """Reprocess a single artifact. Returns True on success, False on error."""

    from fieldkit.config import get_fieldkit_data, get_fieldkit_home
    from fieldkit.ingest.pipeline import render_vault_note
    from fieldkit.ingest.preparation import clean_and_extract_transcript
    from fieldkit.ingest.reprocess_note import capture_reprocess_note
    from fieldkit.ingest.reprocess_replay import prepare_reprocess, replay_reprocess
    from fieldkit.ingest.router import route_by_domains

    source_id: str = art.source_id
    old_version: str = art.pipeline_version

    try:
        note = capture_reprocess_note(art, get_fieldkit_home())
    except (OSError, ValueError):
        human_echo("Cannot verify the existing meeting note; reprocess left it unchanged.", err=True)
        return False

    doc_content = _fetch_doc_for_reprocess(service, source_id)
    if doc_content is None:
        return False

    domains = [email.split("@")[-1].lower() for email in doc_content.invited_emails if "@" in email]
    route = route_by_domains(domains)

    transcript = clean_and_extract_transcript(doc_content, partial(human_echo, err=True))
    cleaned = transcript.cleaned
    meta = transcript.meta

    meta.accounts = route.accounts
    meta.pursuits = route.pursuits

    doc_url = f"https://docs.google.com/document/d/{source_id}"
    note_content = render_vault_note(
        doc_content=doc_content,
        route=route,
        meta=meta,
        cleaned_body=cleaned,
        pipeline_version=pipeline_version,
        doc_url=doc_url,
    )

    try:
        runtime_root = get_fieldkit_data()
        prepare_reprocess(
            conn,
            artifact=art,
            note=note,
            content=note_content,
            to_version=pipeline_version,
            runtime_root=runtime_root,
        )
        replay_reprocess(conn, art.artifact_id, note.workspace, runtime_root=runtime_root)
    except (OSError, ValueError, sqlite3.Error):
        human_echo("Reprocess did not complete; any saved recovery journal was retained.", err=True)
        return False

    human_echo(f"  OK  {source_id}  v{old_version} → v{pipeline_version}  {art.content_path}")
    return True


def _reprocess_transcript_ingest(
    *,
    spec: object,
    from_version: str | None,
    dry_run: bool,
    interactive: bool,
    limit: int | None,
    force: bool = False,
    account: str | None = None,
    as_json: bool = False,
) -> int:
    """Hold the shared transcript run lock through reprocessing and cleanup."""
    from fieldkit.ingest.db import get_db_path

    db_path = get_db_path().resolve()
    with ExitStack() as stack:
        try:
            stack.enter_context(transcript_run_lock(db_path))
        except PathLockTimeoutError:
            human_echo("Transcript ingest is already running; retry after it finishes.", err=True)
            if as_json:
                click.echo(json.dumps({"pipeline": "transcript-ingest", "error": "ingest_busy"}))
            return 1
        return _reprocess_locked_transcript_ingest(
            db_path=db_path,
            spec=spec,
            from_version=from_version,
            dry_run=dry_run,
            interactive=interactive,
            limit=limit,
            force=force,
            account=account,
            as_json=as_json,
        )


def _reprocess_locked_transcript_ingest(
    *,
    db_path: Path,
    spec: object,
    from_version: str | None,
    dry_run: bool,
    interactive: bool,
    limit: int | None,
    force: bool,
    account: str | None,
    as_json: bool,
) -> int:
    """Reprocess existing transcript-ingest artifacts through the current pipeline.

    Per-artifact flow:
      read artifact.source_id → fetch from Drive → re-run Stage 1 + Stage 2
      → render vault note → retain validated journal → replay exact replacement
      → atomically update pipeline version and retire the journal.

    Retained recovery precedes fresh provider work. Selection never broadens
    across account/version/limit boundaries; excluded recovery fails closed.

    SIGINT is caught after each artifact; artifacts processed before the
    interrupt retain their new version.

    Returns:
        0 on success or an explicit interactive stop.
        1 on artifact failure, interruption, or missing intentional selection.
    """
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.config import get_fieldkit_data, get_fieldkit_home
    from fieldkit.ingest.db import get_artifacts_for_reprocess, init_db
    from fieldkit.ingest.reprocess_replay import preflight_reprocess, replay_reprocess

    pipeline_version: str = getattr(spec, "version", "0.1.0")

    # historic regression: Without --from-version, get_artifacts_for_reprocess returns ALL
    # artifacts regardless of their stored version.  When those artifacts are
    # already at the current pipeline version the resulting transitions are
    # v{X} → v{X} no-ops that waste Drive API quota and produce no change.
    # Guard here for both live and --dry-run paths — the warning is equally
    # useful in both cases (D2 decision: warn even on --dry-run).
    #
    # historic regression: On --dry-run, continue past the guard so the artifact list is
    # displayed (the user can see what would be reprocessed before deciding
    # whether to add --from-version).  On live runs, return 1 immediately to
    # prevent no-op transitions.  Both paths emit the warning.
    if from_version is None and not force:
        human_echo(
            f"Warning: --from-version not set. Without it, all artifacts are selected "
            f"regardless of version, and no upgrades will occur if they are already at "
            f"v{pipeline_version}.\n"
            f"Use: fieldkit ingest reprocess --pipeline transcript-ingest "
            f"--from-version <old-version>",
            err=True,
        )
        if not dry_run:
            return 1
        # dry_run: fall through to display the artifact list so the operator
        # can see what would be selected, then return 1 at the end.

    with closing(init_db(db_path, pipelines=PIPELINES)) as conn:
        workspace = get_fieldkit_home()
        try:
            retained = {intent.artifact_id: intent for intent in preflight_reprocess(conn, workspace)}
        except (OSError, ValueError, sqlite3.Error):
            human_echo("Cannot verify retained reprocess work; no replacement was attempted.", err=True)
            if as_json:
                click.echo(json.dumps({"pipeline": "transcript-ingest", "error": "reprocess_preflight_failed"}))
            return 1
        artifacts = get_artifacts_for_reprocess(
            conn, pipeline_id="transcript-ingest", from_version=from_version, limit=limit, account=account
        )
        pending_recovery = set(retained)
        if pending_recovery - {art.artifact_id for art in artifacts}:
            human_echo(
                "Retained recovery is outside this selection; no work was attempted. "
                "Retry with the saved version and a selection including all pending recovery.",
                err=True,
            )
            if as_json:
                BatchOutcomes(pending=[intent.source_id for intent in retained.values()]).emit(
                    pipeline="transcript-ingest", dry_run=dry_run, force=force
                )
            return 1

        if not artifacts:
            version_note = _selection_note(force=force, from_version=from_version)
            account_note = f" in account {account!r}" if account else ""
            human_echo(f"No artifacts found for pipeline 'transcript-ingest'{version_note}{account_note}.")
            # historic regression: if we fell through the guard (dry_run + no from_version), still exit 1
            if as_json:
                BatchOutcomes().emit(pipeline="transcript-ingest", dry_run=dry_run, force=force)
            return 1 if (dry_run and from_version is None and not force) else 0

        if dry_run:
            limit_str = f"up to {limit}" if limit is not None else "all"
            version_note = _selection_note(force=force, from_version=from_version)
            human_echo(
                f"Dry run: pipeline=transcript-ingest{version_note}, {len(artifacts)} artifact(s) ({limit_str} requested):"
            )
            for art in artifacts:
                human_echo(f"  {art.source_id}  v{art.pipeline_version} → v{pipeline_version}  {art.content_path}")
            # historic regression: return 1 when --from-version was absent (guard fell through for dry-run)
            if as_json:
                BatchOutcomes(pending=[art.source_id for art in artifacts]).emit(
                    pipeline="transcript-ingest", dry_run=True, force=force
                )
            if pending_recovery:
                human_echo("Retained recovery remains pending; dry-run did not apply it.", err=True)
            return 1 if pending_recovery or (from_version is None and not force) else 0

        from fieldkit.ingest.docs import get_docs_service

        service = None
        recovery_only = any(
            art.artifact_id in retained and retained[art.artifact_id].to_version != pipeline_version
            for art in artifacts
        )
        n_processed = 0
        n_skipped = 0
        n_errors = 0
        interrupted = False
        outcomes = BatchOutcomes()

        with ExitStack() as provider_stack:
            try:
                for art in sorted(artifacts, key=lambda item: item.artifact_id not in retained):
                    intent = retained.get(art.artifact_id)
                    if intent is None and pending_recovery:
                        break
                    if recovery_only and intent is None:
                        continue
                    target_version = intent.to_version if intent is not None else pipeline_version
                    if interactive:
                        choice = _prompt_reprocess_choice(
                            art.source_id, art.pipeline_version, target_version, art.content_path
                        )
                        if choice == "q":
                            human_echo("Stopping. Remaining artifacts can be reprocessed later.", err=True)
                            break
                        if choice != "y":
                            human_echo(f"  Skipped: {art.source_id}")
                            n_skipped += 1
                            outcomes.skipped.append(art.source_id)
                            if intent is not None:
                                break
                            continue

                    human_echo(f"Reprocessing {art.source_id}  v{art.pipeline_version} → v{target_version} …")
                    if intent is not None:
                        try:
                            replay_reprocess(conn, art.artifact_id, workspace, runtime_root=get_fieldkit_data())
                            ok = True
                        except (OSError, ValueError, sqlite3.Error):
                            human_echo(
                                "Retained reprocess work could not complete; its journal remains for recovery.",
                                err=True,
                            )
                            ok = False
                    else:
                        if service is None:
                            require_optional_profile(
                                "ingest reprocess --pipeline transcript-ingest", "google", GOOGLE_IMPORT_ROOTS
                            )
                            service = provider_stack.enter_context(closing(get_docs_service()))
                        ok = _reprocess_one_artifact(
                            art=art, service=service, conn=conn, pipeline_version=pipeline_version
                        )
                    if ok:
                        n_processed += 1
                        outcomes.completed.append(art.source_id)
                        pending_recovery.discard(art.artifact_id)
                    else:
                        n_errors += 1
                        outcomes.failed.append(art.source_id)
                        # A failed fresh attempt may itself have retained intent.
                        # Stop before starting another provider-backed operation.
                        break

            except KeyboardInterrupt:
                interrupted = True
                human_echo(
                    "\nInterrupted. Checkpoint: artifacts processed before interrupt "
                    "have been updated to the new pipeline version.",
                    err=True,
                )

        human_echo(f"\nSummary: {n_processed} reprocessed, {n_skipped} skipped, {n_errors} error(s).")
        if as_json:
            terminal = set(outcomes.completed) | set(outcomes.skipped) | set(outcomes.failed)
            outcomes.pending.extend(art.source_id for art in artifacts if art.source_id not in terminal)
            outcomes.emit(pipeline="transcript-ingest", dry_run=False, force=force)
        if recovery_only:
            human_echo(
                "Recovered intent targets an older pipeline version; invoke reprocess again for newer work.", err=True
            )
        return 1 if n_errors or interrupted or recovery_only or pending_recovery else 0


def _selection_note(*, force: bool, from_version: str | None) -> str:
    """Describe the explicit artifact selector in human output."""
    if force:
        return " (force=true)"
    if from_version is not None:
        return f" (from-version={from_version!r})"
    return ""


def main(argv: list[str] | None = None) -> int:
    """Invoke the CLI command in-process; writes to real stdout. Returns exit code."""
    try:
        cli.main(argv or [], standalone_mode=False)
        return 0
    except click.exceptions.Exit as exc:
        return int(exc.exit_code) if exc.exit_code is not None else 0
    except click.exceptions.UsageError as exc:
        human_echo(f"Error: {exc.format_message()}", err=True)
        return 2
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0
