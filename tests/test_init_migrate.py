"""Unit tests for init migrate command.

Covers all branches of migrate_cmd:
  - config not found → exit 1
  - YAML parse error → exit 1
  - fieldkit_home already present → no-op exit 0
  - data_repo present, fieldkit_home absent → migrates, backup written, exit 0
  - both data_repo and fieldkit_home absent → no-op exit 0
  - backup file created as config.yaml.bak
  - migrated value is correct
  - data_repo key is absent after migration
  - write permission denied → exit 1
"""

import stat
import unittest.mock as mock

import pytest
import yaml
from click.testing import CliRunner

from fieldkit.commands.init.migrate import migrate_cmd
from tests.conftest import skip_if_root

PATCH_TARGET = "fieldkit.config._loader.CONFIG_PATH"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_not_found_exits_1(tmp_path):
    """Config file absent → exit 1 with error message."""
    runner = CliRunner()
    missing = tmp_path / "config.yaml"

    with mock.patch(PATCH_TARGET, str(missing)):
        result = runner.invoke(migrate_cmd)

    assert result.exit_code == 1
    assert "not found" in result.output.lower() or "not found" in (result.output + "").lower()


@pytest.mark.unit
def test_yaml_parse_error_exits_1(tmp_path):
    """Invalid YAML → exit 1."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "key: [unclosed bracket\n  bad: indent:")

    runner = CliRunner()
    with mock.patch(PATCH_TARGET, str(config_path)):
        result = runner.invoke(migrate_cmd)

    assert result.exit_code == 1


@pytest.mark.unit
def test_fieldkit_home_already_present_noop_exits_0(tmp_path):
    """fieldkit_home already set → no-op, exit 0, mentions 'already migrated'."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "fieldkit_home: /some/path\nother: value\n")

    runner = CliRunner()
    with mock.patch(PATCH_TARGET, str(config_path)):
        result = runner.invoke(migrate_cmd)

    assert result.exit_code == 0
    assert "already migrated" in result.output.lower() or "already" in result.output


@pytest.mark.unit
def test_no_data_repo_no_fieldkit_home_noop_exits_0(tmp_path):
    """Neither data_repo nor fieldkit_home present → nothing to migrate, exit 0."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "sf_org: my-org\nsome_key: value\n")

    runner = CliRunner()
    with mock.patch(PATCH_TARGET, str(config_path)):
        result = runner.invoke(migrate_cmd)

    assert result.exit_code == 0
    assert "nothing to migrate" in result.output.lower() or "nothing" in result.output


@pytest.mark.unit
def test_migration_exits_0(tmp_path):
    """data_repo present, fieldkit_home absent → migration succeeds, exit 0."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "data_repo: <user-home-path>/fieldkit\nother: value\n")

    runner = CliRunner()
    with mock.patch(PATCH_TARGET, str(config_path)):
        result = runner.invoke(migrate_cmd)

    assert result.exit_code == 0


@pytest.mark.unit
def test_migration_creates_backup(tmp_path):
    """Successful migration creates config.yaml.bak next to config.yaml."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "data_repo: <user-home-path>/fieldkit\n")

    runner = CliRunner()
    with mock.patch(PATCH_TARGET, str(config_path)):
        runner.invoke(migrate_cmd)

    backup = tmp_path / "config.yaml.bak"
    assert backup.exists(), "Backup file config.yaml.bak should be created"


@pytest.mark.unit
def test_migration_renames_key_correctly(tmp_path):
    """After migration, fieldkit_home equals the old data_repo value."""
    repo_path = "<user-home-path>/fieldkit/workspace"
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, f"data_repo: {repo_path}\nother: kept\n")

    runner = CliRunner()
    with mock.patch(PATCH_TARGET, str(config_path)):
        result = runner.invoke(migrate_cmd)

    assert result.exit_code == 0
    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated["fieldkit_home"] == repo_path


@pytest.mark.unit
def test_migration_removes_data_repo_key(tmp_path):
    """After migration, data_repo key must not appear in config.yaml."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "data_repo: <user-home-path>/fieldkit\nother: kept\n")

    runner = CliRunner()
    with mock.patch(PATCH_TARGET, str(config_path)):
        runner.invoke(migrate_cmd)

    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "data_repo" not in updated


@pytest.mark.unit
def test_migration_preserves_other_keys(tmp_path):
    """After migration, unrelated keys are preserved."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "data_repo: /some/path\nsome_key: hello\nanother: 42\n")

    runner = CliRunner()
    with mock.patch(PATCH_TARGET, str(config_path)):
        runner.invoke(migrate_cmd)

    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert updated.get("some_key") == "hello"
    assert updated.get("another") == 42


@pytest.mark.unit
@skip_if_root
def test_write_permission_denied_exits_1(tmp_path):
    """Non-writable config file → exit 1."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, "data_repo: <user-home-path>/fieldkit\n")

    # Make the file read-only
    config_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        runner = CliRunner()
        with mock.patch(PATCH_TARGET, str(config_path)):
            result = runner.invoke(migrate_cmd)

        assert result.exit_code == 1
    finally:
        # Restore write permission so tmp_path cleanup works
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
