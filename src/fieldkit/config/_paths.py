"""Private configuration accessors for fieldkit filesystem roots."""

import os
from pathlib import Path

from fieldkit.config import _loader
from fieldkit.config.source import discover_source_checkout
from fieldkit.util.workspace_paths import resolve_workspace_output, validate_relative_output_path


def _resolve_absolute_root(raw: str, setting: str) -> Path:
    """Expand a root without letting resolution silently anchor it to cwd."""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise _loader.ConfigError(f"{setting} must be an absolute path")
    try:
        return candidate.resolve()
    except (OSError, RuntimeError):
        raise _loader.ConfigError(f"{setting} could not be resolved") from None


@_loader._config_cache
def get_fieldkit_home() -> Path:
    """Return the resolved configured workspace root path."""
    data = _loader._load_raw_config_uncached(strict=True)
    if data is None:
        raise _loader.ConfigError("Config file not found")
    if "fieldkit_home" not in data:
        raise _loader.ConfigError("Config file is missing required key 'fieldkit_home'")
    value = data["fieldkit_home"]
    if not isinstance(value, str):
        raise _loader.ConfigError("Config key 'fieldkit_home' must be a string")
    raw = value.strip()
    if not raw:
        raise _loader.ConfigError("Config key 'fieldkit_home' must not be empty or whitespace")
    return _resolve_absolute_root(raw, "Config key 'fieldkit_home'")


@_loader._config_cache
def _get_fieldkit_data_from_config() -> Path:
    """Return the configured runtime-artifacts root, falling back to workspace data."""
    data = _loader._load_raw_config()
    if data is not None and "fieldkit_data" in data:
        raw = str(data["fieldkit_data"]).strip()
        if not raw:
            raise _loader.ConfigError("Config key 'fieldkit_data' must not be empty or whitespace")
        return _resolve_absolute_root(raw, "Config key 'fieldkit_data'")
    return get_fieldkit_home() / "data"


def get_fieldkit_data() -> Path:
    """Return the runtime-artifacts root, honoring FIELDKIT_DATA_DIR live."""
    value = os.environ.get("FIELDKIT_DATA_DIR")
    if value:
        raw = value.strip()
        if not raw:
            raise _loader.ConfigError("FIELDKIT_DATA_DIR must not be empty or whitespace")
        return _resolve_absolute_root(raw, "FIELDKIT_DATA_DIR")
    return _get_fieldkit_data_from_config()


def get_configured_fieldkit_data() -> Path:
    """Return the config-based data root without a process-local override."""
    return _get_fieldkit_data_from_config()


def get_config_path(name: str, *, workspace_root: Path | None = None) -> Path:
    """Resolve configuration in the default or explicitly selected workspace.

    Explicit workspace aliases are allowed, but child redirects are not. Absent
    workspaces can be inspected without creation through their existing ancestor.
    The directory namespace must remain stable during inspection or publication.
    """
    if workspace_root is None:
        return get_fieldkit_home() / "config" / name
    try:
        validate_relative_output_path(f"config/{name}")
        root = workspace_root.absolute()
        anchor = root
        while not anchor.exists():
            if anchor.is_symlink():
                raise ValueError
            anchor = anchor.parent
        return resolve_workspace_output(anchor, (root / "config" / name).relative_to(anchor).as_posix())
    except (OSError, RuntimeError, ValueError):
        raise _loader.ConfigError("Cannot locate confined workspace configuration") from None


@_loader._config_cache
def get_fieldkit_root() -> Path:
    """Return the fieldkit project checkout root.

    A configured root takes precedence. Source discovery requires the fieldkit
    project identity and layout; installed packages use bundled resources and
    have no inferred checkout root.
    """
    configured = get_configured_fieldkit_root()
    if configured is not None:
        return configured
    discovered = discover_source_checkout(Path(__file__))
    if discovered is None:
        raise _loader.ConfigError("Application checkout is unavailable; use bundled package resources")
    return discovered


def validate_configured_fieldkit_root(value: object) -> Path:
    """Validate an explicit checkout setting without requiring it to exist."""
    if not isinstance(value, str):
        raise _loader.ConfigError("Config key 'fieldkit_root' must be a string")
    raw = value.strip()
    if not raw:
        raise _loader.ConfigError("Config key 'fieldkit_root' must not be empty or whitespace")
    return _resolve_absolute_root(raw, "Config key 'fieldkit_root'")


def get_configured_fieldkit_root() -> Path | None:
    """Return the validated explicit code root, or None when no override is set."""
    data = _loader._load_raw_config()
    if data is not None and "fieldkit_root" in data:
        return validate_configured_fieldkit_root(data["fieldkit_root"])
    return None


@_loader._config_cache
def get_watchers_dir() -> Path:
    """Return the watchers runtime directory under the configured workspace."""
    return get_fieldkit_home() / "watchers"


def get_accounts_root() -> Path:
    """Return the accounts directory under the configured workspace."""
    return get_fieldkit_home() / "accounts"


def get_harness_scratch_root() -> Path:
    """Return the non-cached cache-class root for ephemeral harness worktrees."""
    override = os.environ.get("FIELDKIT_HARNESS_ROOT")
    if override and override.strip():
        return _resolve_absolute_root(override.strip(), "FIELDKIT_HARNESS_ROOT")
    xdg = os.environ.get("XDG_CACHE_HOME")
    xdg_path = Path(xdg).expanduser() if xdg and xdg.strip() else None
    base = xdg_path if xdg_path is not None and xdg_path.is_absolute() else Path.home() / ".cache"
    return (base / "fieldkit").resolve()
