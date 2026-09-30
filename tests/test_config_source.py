"""Source checkout discovery must not guess installed-package ancestors."""

from pathlib import Path

import pytest

from fieldkit.config import ConfigError, _loader, _paths
from fieldkit.config.source import MAX_PROJECT_METADATA_BYTES, discover_source_checkout

pytestmark = pytest.mark.unit


def test_installed_root_accessor_does_not_guess_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "site-packages" / "fieldkit" / "config" / "_paths.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    monkeypatch.setattr(_paths, "__file__", str(module))
    monkeypatch.setattr(_loader, "CONFIG_PATH", tmp_path / "absent.yaml")
    _loader.clear_config_caches()
    try:
        with pytest.raises(ConfigError, match="checkout"):
            _paths.get_fieldkit_root()
    finally:
        _loader.clear_config_caches()


def test_source_checkout_requires_package_and_project_identity(tmp_path: Path) -> None:
    module = tmp_path / "src" / "fieldkit" / "config" / "_paths.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    (tmp_path / "src" / "fieldkit" / "__main__.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fieldkit-cli"\n', encoding="utf-8")

    result = discover_source_checkout(module)

    assert result == tmp_path


@pytest.mark.parametrize("project", [None, '[project]\nname = "other-project"\n', "[invalid"])
def test_source_checkout_rejects_missing_wrong_or_malformed_project(tmp_path: Path, project: str | None) -> None:
    module = tmp_path / "src" / "fieldkit" / "config" / "_paths.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    (tmp_path / "src" / "fieldkit" / "__main__.py").write_text("", encoding="utf-8")
    if project is not None:
        (tmp_path / "pyproject.toml").write_text(project, encoding="utf-8")

    result = discover_source_checkout(module)

    assert result is None


def test_installed_package_inside_checkout_is_not_source_checkout(tmp_path: Path) -> None:
    module = tmp_path / ".venv" / "lib" / "python3.11" / "site-packages" / "fieldkit" / "config" / "_paths.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fieldkit-cli"\n', encoding="utf-8")

    result = discover_source_checkout(module)

    assert result is None


@pytest.mark.parametrize("kind", ["missing-module", "missing-entrypoint", "oversize", "metadata-symlink"])
def test_source_discovery_rejects_incomplete_or_unsafe_metadata(tmp_path: Path, kind: str) -> None:
    module = tmp_path / "src" / "fieldkit" / "config" / "_paths.py"
    module.parent.mkdir(parents=True)
    if kind != "missing-module":
        module.write_text("", encoding="utf-8")
    if kind != "missing-entrypoint":
        (tmp_path / "src" / "fieldkit" / "__main__.py").write_text("", encoding="utf-8")
    metadata = tmp_path / "pyproject.toml"
    if kind == "metadata-symlink":
        target = tmp_path / "target.toml"
        target.write_text('[project]\nname = "fieldkit-cli"\n', encoding="utf-8")
        metadata.symlink_to(target)
    else:
        metadata.write_text("#" * (MAX_PROJECT_METADATA_BYTES + 1), encoding="utf-8")

    result = discover_source_checkout(module)

    assert result is None
