"""fieldkit pursuit create — scaffold a new pursuit file from the schema template.

Usage:
    fieldkit pursuit create --account <account> --name <pursuit-slug>
    fieldkit pursuit create --account acme --name "new-deal-slug"
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.config import get_fieldkit_home
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.io import render_raw_key_value, write_frontmatter_raw

LOG_PREFIX = "[pursuit-create]"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")


def _data_root() -> Path:
    root = get_fieldkit_home()
    if root is None:
        click.echo(f"{LOG_PREFIX} No data root configured. Run: fieldkit init", err=True)
        raise SystemExit(EXIT_DATA) from None
    return Path(root)


_BODY_TEMPLATE = """
# {title}

## Overview

_Add a brief description of this pursuit._

## Next Steps

_What needs to happen next?_
"""


def _render_preview(frontmatter: dict[str, object], body: str) -> str:
    lines = [line for key, value in frontmatter.items() for line in render_raw_key_value(key, value)]
    yaml_text = "\n".join(lines)
    return f"---\n{yaml_text}\n---{body}"


def _emit_dry_run(
    target: Path,
    *,
    account: str,
    slug: str,
    frontmatter: dict[str, object],
    body: str,
    as_json: bool,
) -> None:
    """Emit the requested dry-run representation."""
    if as_json:
        click.echo(
            json.dumps({"account": account, "slug": slug, "path": str(target), "created": False, "dry_run": True})
        )
        return
    click.echo(f"Would create: {target}")
    click.echo(_render_preview(frontmatter, body))


def _emit_created(target: Path, account: str, slug: str, as_json: bool) -> None:
    """Emit the creation result and next-step guidance."""
    if as_json:
        click.echo(
            json.dumps({"account": account, "slug": slug, "path": str(target), "created": True, "dry_run": False})
        )
        return
    click.echo(f"{LOG_PREFIX} Created: {target}")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  1. Edit the file and fill in opportunity details: {target}")
    click.echo("  2. Set sf_opportunity_id in the frontmatter, then sync SF data:")
    click.echo(f"       fieldkit sf opportunity <SF_OPP_ID> {target}")
    click.echo("     Or run a bulk sync (matches by name automatically):")
    click.echo(f"       fieldkit sf listview {account}")
    click.echo()
    click.echo("  Note: pre-pipeline pursuits are excluded from 'fieldkit pursuit health'")
    click.echo("  by default. Run 'fieldkit pursuit health --include-prospect' to verify")
    click.echo("  this file appears, or advance the stage to 'prospect' or beyond.")


@declare_write("workspace")
@click.command(name="create")
@click.option("--account", "-a", required=True, help="Account directory name (e.g. acme, globalpay).")
@click.option(
    "--name",
    "-n",
    required=True,
    help="Pursuit name or slug (will be slugified: 'New Deal' → 'new-deal').",
)
@click.option("--title", "-t", default=None, help="Human-readable title (defaults to slugified name).")
@click.option(
    "--stage",
    default=Stage.PRE_PIPELINE.value,
    show_default=True,
    help="Initial pursuit stage.",
)
@click.option(
    "--sf-id",
    "sf_opportunity_id",
    default="",
    help="Salesforce opportunity ID (optional — can be set later with fieldkit sf opportunity).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be created without writing.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the creation outcome as JSON.")
def cli(
    account: str, name: str, title: str | None, stage: str, sf_opportunity_id: str, dry_run: bool, as_json: bool
) -> None:
    """Scaffold a new pursuit file with compliant frontmatter."""
    slug = _slugify(name)
    pursuit_title = title or name
    today = datetime.now(tz=UTC).date().isoformat()

    root = _data_root()
    accounts_dir = root / "accounts"
    account_dir = accounts_dir / account

    if not account_dir.is_dir():
        known = sorted(d.name for d in accounts_dir.iterdir() if d.is_dir() and not d.name.startswith("."))
        click.echo(
            f"{LOG_PREFIX} Account directory not found: {account_dir}\nKnown accounts: {', '.join(known)}",
            err=True,
        )
        raise SystemExit(EXIT_DATA) from None

    pursuits_dir = account_dir / "pursuits"
    pursuits_dir.mkdir(exist_ok=True)

    target = pursuits_dir / f"{slug}.md"
    if target.exists():
        click.echo(f"{LOG_PREFIX} File already exists: {target}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    frontmatter: dict[str, object] = {
        "stage": stage,
        "gate-status": "pending",
        "last-transition": today,
        "transition-history": [],
        "sf_opportunity_id": sf_opportunity_id,
        "sf_stage": "",
        "sf_close_date": "",
        "sf_acv": "",
        "sf_arr": "",
        "sf_consulting_acv": "",
        "sf_training_acv": "",
        "sf_deal_splits": [],
        "sf_owner": "",
        "sf_next_steps": "",
        "sf_last_pulled": "",
        "last-updated": today,
    }
    body = _BODY_TEMPLATE.format(title=pursuit_title)

    if dry_run:
        _emit_dry_run(
            target,
            account=account,
            slug=slug,
            frontmatter=frontmatter,
            body=body,
            as_json=as_json,
        )
        return

    try:
        write_frontmatter_raw(target, frontmatter, body, create=True, exclusive_create=True)
    except FileExistsError:
        click.echo(f"{LOG_PREFIX} File already exists: {target}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None
    _emit_created(target, account, slug, as_json)
