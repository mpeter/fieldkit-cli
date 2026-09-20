"""Migrate deprecated keys in the fieldkit config file.

implementation change renamed the ``config.yaml`` key ``data_repo`` to ``fieldkit_home``.
This module owns the migration itself; ``commands/init/migrate.py`` is the thin
Click adapter over it.
"""

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from fieldkit.errors import FieldkitError

DEPRECATED_KEY = "data_repo"
CURRENT_KEY = "fieldkit_home"

MigrationOutcome = Literal["migrated", "already-migrated", "nothing-to-migrate"]


class ConfigMigrationError(FieldkitError):
    """The migration could not be completed.

    Both the message and ``remedy`` are user-facing: the CLI adapter prints the
    message, then the remedy on its own line when one is set.
    """

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.remedy = remedy


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of a migration attempt.

    ``backup_path`` is set only when the file was actually rewritten.
    """

    config_path: Path
    outcome: MigrationOutcome
    migrated: bool
    backup_path: Path | None = None


def migrate_config(config_path: Path) -> MigrationResult:
    """Rename ``data_repo`` to ``fieldkit_home`` in *config_path*, in place.

    A backup is written beside the config as ``config.yaml.bak`` before the
    file is modified. Returns without writing anything when the config is
    already migrated or carries neither key.

    Raises:
        ConfigMigrationError: the config is missing, unreadable, not writable,
            or the backup or the rewrite failed.
    """
    data = _read_config(config_path)

    if CURRENT_KEY in data:
        return MigrationResult(config_path, "already-migrated", migrated=False)
    if DEPRECATED_KEY not in data:
        return MigrationResult(config_path, "nothing-to-migrate", migrated=False)

    # Checked before the backup so a read-only config fails without leaving a
    # stray .bak behind.
    if not os.access(config_path, os.W_OK):
        raise ConfigMigrationError(
            f"Cannot write to {config_path}: permission denied.",
            remedy=f"Update manually: rename '{DEPRECATED_KEY}' to '{CURRENT_KEY}' in the config file.",
        )

    backup_path = _write_backup(config_path)
    _write_yaml_preserving_order(config_path, _renamed(data))
    return MigrationResult(config_path, "migrated", migrated=True, backup_path=backup_path)


def _read_config(config_path: Path) -> dict[str, object]:
    """Load *config_path* as a YAML mapping, or raise ConfigMigrationError."""
    if not config_path.exists():
        raise ConfigMigrationError(
            f"Config file not found: {config_path}",
            remedy="Run 'fieldkit init' to create it.",
        )

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001  # OSError and every yaml.YAMLError land here
        raise ConfigMigrationError(f"Failed to read config: {exc}") from None

    if not isinstance(data, dict):
        raise ConfigMigrationError(f"Failed to read config: expected a mapping, got {type(data).__name__}.")
    return data


def _renamed(data: dict[str, object]) -> dict[str, object]:
    """Return *data* with the deprecated key renamed, preserving insertion order."""
    return {(CURRENT_KEY if key == DEPRECATED_KEY else key): value for key, value in data.items()}


def _write_backup(config_path: Path) -> Path:
    """Copy *config_path* to ``<name>.yaml.bak`` and return the backup path."""
    backup_path = config_path.with_suffix(".yaml.bak")
    try:
        shutil.copy2(config_path, backup_path)
    except OSError as exc:
        raise ConfigMigrationError(f"Could not create backup {backup_path}: {exc}") from None
    return backup_path


def _write_yaml_preserving_order(config_path: Path, data: dict[str, object]) -> None:
    """Atomically replace *config_path* with *data*, keeping key order.

    Deliberately NOT ``util.atomic_yaml_write``: that helper omits
    ``sort_keys=False``, so routing this through it would silently alphabetise
    the user's whole config as a side effect of renaming one key.
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=config_path.parent, delete=False, suffix=".tmp"
        ) as tmp:
            # Recorded before the dump, not after: a serialisation failure is
            # exactly the case the cleanup below exists for, and it cannot
            # delete a name it was never given.
            tmp_path = tmp.name
            yaml.dump(data, tmp, default_flow_style=False, allow_unicode=True, sort_keys=False)
        Path(tmp_path).replace(config_path)
    except Exception as exc:  # noqa: BLE001  # temp-file, serialisation and rename errors are one outcome
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)
        raise ConfigMigrationError(f"Failed to write config: {exc}") from None
