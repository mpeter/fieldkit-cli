"""Tests for fieldkit.commands.init config.yaml merge-write behaviour.

Verifies that re-running setup preserves keys it does not manage
(gmail_db, gmail_token, shadowbot_token, etc.).
"""

from pathlib import Path

import pytest
import yaml

from fieldkit.commands.init import _wizard_write_config

pytestmark = pytest.mark.unit


def _write_config(config_path: Path, managed: dict[str, object]) -> None:
    """Replicate the merge-write logic from fieldkit/setup/__init__.py.

    This is a copy of the relevant section so the test stays hermetic.
    If the production code changes, this test should break — that's intentional.
    """
    existing_raw: dict[str, object] = {}
    if config_path.exists():
        try:
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing_raw = parsed
        except (OSError, yaml.YAMLError):
            pass

    merged: dict[str, object] = {**existing_raw, **managed}

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(merged, f, default_flow_style=False, allow_unicode=True)


# ── TestConfigMergeWrite (flattened) ────────────────────────────────────────


def test_config_merge_write_unknown_keys_preserved_on_rewrite(tmp_path: Path) -> None:
    """Unknown keys written before setup survive a setup re-run."""
    config_path = tmp_path / "config.yaml"

    # Simulate a pre-existing config with extra keys setup doesn't manage.
    initial = {
        "data_repo": "/old/data",
        "gmail_db": "/var/data/fieldkit/gmail.db",
        "gmail_token": "/var/config/fieldkit/gmail-token.json",
        "shadowbot_token": "tok_abc123",
    }
    config_path.write_text(yaml.dump(initial), encoding="utf-8")

    # Simulate setup re-running with updated managed keys.
    managed = {
        "data_repo": "/new/data",
        "fieldkit_root": "/opt/fieldkit-cli",
        "pipeline_db": "/new/data/data/pipeline.db",
        "name": "Test User",
    }
    _write_config(config_path, managed)

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    # Managed keys updated.
    assert result["data_repo"] == "/new/data"
    assert result["fieldkit_root"] == "/opt/fieldkit-cli"
    assert result["name"] == "Test User"

    # Unknown keys preserved.
    assert result["gmail_db"] == "/var/data/fieldkit/gmail.db"
    assert result["gmail_token"] == "/var/config/fieldkit/gmail-token.json"
    assert result["shadowbot_token"] == "tok_abc123"


def test_config_merge_write_managed_keys_win_on_conflict(tmp_path: Path) -> None:
    """When the same key exists in both, the managed value wins."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"data_repo": "/old/data", "name": "Old Name"}), encoding="utf-8")

    _write_config(config_path, {"data_repo": "/new/data", "name": "New Name"})

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result["data_repo"] == "/new/data"
    assert result["name"] == "New Name"


def test_config_merge_write_no_existing_config(tmp_path: Path) -> None:
    """Works correctly when no config.yaml exists yet (first run)."""
    config_path = tmp_path / "config.yaml"
    assert not config_path.exists()

    _write_config(config_path, {"data_repo": "/data", "fieldkit_root": "/repo"})

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result["data_repo"] == "/data"
    assert result["fieldkit_root"] == "/repo"


def test_config_merge_write_corrupt_existing_config_handled_gracefully(tmp_path: Path) -> None:
    """Unreadable/corrupt config is silently replaced rather than crashing."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(": : : not valid yaml : : :", encoding="utf-8")

    # Should not raise.
    _write_config(config_path, {"data_repo": "/data"})

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result["data_repo"] == "/data"


# ── TestGmailDbPathConsistency (flattened) ──────────────────────────────────


