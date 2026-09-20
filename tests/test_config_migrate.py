"""Unit tests for the config migration domain module.

Covers `fieldkit.config.migrate` directly, without going through Click. The
CLI adapter's own behaviour (exit codes, --json payload) is covered by
tests/test_init_migrate.py and tests/test_json_flag_batch3.py.
"""

import stat

import pytest
import yaml

from fieldkit.config.migrate import (
    ConfigMigrationError,
    MigrationResult,
    migrate_config,
)
from fieldkit.errors import FieldkitError
from tests.conftest import skip_if_root

pytestmark = pytest.mark.unit


def test_config_migration_error_inherits_from_fieldkit_error() -> None:
    error = ConfigMigrationError("failure")
    assert isinstance(error, FieldkitError)


def _write(path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_renames_the_deprecated_key_and_reports_migrated(tmp_path):
    """data_repo present, fieldkit_home absent → renamed, backup recorded."""
    config = _write(tmp_path / "config.yaml", "data_repo: /w/fieldkit\nother: keep\n")

    result = migrate_config(config)

    assert result.outcome == "migrated"
    assert result.migrated is True
    assert result.backup_path == tmp_path / "config.yaml.bak"
    assert result.config_path == config

    written = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert written == {"fieldkit_home": "/w/fieldkit", "other": "keep"}


def test_backup_holds_the_pre_migration_content(tmp_path):
    """The .bak is the original file, not a copy of the rewritten one."""
    original = "data_repo: /w/fieldkit\nother: keep\n"
    config = _write(tmp_path / "config.yaml", original)

    result = migrate_config(config)

    assert result.backup_path is not None
    assert result.backup_path.read_text(encoding="utf-8") == original


def test_already_migrated_is_a_no_op(tmp_path):
    """fieldkit_home present → returns without touching the file."""
    content = "fieldkit_home: /w/fieldkit\n"
    config = _write(tmp_path / "config.yaml", content)

    result = migrate_config(config)

    assert result.outcome == "already-migrated"
    assert result.migrated is False
    assert result.backup_path is None
    assert config.read_text(encoding="utf-8") == content
    assert not (tmp_path / "config.yaml.bak").exists()


def test_neither_key_present_is_a_no_op(tmp_path):
    """Neither key present → nothing to migrate, file untouched."""
    content = "sf_org: my-org\n"
    config = _write(tmp_path / "config.yaml", content)

    result = migrate_config(config)

    assert result.outcome == "nothing-to-migrate"
    assert result.migrated is False
    assert result.backup_path is None
    assert config.read_text(encoding="utf-8") == content


def test_empty_config_is_nothing_to_migrate(tmp_path):
    """An empty file parses to {} rather than raising."""
    config = _write(tmp_path / "config.yaml", "")

    result = migrate_config(config)

    assert result.outcome == "nothing-to-migrate"
    assert result.migrated is False


# ---------------------------------------------------------------------------
# Key order — the reason this module does not use util.atomic_yaml_write
# ---------------------------------------------------------------------------


def test_migration_preserves_key_order(tmp_path):
    """Renaming one key must not reorder the rest of the user's config.

    Regression guard: util.atomic_yaml_write omits sort_keys=False, so routing
    the write through it would alphabetise every key as a side effect.
    """
    config = _write(tmp_path / "config.yaml", "data_repo: /w/fieldkit\nzzz_last: 1\naaa_first: 2\n")

    result = migrate_config(config)

    assert result.outcome == "migrated"
    keys = [line.split(":")[0] for line in config.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert keys == ["fieldkit_home", "zzz_last", "aaa_first"]


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_missing_config_raises_with_a_remedy(tmp_path):
    """Absent config → error naming the path, plus the 'fieldkit init' remedy."""
    missing = tmp_path / "config.yaml"

    with pytest.raises(ConfigMigrationError, match="Config file not found") as exc_info:
        migrate_config(missing)

    assert exc_info.value.remedy == "Run 'fieldkit init' to create it."


def test_unparseable_yaml_raises(tmp_path):
    """Malformed YAML → read failure, no remedy offered."""
    config = _write(tmp_path / "config.yaml", "key: [unclosed\n  bad: indent:")

    with pytest.raises(ConfigMigrationError, match="Failed to read config") as exc_info:
        migrate_config(config)

    assert exc_info.value.remedy is None


def test_non_mapping_yaml_raises(tmp_path):
    """A YAML list parses fine but is not a config — rejected, not crashed."""
    config = _write(tmp_path / "config.yaml", "- one\n- two\n")

    with pytest.raises(ConfigMigrationError, match="expected a mapping, got list"):
        migrate_config(config)


@skip_if_root
def test_read_only_config_raises_before_writing_a_backup(tmp_path):
    """Non-writable config → refused, and no stray .bak is left behind."""
    config = _write(tmp_path / "config.yaml", "data_repo: /w/fieldkit\n")
    config.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    try:
        with pytest.raises(ConfigMigrationError, match="permission denied") as exc_info:
            migrate_config(config)

        assert "rename 'data_repo' to 'fieldkit_home'" in (exc_info.value.remedy or "")
        assert not (tmp_path / "config.yaml.bak").exists()
    finally:
        config.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


def test_backup_failure_raises_and_leaves_the_config_intact(tmp_path, monkeypatch):
    """shutil.copy2 failing aborts the migration before the config is rewritten."""
    original = "data_repo: /w/fieldkit\n"
    config = _write(tmp_path / "config.yaml", original)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("fieldkit.config.migrate.shutil.copy2", _boom)

    with pytest.raises(ConfigMigrationError, match="Could not create backup"):
        migrate_config(config)

    assert config.read_text(encoding="utf-8") == original


def test_write_failure_raises_and_cleans_up_the_temp_file(tmp_path, monkeypatch):
    """A failed rewrite reports the failure and leaves no .tmp behind."""
    config = _write(tmp_path / "config.yaml", "data_repo: /w/fieldkit\n")

    def _boom(*_args, **_kwargs):
        raise OSError("no space left")

    monkeypatch.setattr("fieldkit.config.migrate.yaml.dump", _boom)

    with pytest.raises(ConfigMigrationError, match="Failed to write config"):
        migrate_config(config)

    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_migration_result_is_frozen(tmp_path):
    """MigrationResult is immutable — callers cannot rewrite the outcome."""
    config = _write(tmp_path / "config.yaml", "fieldkit_home: /w/fieldkit\n")

    result = migrate_config(config)

    assert isinstance(result, MigrationResult)
    with pytest.raises(AttributeError, match="cannot assign to field"):
        result.outcome = "migrated"  # type: ignore[misc]
