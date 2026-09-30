"""Contact enrichment paths follow canonical workspace configuration reloads."""

from pathlib import Path

import pytest

from fieldkit.config import _loader, clear_config_caches, get_fieldkit_home
from fieldkit.enrich import _io


@pytest.mark.unit
@pytest.mark.parametrize("directory", ["enrich_dir", "contacts_memory_dir"])
def test_contact_paths_follow_reloaded_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(_loader, "CONFIG_PATH", config_path)
    first_home = tmp_path / "first-workspace"
    second_home = tmp_path / "second-workspace"
    config_path.write_text(f"fieldkit_home: {first_home}\n", encoding="utf-8")
    clear_config_caches()
    try:
        directory_fn = getattr(_io, directory)
        first_directory = directory_fn()
        assert first_directory.is_relative_to(first_home)
        assert first_directory.is_dir()

        config_path.write_text(f"fieldkit_home: {second_home}\n", encoding="utf-8")
        clear_config_caches()
        assert get_fieldkit_home() == second_home
        second_directory = directory_fn()
        assert second_directory == second_home / first_directory.relative_to(first_home)
        assert second_directory.is_dir()
        artifact = second_directory / "new-contact.txt"
        artifact.write_text("fictional contact", encoding="utf-8")
        assert artifact.read_text(encoding="utf-8") == "fictional contact"
        assert not (first_directory / artifact.name).exists()
    finally:
        clear_config_caches()
