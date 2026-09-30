"""Preflight initialization destinations before producing workspace artifacts."""

from pathlib import Path

from fieldkit.config import ConfigError
from fieldkit.util.workspace_paths import resolve_workspace_output


def bind_initialization_workspace(selected: Path) -> Path:
    """Allow an existing directory alias, never a dangling or looping alias."""
    selected = selected.expanduser()
    try:
        for component in (selected, *selected.parents):
            if component.is_symlink() and not component.resolve(strict=True).is_dir():
                raise ValueError("Invalid workspace alias")
        workspace = selected.resolve()
        if workspace.exists() and not workspace.is_dir():
            raise ValueError("Invalid workspace type")
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("Initialization workspace must be a directory or a new unaliased path") from None
    return workspace


def validate_initialization_paths(workspace: Path, directories: tuple[str, ...], files: tuple[str, ...]) -> None:
    """Reject existing redirects and wrong types in a stable workspace namespace.

    A newly absent workspace has no child destinations to inspect. The selected
    workspace itself may be an alias; child destinations must not be aliases.
    This does not provide containment against concurrent same-user renames.
    """
    if not workspace.exists():
        return
    if not workspace.is_dir():
        raise ConfigError("Initialization workspace must be a directory")
    try:
        for relative in directories:
            path = resolve_workspace_output(workspace, relative)
            if path.exists() and not path.is_dir():
                raise ValueError("Invalid directory destination")
        for relative in files:
            path = resolve_workspace_output(workspace, relative)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("Invalid file destination")
    except (OSError, ValueError):
        raise ConfigError("Initialization destinations must not redirect or have incompatible types") from None
