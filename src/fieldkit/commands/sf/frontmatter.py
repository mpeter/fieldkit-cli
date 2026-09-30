"""CLI adapter for Salesforce pursuit frontmatter operations."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import click

from fieldkit.cli_registry import declare_write
from fieldkit.config import get_fieldkit_home
from fieldkit.errors import FieldkitError, SalesforceSyncPartialError
from fieldkit.pursuit.io import read_pursuit_text_snapshot
from fieldkit.pursuit.validation import inspect_pursuit_quality, pursuit_schema_path, validate_pursuit_content
from fieldkit.sf.frontmatter import (
    SalesforceFrontmatterResult,
    parse_salesforce_frontmatter_payload,
    update_salesforce_frontmatter,
)

logger = logging.getLogger(__name__)


def _emit_json(payload: dict[str, Any]) -> None:
    """Emit one deterministic frontmatter result document."""
    click.echo(json.dumps(payload, indent=2, default=str))


def _read_target(path: Path) -> str:
    """Read one bounded regular Markdown file for inspection."""
    try:
        return read_pursuit_text_snapshot(path).content
    except FileNotFoundError:
        raise FieldkitError("Salesforce frontmatter target does not exist") from None
    except ValueError:
        raise FieldkitError("Salesforce frontmatter target is not a stable UTF-8 file") from None


def _run_sf_mode(
    pursuit_path: str,
    json_string: str,
    as_json: bool = False,
    dry_run: bool = False,
    *,
    expected_opportunity_id: str | None = None,
) -> SalesforceFrontmatterResult:
    """Parse CLI input, call the domain service, and render its result."""
    try:
        raw = json.loads(json_string)
    except json.JSONDecodeError:
        raise FieldkitError("Salesforce frontmatter payload is not valid JSON") from None
    payload = parse_salesforce_frontmatter_payload(raw)
    result = update_salesforce_frontmatter(
        Path(pursuit_path),
        workspace=get_fieldkit_home(),
        payload=payload,
        dry_run=dry_run,
        expected_opportunity_id=expected_opportunity_id,
    )
    _render_update_result(result, as_json=as_json, dry_run=dry_run)
    return result


def _render_update_result(result: SalesforceFrontmatterResult, *, as_json: bool, dry_run: bool) -> None:
    if result.name_slug_diverges:
        logger.warning("Salesforce opportunity name differs from pursuit filename: %s", result.path.name)
    if result.stage_drift:
        logger.warning("Salesforce and local pursuit stages differ: %s", result.path.name)
        if not as_json:
            click.echo(f"⚠  Stage drift in {result.path.name}. Run 'fieldkit pursuit advance' to align.")
    if as_json:
        payload: dict[str, Any] = {
            "mode": "sf",
            "file": str(result.path),
            "written": result.written,
            "record_kind": result.record_kind,
            "stage_drift": result.stage_drift,
            "legacy_migration": result.legacy_migration,
        }
        payload["keys_previewed" if dry_run else "keys_written"] = list(result.keys)
        if dry_run:
            payload["dry_run"] = True
        _emit_json(payload)
        return
    if dry_run:
        suffix = " (meddpicc -> legacy_meddpicc)" if result.legacy_migration else ""
        click.echo(f"DRY RUN: would update Salesforce frontmatter: {result.path}{suffix}")
    else:
        click.echo(f"Frontmatter updated: {result.path}", err=True)


def _quality_check_pursuit(pursuit_path: str, as_json: bool = False) -> None:
    result = inspect_pursuit_quality(_read_target(Path(pursuit_path)))
    if as_json:
        _emit_json(
            {
                "mode": "quality-check",
                "file": pursuit_path,
                "has_frontmatter": result.has_frontmatter,
                "advisory_count": len(result.backstory_paths),
            }
        )
        return
    for field_path in result.backstory_paths:
        click.echo(
            f"ADVISORY [{pursuit_path}]: Backstory-derived data found in frontmatter field "
            f"'{field_path}' — remove and replace with first-hand intelligence",
            err=True,
        )
    if result.has_frontmatter:
        count = len(result.backstory_paths)
        detail = "no issues found" if count == 0 else f"{count} {'advisory' if count == 1 else 'advisories'} found"
        click.echo(f"PASS [{pursuit_path}]: quality check complete — {detail}")


def _dispatch_quality_check(file_opt: str | None, as_json: bool = False) -> None:
    if not file_opt:
        raise click.UsageError("--quality-check requires --file <file.md>")
    _quality_check_pursuit(file_opt, as_json=as_json)


def _dispatch_validate(file_opt: str | None, as_json: bool = False) -> None:
    if not file_opt:
        raise click.UsageError("--validate requires --file <file.md>")
    path = Path(file_opt)
    if path.name == "template.md" or ".template" in path.parts:
        click.echo(f"SKIP: {file_opt} (template file — not validated)", err=True)
        if as_json:
            _emit_json({"mode": "validate", "file": file_opt, "status": "skipped", "errors": []})
        return
    errors = validate_pursuit_content(_read_target(path), schema_path=pursuit_schema_path())
    if errors:
        click.echo(f"INVALID: {file_opt}", err=True)
        for error in errors:
            click.echo(f"  {error}", err=True)
        if as_json:
            _emit_json({"mode": "validate", "file": file_opt, "status": "invalid", "errors": errors})
        raise SalesforceSyncPartialError("Pursuit frontmatter validation failed")
    if as_json:
        _emit_json({"mode": "validate", "file": file_opt, "status": "valid", "errors": []})
    else:
        click.echo(f"VALID: {file_opt}")


@declare_write("workspace")
@click.command("frontmatter")
@click.argument("file", required=False)
@click.argument("json_string", required=False)
@click.option("--quality-check", is_flag=True, default=False, help="Run Backstory quality checks.")
@click.option("--validate", is_flag=True, default=False, help="Validate frontmatter against the pursuit schema.")
@click.option(
    "--file",
    "file_opt",
    default=None,
    metavar="FILE",
    help="Pursuit file path (required for --quality-check and --validate).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the result of the selected mode as JSON on stdout.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview SF-mode changes without writing the file.")
def cli(
    file: str | None,
    json_string: str | None,
    quality_check: bool,
    validate: bool,
    file_opt: str | None,
    as_json: bool,
    dry_run: bool,
) -> None:
    """Write sf_* frontmatter fields to pursuit files.

    \b
    Modes:
      FILE JSON_STRING          SF-mode: upsert Salesforce fields from JSON
      --quality-check --file F  Backstory advisory check
      --validate --file F       Schema validation
    """
    if quality_check and validate:
        raise click.UsageError("Select only one inspection mode")
    if dry_run and (quality_check or validate):
        raise click.UsageError("--dry-run is only valid for FILE JSON_STRING mode")
    if quality_check:
        _dispatch_quality_check(file_opt, as_json=as_json)
        return
    if validate:
        _dispatch_validate(file_opt, as_json=as_json)
        return
    if not file or not json_string:
        raise click.UsageError("Provide FILE and JSON_STRING, or select an inspection mode")
    _run_sf_mode(file, json_string, as_json=as_json, dry_run=dry_run)
