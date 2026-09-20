"""fieldkit configuration module.

Owns config-path selection, cache registration, validation, and raw YAML reads.
"""

import os
import stat
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import ValidationError as _PydanticValidationError

from fieldkit.config._schema import _FieldkitConfig
from fieldkit.errors import FieldkitError


def _default_config_dir() -> Path:
    """Return fieldkit's XDG-aware user configuration directory."""
    raw_xdg = os.environ.get("XDG_CONFIG_HOME")
    xdg = Path(raw_xdg) if raw_xdg and raw_xdg.strip() else None
    base = xdg if xdg is not None and xdg.is_absolute() else Path.home() / ".config"
    return base / "fieldkit"


CONFIG_PATH: Path = _default_config_dir() / "config.yaml"

# ---------------------------------------------------------------------------
# implementation change: Self-maintaining cache registry
# ---------------------------------------------------------------------------

_F = TypeVar("_F", bound=Callable[..., object])

# All @_config_cache-decorated functions are appended here automatically.
# clear_config_caches() iterates this list so it never goes stale.
_CACHED: list[Callable[..., object]] = []


def _config_cache(fn: _F) -> _F:
    """Wrap @cache and register the cached function in _CACHED.

    implementation change: Replaces bare @cache on config accessor functions so that
    clear_config_caches() can iterate _CACHED without a hand-maintained list.
    The wrapper preserves the original function signature and all cache_clear /
    cache_info attributes added by @functools.cache.
    """
    cached_fn = cache(fn)
    _CACHED.append(cached_fn)
    return cached_fn  # type: ignore[return-value]


class ConfigError(FieldkitError):
    """Raised for all configuration failures (missing file, bad YAML, missing keys)."""


def _read_config_dict(path: Path) -> dict[str, object] | None:
    """Read and parse a YAML config file at an explicit path into a dict, or None on any failure.

    Uncached, single-purpose helper for callers that need a one-off raw read of
    a config-shaped YAML file — e.g. resolving an optional override key at a
    caller-supplied (possibly test-patched) path — without the cached,
    validated default-path resolution that `_load_raw_config()` performs.
    """
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _has_stable_directory_ancestor(path: Path) -> bool:
    """Return whether a missing path has a stable, usable directory ancestor."""
    candidate = path
    while True:
        try:
            entry = candidate.lstat()
        except FileNotFoundError:
            if candidate.parent == candidate:
                return False
            candidate = candidate.parent
            continue
        except OSError:
            return False

        if candidate == path:
            return False
        if stat.S_ISLNK(entry.st_mode):
            try:
                target = candidate.stat()
            except OSError:
                return False
            if not stat.S_ISDIR(target.st_mode):
                return False
        elif not stat.S_ISDIR(entry.st_mode):
            return False

        try:
            rechecked = candidate.lstat()
        except OSError:
            return False
        return os.path.samestat(entry, rechecked) and stat.S_IFMT(entry.st_mode) == stat.S_IFMT(rechecked.st_mode)


def _load_raw_config_uncached(*, strict: bool) -> dict[str, object] | None:
    """Load and validate config.yaml without consulting the process cache.

    Permissive reads preserve the historical fallback behavior for absent
    paths, OS read errors, malformed YAML, and non-mapping config. Strict reads
    distinguish a truly absent path from a broken filesystem entry and
    translate every user config read or parse failure into ``ConfigError``.
    """
    try:
        raw_text = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if not strict or _has_stable_directory_ancestor(CONFIG_PATH):
            return None
        raise ConfigError(f"Could not read config file {CONFIG_PATH}: {exc}") from exc
    except OSError as exc:
        if strict:
            raise ConfigError(f"Could not read config file {CONFIG_PATH}: {exc}") from exc
        return None
    except UnicodeError as exc:
        if strict:
            raise ConfigError(f"Could not read config file {CONFIG_PATH}: {exc}") from exc
        raise

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        if strict:
            raise ConfigError(f"Config file {CONFIG_PATH} contains invalid YAML (Invalid YAML): {exc}") from exc
        return None
    if not isinstance(data, dict):
        if strict:
            raise ConfigError(f"Config file {CONFIG_PATH} must contain a YAML mapping, got {type(data).__name__}")
        return None
    # implementation note: type-check known fields via Pydantic. Catches bad values (e.g.
    # fieldkit_home: 123) before they reach domain logic. Catches only
    # ValidationError — PydanticUserError or other exceptions propagate uncaught
    # (they indicate a programming error, not a user config error).
    try:
        _FieldkitConfig.model_validate(data)
    except _PydanticValidationError as exc:
        raise ConfigError(f"Config file {CONFIG_PATH} has invalid values: {exc}") from exc
    return data


@_config_cache
def _load_raw_config() -> dict[str, object] | None:
    """Load config.yaml as a validated raw dict using the process cache.

    Raises ConfigError if known fields have wrong types (e.g. fieldkit_home: 123).
    Unknown keys are silently ignored. Validation fires once per process lifetime
    (or after clear_config_caches()) because this function is @cache-decorated.
    """
    return _load_raw_config_uncached(strict=False)


def clear_config_caches() -> None:
    """Clear all cached config values. Call in test fixtures for isolation.

    implementation change: Iterates the _CACHED registry instead of a hand-maintained list,
    so new @_config_cache-decorated functions are cleared automatically without
    needing to update this function.
    """
    for fn in _CACHED:
        fn.cache_clear()  # type: ignore[attr-defined]
