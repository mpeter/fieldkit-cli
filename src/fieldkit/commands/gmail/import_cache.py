"""Import an explicitly selected legacy Gmail cache into managed storage."""

import json
from pathlib import Path

import click

from fieldkit.cli_registry import declare_write
from fieldkit.gmail.cache_import import import_gmail_cache, preview_gmail_cache_import


@declare_write("workspace")
@click.command(name="import-cache")
@click.option(
    "--source",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Existing legacy Gmail cache to read without modifying.",
)
@click.option(
    "--db",
    "target",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Fresh managed Gmail database path to create.",
)
@click.option("--dry-run", is_flag=True, help="Validate the complete import without creating managed data.")
@click.option("--json", "as_json", is_flag=True, help="Emit the import result as JSON.")
def cli(source: Path, target: Path, dry_run: bool, as_json: bool) -> None:
    """Import a supported legacy cache into a fresh managed Gmail database.

    The source is never changed or adopted in place. Unknown schemas, missing
    Gmail tables, changing inputs, and an existing target fail closed.

    Exit codes: 0 success; 1 resource or active-state failure; 3 invalid or
    unverified cache data.
    """
    if dry_run:
        preview = preview_gmail_cache_import(source, target)
        table_counts = preview.table_counts
        payload = {"status": "preview", "dry_run": True, "table_counts": table_counts}
        summary = "Gmail cache import preview passed without creating managed data."
    else:
        result = import_gmail_cache(source, target)
        table_counts = result.table_counts
        payload = {
            "status": "ok",
            "generation": result.generation,
            "table_counts": table_counts,
        }
        summary = f"Imported Gmail cache as managed generation {result.generation}."
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    click.echo(summary)
    for table, count in table_counts.items():
        click.echo(f"  {table}: {count}")
