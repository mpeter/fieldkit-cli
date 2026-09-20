"""fieldkit ingest status — list registered pipelines and their DB state."""

import json

import click

# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _run_status(account: str | None = None, as_json: bool = False) -> None:
    """Print a formatted table of registered pipelines and optional DB row counts."""
    from fieldkit.commands.ingest.registry import PIPELINES

    # --- optional DB inspection ---
    db_counts: dict[str, dict[str, int]] = {}
    db_exists = False

    try:
        from fieldkit.ingest.db import get_db

        conn = get_db()
        db_exists = True
        for row in conn.execute("SELECT pipeline_id, COUNT(*) AS n FROM sources GROUP BY pipeline_id"):
            db_counts.setdefault(row["pipeline_id"], {})["sources"] = row["n"]
        for row in conn.execute("SELECT pipeline_id, COUNT(*) AS n FROM artifacts GROUP BY pipeline_id"):
            db_counts.setdefault(row["pipeline_id"], {})["artifacts"] = row["n"]
        conn.close()
    except FileNotFoundError:
        db_exists = False
    except Exception as exc:  # noqa: BLE001 — surface db open error to user
        click.echo(f"[warn] Could not open pipeline.db: {exc}", err=True)

    if as_json:
        items = [
            {
                "pipeline_id": spec.pipeline_id,
                "version": spec.version,
                "source_format": spec.source_format,
                "status": spec.status,
                "description": spec.description,
                "sources": db_counts.get(spec.pipeline_id, {}).get("sources", 0) if db_exists else None,
                "artifacts": db_counts.get(spec.pipeline_id, {}).get("artifacts", 0) if db_exists else None,
            }
            for spec in PIPELINES
        ]
        click.echo(
            json.dumps(
                {
                    "items": items,
                    "count": len(items),
                    "db_connected": db_exists,
                    "filters": {"account": account},
                },
                indent=2,
                default=str,
            )
        )
        return

    # --- header ---
    if db_exists:
        header = f"{'PIPELINE':<30} {'VERSION':<10} {'FORMAT':<12} {'STATUS':<8} {'SOURCES':>8} {'ARTIFACTS':>10}"
        sep = "-" * len(header)
    else:
        header = f"{'PIPELINE':<30} {'VERSION':<10} {'FORMAT':<12} {'STATUS':<8}"
        sep = "-" * len(header)

    click.echo(header)
    click.echo(sep)

    for spec in PIPELINES:
        if db_exists:
            counts = db_counts.get(spec.pipeline_id, {})
            sources = counts.get("sources", 0)
            artifacts = counts.get("artifacts", 0)
            click.echo(
                f"{spec.pipeline_id:<30} {spec.version:<10} {spec.source_format:<12}"
                f" {spec.status:<8} {sources:>8} {artifacts:>10}"
            )
        else:
            click.echo(f"{spec.pipeline_id:<30} {spec.version:<10} {spec.source_format:<12} {spec.status:<8}")

    click.echo()
    if db_exists:
        click.echo("pipeline.db: connected")
    else:
        click.echo("pipeline.db: not initialized (run `fieldkit ingest init` to create)")

    # Note stub pipelines for discoverability
    stub_pipelines = [s for s in PIPELINES if s.status == "stub"]
    if stub_pipelines:
        click.echo()
        click.echo("Stub pipelines (not yet active — contribute a SourceRecord scanner to activate):")
        for spec in stub_pipelines:
            click.echo(f"  {spec.pipeline_id}: {spec.description}")


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("status", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--account", "-a", default=None, help="Filter status to a single account slug.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the pipeline table as JSON.")
def cli(account: str | None, as_json: bool) -> None:
    """List registered pipelines and their status.

    If pipeline.db exists, also shows source and artifact row counts.
    If count=0, run 'fieldkit ingest discover' to register sources.
    """
    from fieldkit.commands._account_guard import validate_account_slug

    validate_account_slug(account)
    if account is not None and not as_json:
        # Pipeline registry is not per-account scoped; --account validates the slug
        # but does not filter the output. Notify the user before showing the table.
        click.echo(
            f"Note: pipeline status is not per-account — showing all pipelines "
            f"(account '{account}' validated but not filtered).",
            err=False,
        )
    _run_status(account=account, as_json=as_json)


def main(argv: list[str] | None = None) -> int:
    """Invoke the CLI command in-process; writes to real stdout. Returns exit code."""
    try:
        cli.main(argv or [], standalone_mode=False)
        return 0
    except click.exceptions.Exit as exc:
        return int(exc.exit_code) if exc.exit_code is not None else 0
    except click.exceptions.UsageError as exc:
        click.echo(f"Error: {exc.format_message()}", err=True)
        return 2
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0
