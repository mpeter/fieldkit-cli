"""Configured application roots take precedence over the development fallback.

Explicit overrides must be valid absolute paths after home expansion. Bundled
skill discovery does not require an application-checkout override.
"""

from pathlib import Path

import pytest
import yaml

import fieldkit.config as _config_mod
import fieldkit.config._loader as _config_impl_mod
import fieldkit.config._paths as _paths_impl_mod
from fieldkit.config import ConfigError, get_fieldkit_root

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["checkout", "./checkout", "../checkout", "", "   ", None, 42])
def test_fieldkit_root_rejects_invalid_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"fieldkit_root": value}), encoding="utf-8")
    monkeypatch.setattr(_config_impl_mod, "CONFIG_PATH", cfg)
    _config_mod.clear_config_caches()

    with pytest.raises(ConfigError, match="fieldkit_root"):
        get_fieldkit_root()


def test_fieldkit_root_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fieldkit_root: ~/checkout\n", encoding="utf-8")
    monkeypatch.setattr(_config_impl_mod, "CONFIG_PATH", cfg)
    _config_mod.clear_config_caches()

    result = get_fieldkit_root()

    assert result == (Path.home() / "checkout").resolve()


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the @cache between tests to prevent cross-test bleed."""
    _config_mod.clear_config_caches()
    yield
    _config_mod.clear_config_caches()


def test_fieldkit_root_honours_config_key(tmp_path, monkeypatch):
    """When fieldkit_root is set in config.yaml, get_fieldkit_root() returns it."""
    expected = tmp_path / "my-fieldkit-cli"
    expected.mkdir()

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.dump({"fieldkit_home": str(tmp_path / "data"), "fieldkit_root": str(expected)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(_config_impl_mod, "CONFIG_PATH", cfg)

    result = get_fieldkit_root()
    assert result == expected.resolve()


def test_fieldkit_root_computed_fallback_points_to_repo(monkeypatch, tmp_path):
    """When fieldkit_root is absent from config, fallback is parent of lib/config.py.

    In a dev install (not uv tool install) this correctly resolves to the repo root.
    In a uv tool install the fallback would point to site-packages — hence the config
    key is required for installed use.
    """
    # Simulate missing config
    monkeypatch.setattr(_config_impl_mod, "CONFIG_PATH", tmp_path / "nonexistent.yaml")

    result = get_fieldkit_root()
    # Path(__file__).parent.parent: lib/config.py -> lib/ -> fieldkit-cli/
    expected = Path(_paths_impl_mod.__file__).resolve().parent.parent.parent.parent
    assert result == expected


def test_fieldkit_root_falls_back_on_yaml_error(tmp_path, monkeypatch):
    """Malformed YAML in config falls back to computed path gracefully."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{{ not: valid: yaml ::::", encoding="utf-8")
    monkeypatch.setattr(_config_impl_mod, "CONFIG_PATH", cfg)

    result = get_fieldkit_root()
    expected = Path(_paths_impl_mod.__file__).resolve().parent.parent.parent.parent
    assert result == expected


def test_fieldkit_root_falls_back_when_key_missing(tmp_path, monkeypatch):
    """Config exists but has no fieldkit_root key — falls back to computed path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"fieldkit_home": str(tmp_path / "data")}), encoding="utf-8")
    monkeypatch.setattr(_config_impl_mod, "CONFIG_PATH", cfg)

    result = get_fieldkit_root()
    expected = Path(_paths_impl_mod.__file__).resolve().parent.parent.parent.parent
    assert result == expected