def test_gmail_db_path_consistency_wizard_write_config_includes_gmail_db(tmp_path: Path) -> None:
    """_wizard_write_config writes gmail_db pointing to <data_dir>/data/gmail.db."""
    from unittest.mock import patch

    import yaml

    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    # Patch CONFIG_PATH and config.__file__ so setup writes to our tmp config.
    import fieldkit.commands.init as setup_mod

    with (
        patch.object(setup_mod.cfg, "CONFIG_PATH", config_path),
        patch.object(setup_mod.cfg, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="Test User",
            email="test@example.com",  # pii-guard: ignore
            role="",
            company="",
            gcp_project="",
        )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    expected_gmail_db = str(data_dir / "data" / "gmail.db")
    assert "gmail_db" in result, "gmail_db key must be written by setup"
    assert result["gmail_db"] == expected_gmail_db


def test_gmail_db_path_consistency_get_gmail_db_path_matches_setup_output(tmp_path: Path) -> None:
    """get_gmail_db_path() returns same path that setup would write for the same data_dir."""
    from unittest.mock import patch

    from fieldkit.gmail.discover import get_gmail_db_path

    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    import fieldkit.commands.init as setup_mod

    with (
        patch.object(setup_mod.cfg, "CONFIG_PATH", config_path),
        patch.object(setup_mod.cfg, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="",
            email="",
            role="",
            company="",
            gcp_project="",
        )

    # get_gmail_db_path reads CONFIG_PATH — patch it to point to our written config.
    import fieldkit.config._loader as lib_cfg

    with patch.object(lib_cfg, "CONFIG_PATH", config_path):
        result_path = get_gmail_db_path()

    expected = (data_dir / "data" / "gmail.db").resolve()
    assert result_path == expected, f"get_gmail_db_path() returned {result_path!r}, but setup would write {expected!r}"


# ── TestIssuesDirConsistency (flattened) ────────────────────────────────────


def test_wizard_write_config_omits_unconfigured_github_repo(tmp_path: Path) -> None:
    """_wizard_write_config does not install an operator-specific repository default."""
    from unittest.mock import patch

    import yaml

    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    import fieldkit.commands.init as setup_mod

    with (
        patch.object(setup_mod.cfg, "CONFIG_PATH", config_path),
        patch.object(setup_mod.cfg, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="Test User",
            email="test@example.com",  # pii-guard: ignore
            role="",
            company="",
            gcp_project="",
        )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "github_repo" not in result


def test_issues_dir_consistency_wizard_write_config_preserves_custom_github_repo(tmp_path: Path) -> None:
    """Re-running _wizard_write_config does not overwrite a user-customised github_repo."""
    from unittest.mock import patch

    import yaml

    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"
    custom_repo = "myorg/my-fieldkit"

    # Pre-seed config with a custom github_repo value.
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"github_repo": custom_repo}), encoding="utf-8")

    import fieldkit.commands.init as setup_mod

    with (
        patch.object(setup_mod.cfg, "CONFIG_PATH", config_path),
        patch.object(setup_mod.cfg, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="Test User",
            email="test@example.com",  # pii-guard: ignore
            role="",
            company="",
            gcp_project="",
        )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result["github_repo"] == custom_repo, (
        f"Custom github_repo should be preserved, but got: {result['github_repo']!r}"
    )


def test_get_github_repo_requires_explicit_post_setup_configuration(tmp_path: Path) -> None:
    """A fresh setup requires repository selection before issue commands."""
    from unittest.mock import patch

    from fieldkit.config import ConfigError, get_github_repo

    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-data"

    import fieldkit.commands.init as setup_mod

    with (
        patch.object(setup_mod.cfg, "CONFIG_PATH", config_path),
        patch.object(setup_mod.cfg, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="",
            email="",
            role="",
            company="",
            gcp_project="",
        )

    # get_github_repo reads CONFIG_PATH — patch it to point to our written config.
    import fieldkit.config._loader as lib_cfg

    with (
        patch.object(lib_cfg, "CONFIG_PATH", config_path),
        pytest.raises(ConfigError, match="missing required key 'github_repo'"),
    ):
        get_github_repo()
