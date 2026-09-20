"""Unit tests for FieldkitConfig Pydantic validation in config/_loader.py (implementation note).

Tests the type-checking side-effect of _load_raw_config() introduced in spec 031.
Uses the same CONFIG_PATH patch target as test_lib_config.py.
Cache clearing is handled by the autouse fixture in conftest.py.
"""

from pathlib import Path

import pytest
import yaml

import fieldkit.config._loader as _loader_mod
from fieldkit.config import ConfigError, get_fieldkit_home
from fieldkit.config._loader import _load_raw_config, _load_raw_config_uncached, _read_config_dict

pytestmark = pytest.mark.unit


def _write_config(tmp_path: Path, data: dict) -> Path:
    """Write a config dict to a YAML file in tmp_path and return the path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump(data), encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# Wrong-type field → ConfigError
# ---------------------------------------------------------------------------


def test_wrong_type_fieldkit_home_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fieldkit_home: 123 (int) must raise ConfigError with field name in message."""
    cfg = _write_config(tmp_path, {"fieldkit_home": 123})
    monkeypatch.setattr(_loader_mod, "CONFIG_PATH", cfg)

    with pytest.raises(ConfigError, match=r"fieldkit_home"):
        _load_raw_config()

    with pytest.raises(ConfigError, match=r"fieldkit_home"):
        get_fieldkit_home()


def test_wrong_type_github_repo_raises_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """github_repo: [list] (wrong type) must raise ConfigError."""
    cfg = _write_config(tmp_path, {"fieldkit_home": "/tmp/ws", "github_repo": ["a", "b"]})
    monkeypatch.setattr(_loader_mod, "CONFIG_PATH", cfg)

    with pytest.raises(ConfigError, match=r"github_repo"):
        _load_raw_config()


# ---------------------------------------------------------------------------
# Unknown key → no error (extra="ignore")
# ---------------------------------------------------------------------------


def test_unknown_key_is_silently_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown keys in config.yaml must not raise — extra='ignore' is required.

    Also verifies that known keys are preserved alongside unknown ones (i.e.
    the validator does not drop any keys from the returned raw dict).
    """
    cfg = _write_config(tmp_path, {"fieldkit_home": "/tmp/ws", "future_key": "value"})
    monkeypatch.setattr(_loader_mod, "CONFIG_PATH", cfg)

    result = _load_raw_config()
    assert result is not None
    assert result["future_key"] == "value"
    assert result["fieldkit_home"] == "/tmp/ws"  # known key preserved


# ---------------------------------------------------------------------------
# Valid minimal config → no error
# ---------------------------------------------------------------------------


def test_valid_minimal_config_loads_successfully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid minimal config must load without raising."""
    cfg = _write_config(tmp_path, {"fieldkit_home": "/tmp/workspace"})
    monkeypatch.setattr(_loader_mod, "CONFIG_PATH", cfg)

    result = _load_raw_config()
    assert result is not None
    assert result["fieldkit_home"] == "/tmp/workspace"


def test_strict_raw_load_bypasses_permissive_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict reads surface a malformed current config after a permissive cache warm."""
    cfg = _write_config(tmp_path, {"fieldkit_home": "/tmp/workspace"})
    monkeypatch.setattr(_loader_mod, "CONFIG_PATH", cfg)
    assert _load_raw_config() == {"fieldkit_home": "/tmp/workspace"}

    cfg.write_text("fieldkit_home: [unclosed\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"Invalid YAML"):
        _load_raw_config_uncached(strict=True)


def test_explicit_raw_read_retains_extension_keys(tmp_path: Path) -> None:
    """Explicit-path reads return future configuration keys without normalization."""
    cfg = _write_config(tmp_path, {"fieldkit_home": "/tmp/workspace", "future_extension": {"enabled": True}})

    result = _read_config_dict(cfg)

    assert result == {"fieldkit_home": "/tmp/workspace", "future_extension": {"enabled": True}}
