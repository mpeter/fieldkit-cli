"""Organization-neutral, non-interactive first-run initialization."""

from pathlib import Path

import click
import yaml

import fieldkit.config as cfg
from fieldkit.commands.init.scaffold import write_judgment_configs
from fieldkit.util.atomic import atomic_yaml_write


def initialize_minimal(fieldkit_home: Path) -> int:
    """Create the smallest useful offline workspace and global configuration."""
    workspace = fieldkit_home.resolve()
    existing: dict[str, object] = {}
    if cfg.CONFIG_PATH.exists():
        try:
            loaded = yaml.safe_load(cfg.CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise cfg.ConfigError(f"Could not read existing config {cfg.CONFIG_PATH}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise cfg.ConfigError(f"Existing config {cfg.CONFIG_PATH} must contain a YAML mapping")
        existing = loaded

    config_dir = workspace / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "accounts").mkdir(exist_ok=True)
    (workspace / "data").mkdir(exist_ok=True)

    accounts_path = config_dir / "accounts.yaml"
    if not accounts_path.exists() and not accounts_path.is_symlink():
        atomic_yaml_write(accounts_path, {"internal_domains": [], "accounts": {}})
    write_judgment_configs(workspace)

    cfg.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
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
