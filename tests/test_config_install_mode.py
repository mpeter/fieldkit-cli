"""Tests for get_fieldkit_root() install-mode behaviour.

Design background (M007/S02 deferred item):
  When fieldkit is installed via `uv tool install`, Path(__file__) inside
  lib/config.py resolves to a site-packages path, not the repo root. The
  computed fallback (Path(__file__).parent.parent) would then point into
  site-packages, which is wrong for skills and data-root resolution.

  The fix is already in place: get_fieldkit_root() reads an optional
  `fieldkit_root` key from ~/.config/fieldkit/config.yaml and honours it
  over the computed fallback. Users who install via uv tool install should
  set this key.

  These tests verify:
    1. The config-override path works correctly (primary protection).
    2. The computed fallback points to the parent of lib/ (correct in dev).
    3. Malformed values (YAML errors, wrong types) fall back gracefully.
"""

from pathlib import Path

import pytest
import yaml

import fieldkit.config as _config_mod
import fieldkit.config._loader as _config_impl_mod
import fieldkit.config._paths as _paths_impl_mod
from fieldkit.config import get_fieldkit_root

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the @cache between tests to prevent cross-test bleed."""
    _config_mod.get_fieldkit_root.cache_clear()
    yield
    _config_mod.get_fieldkit_root.cache_clear()


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
