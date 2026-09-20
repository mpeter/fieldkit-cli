"""Private configuration accessors for fieldkit filesystem roots."""

import os
import warnings
from pathlib import Path

from fieldkit.config import _loader


@_loader._config_cache
def get_fieldkit_home() -> Path:
    """Return the resolved configured workspace root path."""
    data = _loader._load_raw_config_uncached(strict=True)
    if data is None:
        raise _loader.ConfigError(f"Config file not found: {_loader.CONFIG_PATH}")
    if "fieldkit_home" not in data:
        if "data_repo" in data:
            warnings.warn(
                "config key 'data_repo' is deprecated — use 'fieldkit_home' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            value = data["data_repo"]
            if not isinstance(value, str):
                raise _loader.ConfigError("Config key 'data_repo' (deprecated) must be a string")
            raw = value.strip()
            if not raw:
                raise _loader.ConfigError("Config key 'data_repo' (deprecated) must not be empty or whitespace")
            return Path(raw).expanduser().resolve()
        raise _loader.ConfigError(
            f"Config file {_loader.CONFIG_PATH} is missing required key 'fieldkit_home'"
            " (Note: 'data_repo' was renamed to 'fieldkit_home' — update your config.yaml)"
        )
    value = data["fieldkit_home"]
    if not isinstance(value, str):
        raise _loader.ConfigError("Config key 'fieldkit_home' must be a string")
    raw = value.strip()
    if not raw:
        raise _loader.ConfigError("Config key 'fieldkit_home' must not be empty or whitespace")
    return Path(raw).expanduser().resolve()


@_loader._config_cache
def _get_fieldkit_data_from_config() -> Path:
    """Return the configured runtime-artifacts root, falling back to workspace data."""
    data = _loader._load_raw_config()
    if data is not None and "fieldkit_data" in data:
        raw = str(data["fieldkit_data"]).strip()
        if not raw:
            raise _loader.ConfigError("Config key 'fieldkit_data' must not be empty or whitespace")
        resolved = Path(raw).expanduser().resolve()
        if not resolved.is_absolute():
            raise _loader.ConfigError(f"Config key 'fieldkit_data' must be an absolute path, got: {raw!r}")
        return resolved
    return get_fieldkit_home() / "data"


def get_fieldkit_data() -> Path:
    """Return the runtime-artifacts root, honoring FIELDKIT_DATA_DIR live."""
    value = os.environ.get("FIELDKIT_DATA_DIR")
    if value:
        raw = value.strip()
        if not raw:
            raise _loader.ConfigError("FIELDKIT_DATA_DIR must not be empty or whitespace")
        resolved = Path(raw).expanduser().resolve()
        if not resolved.is_absolute():
            raise _loader.ConfigError(f"FIELDKIT_DATA_DIR must be an absolute path, got: {raw!r}")
        return resolved
    return _get_fieldkit_data_from_config()


def get_configured_fieldkit_data() -> Path:
    """Return the config-based data root without a process-local override."""
    return _get_fieldkit_data_from_config()


def get_config_path(name: str) -> Path:
    """Return a named configuration file path under the workspace root."""
    return get_fieldkit_home() / "config" / name


@_loader._config_cache
def get_fieldkit_root() -> Path:
    """Return the fieldkit project checkout root.

    A configured root takes precedence. The development fallback preserves the
    original four-parent inference because this module has the same package
    depth as the former loader implementation.
    """
    data = _loader._load_raw_config()
    if data is not None and "fieldkit_root" in data:
        return Path(str(data["fieldkit_root"])).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent.parent


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
        candidate = Path(override.strip()).expanduser()
        if not candidate.is_absolute():
            raise _loader.ConfigError(f"FIELDKIT_HARNESS_ROOT must be an absolute path, got: {override.strip()!r}")
        return candidate.resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    xdg_path = Path(xdg).expanduser() if xdg and xdg.strip() else None
    base = xdg_path if xdg_path is not None and xdg_path.is_absolute() else Path.home() / ".cache"
    return (base / "fieldkit").resolve()
