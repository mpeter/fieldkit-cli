"""Organization-neutral, non-interactive first-run initialization."""

from pathlib import Path

import click

import fieldkit.config as cfg
from fieldkit.commands.init.paths import bind_initialization_workspace, validate_initialization_paths
from fieldkit.config._accounts import read_accounts_mapping_for_update
from fieldkit.config._loader import read_config_mapping_for_update
from fieldkit.util.atomic import atomic_yaml_write


def initialize_minimal(fieldkit_home: Path, *, dry_run: bool = False) -> int:
    """Create the smallest useful offline workspace and global configuration."""
    workspace = bind_initialization_workspace(fieldkit_home)
    validate_initialization_paths(workspace, ("config", "accounts", "data"), ("config/accounts.yaml",))
    read_config_mapping_for_update(cfg.CONFIG_PATH)
    read_accounts_mapping_for_update(workspace / "config" / "accounts.yaml")
    if dry_run:
        click.echo("Preview: initialization inputs and destinations validated; no files written.")
        return 0

    config_dir = workspace / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "accounts").mkdir(exist_ok=True)
    (workspace / "data").mkdir(exist_ok=True)

    accounts_path = config_dir / "accounts.yaml"
    if not accounts_path.exists() and not accounts_path.is_symlink():
        atomic_yaml_write(accounts_path, {"internal_domains": [], "accounts": {}})

    cfg.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = read_config_mapping_for_update(cfg.CONFIG_PATH)
    atomic_yaml_write(
        cfg.CONFIG_PATH,
        {
            **existing,
            "fieldkit_home": str(workspace),
            "pipeline_db": str(workspace / "data" / "pipeline.db"),
            "gmail_db": str(workspace / "data" / "gmail.db"),
        },
    )
    click.echo(f"Initialized offline workspace: {workspace}")
    click.echo("First success: fieldkit skill list")
    return 0
