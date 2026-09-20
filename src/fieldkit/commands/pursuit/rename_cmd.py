"""fieldkit pursuit rename — rename a pursuit file and update all watcher state keys.

Usage:
    fieldkit pursuit rename --account <account> --from <old-slug> --to <new-slug>
    fieldkit pursuit rename --account acme-corp --from old-deal --to new-deal
    fieldkit pursuit rename --account acme-corp --from old-deal --to new-deal --dry-run

When a pursuit opportunity name changes, run this command to:
  1. Rename the .md file
  2. Update watchers/pursuit-stall-state.json key and internal slug field
  3. Update watchers/close-date-countdown-state.json key and internal slug field
"""

import json
import shlex
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.config import get_fieldkit_home

LOG_PREFIX = "[pursuit-rename]"


def _data_root() -> Path:
    root = get_fieldkit_home()
    if root is None:
        click.echo(f"{LOG_PREFIX} No data root configured. Run: fieldkit init", err=True)
        raise SystemExit(EXIT_DATA) from None
    return Path(root)


def _update_state_json(
    state_path: Path,
    old_key: str,
    new_key: str,
    new_slug: str,
    slug_field: str,
    *,
    dry_run: bool,
    quiet: bool = False,
) -> bool:
    """Update a state JSON file's key from old_key → new_key.

    ``quiet`` suppresses the dry-run narration so JSON mode owns stdout.

    Returns True if an update was made, False if the file or key was absent.
    """
    if not state_path.exists():
        return False

    try:
        raw = state_path.read_text(encoding="utf-8")
        state: dict[str, dict[str, object]] = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"{LOG_PREFIX} WARNING: could not read {state_path.name}: {exc}", err=True)
        return False

    if old_key not in state:
        return False

    if dry_run:
        if not quiet:
            click.echo(f"[dry-run] {state_path.name}: rename key {old_key!r} → {new_key!r}")
        return True

    entry = state.pop(old_key)
    if slug_field and slug_field in entry:
        entry[slug_field] = new_slug
    state[new_key] = entry

    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


@declare_write("workspace")
@click.command(name="rename")
@click.option("--account", "-a", required=True, help="Account slug (e.g. acme-corp).")
@click.option("--from", "from_slug", required=True, help="Current pursuit slug (without .md).")
@click.option("--to", "to_slug", required=True, help="New pursuit slug (without .md).")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would change without making any modifications.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the rename outcome as JSON.")
def cli(account: str, from_slug: str, to_slug: str, dry_run: bool, as_json: bool) -> None:
    """Rename a pursuit file and update all watcher state keys.

    Renames accounts/<account>/pursuits/<from>.md to <to>.md and updates the
    slug keys in pursuit-stall-state.json and close-date-countdown-state.json.

    Run after a Salesforce opportunity name change to keep the local pursuit
    file slug aligned with the SF record name.
    """
    data_root = _data_root()
    pursuits_dir = data_root / "accounts" / account / "pursuits"
    watchers_dir = data_root / "watchers"

    # Validate account directory
    if not pursuits_dir.is_dir():
        click.echo(
            f"{LOG_PREFIX} ERROR: pursuits directory not found: {pursuits_dir}",
            err=True,
        )
        raise SystemExit(EXIT_DATA) from None

    from_path = pursuits_dir / f"{from_slug}.md"
    to_path = pursuits_dir / f"{to_slug}.md"

    # Validate source exists
    if not from_path.exists():
        click.echo(
            f"{LOG_PREFIX} ERROR: source pursuit file not found: {from_path}",
            err=True,
        )
        raise SystemExit(EXIT_DATA) from None

    # Validate target does not exist
    if to_path.exists():
        click.echo(
            f"{LOG_PREFIX} ERROR: target pursuit file already exists: {to_path}",
            err=True,
        )
        raise SystemExit(EXIT_PARTIAL)

    # Rename the pursuit file
    if dry_run:
        if not as_json:
            click.echo(f"[dry-run] rename: {from_path.name} → {to_path.name}")
    else:
        from_path.rename(to_path)
        if not as_json:
            click.echo(f"{LOG_PREFIX} renamed: {from_path.name} → {to_path.name}")

    # Update watcher state files
    old_key = f"{account}/{from_slug}"
    new_key = f"{account}/{to_slug}"
    state_updates: list[str] = []

    stall_path = watchers_dir / "pursuit-stall-state.json"
    if _update_state_json(stall_path, old_key, new_key, to_slug, "pursuit", dry_run=dry_run, quiet=as_json):
        state_updates.append("pursuit-stall-state.json")

    countdown_path = watchers_dir / "close-date-countdown-state.json"
    if _update_state_json(countdown_path, old_key, new_key, to_slug, "pursuit", dry_run=dry_run, quiet=as_json):
        state_updates.append("close-date-countdown-state.json")

    if as_json:
        click.echo(
            json.dumps(
                {
                    "account": account,
                    "from_slug": from_slug,
                    "to_slug": to_slug,
                    "from_path": str(from_path),
                    "to_path": str(to_path),
                    "renamed": not dry_run,
                    "dry_run": dry_run,
                    "state_updates": state_updates,
                    "old_key": old_key,
                    "new_key": new_key,
                },
                indent=2,
                default=str,
            )
        )
        return

    if state_updates:
        action = "[dry-run] would update" if dry_run else "updated"
        click.echo(f"{LOG_PREFIX} {action} state keys in: {', '.join(state_updates)}")
    else:
        click.echo(f"{LOG_PREFIX} no watcher state keys found for {old_key!r} — nothing to update")

    # Next steps — implementation change: replace vague "manually" GDoc hint with the exact
    # grep command the AE can copy-paste to find all references to the old slug.
    #
    # Security: use shlex.quote() on both the pattern and the path argument so
    # that slugs containing single-quotes, spaces, or shell metacharacters are
    # safely escaped in the displayed hint.  Use a relative accounts/<account>/
    # path rather than the absolute data-root path to keep the hint portable
    # (the AE runs it from the data-repo root, which is the common case).
    grep_pattern = shlex.quote(from_slug)
    grep_account = shlex.quote(f"accounts/{account}/")
    click.echo(
        f"\n  Next steps:\n"
        f"  1. Re-sync SF data with the renamed file:\n"
        f"       fieldkit sf opportunity <SF_OPP_ID> accounts/{account}/pursuits/{to_slug}.md\n"
        f"  2. Find any remaining references to the old slug:\n"
        f"       grep -r {grep_pattern} {grep_account}\n"
        f"  3. Run 'fieldkit watch run pursuit-stalls --dry-run' to confirm state is clean."
    )
