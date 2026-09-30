"""CLI adapter for guarded Salesforce Key Fields reconciliation."""

import json
from pathlib import Path

import click

from fieldkit.cli_exit import handle_cli_exception
from fieldkit.cli_registry import declare_write
from fieldkit.config import get_fieldkit_home
from fieldkit.errors import FieldkitError
from fieldkit.sf.frontmatter import reconcile_salesforce_pursuit


@declare_write("workspace")
@click.command("reconcile")
@click.argument("pursuit_file")
@click.option("--dry-run", is_flag=True, default=False, help="Preview changes without writing any files.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the reconcile outcome as JSON.")
def cli(pursuit_file: str, dry_run: bool, as_json: bool) -> None:
    """Rewrite Key Fields from strict pursuit frontmatter under its publication lock."""
    try:
        result = reconcile_salesforce_pursuit(Path(pursuit_file), workspace=get_fieldkit_home(), dry_run=dry_run)
    except (FieldkitError, OSError) as exc:
        if as_json:
            click.echo(json.dumps({"file": pursuit_file, "status": "error", "dry_run": dry_run, "changed": False}))
        raise SystemExit(handle_cli_exception(exc)) from None
    for change in result.changes:
        click.echo(f"  {change.field}: {change.previous!r} → {change.updated!r}", err=True)
    if as_json:
        click.echo(
            json.dumps(
                {"file": pursuit_file, "status": result.status, "dry_run": dry_run, "changed": bool(result.changes)}
            )
        )
    else:
        prefix = "DRY RUN: " if dry_run else ""
        click.echo(f"{prefix}{result.status.upper()}: {result.path.name}", err=True)
