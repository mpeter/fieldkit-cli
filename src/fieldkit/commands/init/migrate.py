"""init migrate — Rename deprecated config key data_repo → fieldkit_home.

Thin Click adapter. The migration itself lives in ``fieldkit.config.migrate``.
"""

import json
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.config.migrate import (
    CURRENT_KEY,
    DEPRECATED_KEY,
    ConfigMigrationError,
    MigrationResult,
    migrate_config,
)

LOG_PREFIX = "[init-migrate]"

_NO_OP_MESSAGES = {
    "already-migrated": f"Config already migrated — '{CURRENT_KEY}' is already set. No changes made.",
    "nothing-to-migrate": f"No '{DEPRECATED_KEY}' key found — nothing to migrate.",
}


@click.command(name="migrate")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the migration outcome as JSON.")
def migrate_cmd(as_json: bool) -> None:
    """Rename deprecated 'data_repo' config key to 'fieldkit_home'.

    Reads ~/.config/fieldkit/config.yaml. If the deprecated 'data_repo' key
    exists and 'fieldkit_home' is absent, renames it in-place.

    A backup is saved as config.yaml.bak before any modification.

    Exit codes:
      0 — migration done (or already migrated)
      1 — config file not found or not writable
    """
    # Imported inside the command, not at module scope: CONFIG_PATH is patched
    # in tests, which only takes effect for imports resolved at call time.
    from fieldkit.config._loader import CONFIG_PATH

    try:
        result = migrate_config(Path(CONFIG_PATH))
    except ConfigMigrationError as exc:
        click.echo(f"{LOG_PREFIX} {exc}", err=True)
        if exc.remedy:
            click.echo(exc.remedy, err=True)
        raise SystemExit(EXIT_PARTIAL) from None

    if as_json:
        click.echo(json.dumps(_payload(result), indent=2, default=str))
        return

    if result.outcome == "migrated":
        click.echo(f"{LOG_PREFIX} Migrated: '{DEPRECATED_KEY}' → '{CURRENT_KEY}' in {result.config_path}")
        click.echo(f"{LOG_PREFIX} Backup saved to {result.backup_path}")
        return

    click.echo(f"{LOG_PREFIX} {_NO_OP_MESSAGES[result.outcome]}")


def _payload(result: MigrationResult) -> dict[str, object]:
    """Render *result* as the --json contract."""
    return {
        "config_path": str(result.config_path),
        "outcome": result.outcome,
        "migrated": result.migrated,
        "backup_path": str(result.backup_path) if result.backup_path is not None else None,
    }
