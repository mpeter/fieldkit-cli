"""Tests for fieldkit.commands.init config.yaml merge-write behaviour.

Verifies that re-running setup preserves keys it does not manage
(unrelated paths, nested settings, and optional integration values).
"""

from pathlib import Path

import pytest
import yaml

import fieldkit.commands.init.wizard as wizard
import fieldkit.config as setup_config
from fieldkit.commands.init.wizard import _wizard_write_config

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("source_mode", [False, True])
@pytest.mark.parametrize("artifacts", [False, True])
@pytest.mark.parametrize("override", [7, None, "", "  ", "relative-checkout"])
def test_config_writer_rejects_invalid_existing_checkout_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_mode: bool, artifacts: bool, override: object
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"fieldkit_root": override}), encoding="utf-8")
    original = config_path.read_bytes()
    monkeypatch.setattr(setup_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(
        wizard, "discover_source_checkout", lambda _module: tmp_path / "source" if source_mode else None
    )

    with pytest.raises(setup_config.ConfigError, match="fieldkit_root") as caught:
        if artifacts:
            wizard._wizard_write_artifacts(
                "Example User", "user@example.com", "", "", "", "", [], tmp_path / "workspace", "", ""
            )
        else:
            _wizard_write_config(tmp_path / "workspace", "Example User", "user@example.com", "", "", "")

    assert config_path.read_bytes() == original
    assert "relative-checkout" not in str(caught.value)
    assert not (tmp_path / "workspace").exists()


def test_source_config_writer_preserves_explicit_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    explicit = tmp_path / "explicit-checkout"
    config_path.write_text(yaml.safe_dump({"fieldkit_root": str(explicit)}), encoding="utf-8")
    monkeypatch.setattr(setup_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wizard, "discover_source_checkout", lambda _module: tmp_path / "discovered")

    result = _wizard_write_config(tmp_path / "workspace", "Example User", "user@example.com", "", "", "")

    assert result is None
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert written["fieldkit_root"] == str(explicit)


@pytest.mark.parametrize("explicit_override", [False, True])
def test_installed_config_writer_omits_guessed_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_override: bool
) -> None:
    config_path = tmp_path / "config.yaml"
    module = tmp_path / "site-packages" / "fieldkit" / "commands" / "init" / "wizard.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    if explicit_override:
        config_path.write_text(yaml.safe_dump({"fieldkit_root": str(tmp_path / "explicit-checkout")}), encoding="utf-8")
    monkeypatch.setattr(setup_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wizard, "__file__", str(module))

    result = _wizard_write_config(tmp_path / "workspace", "Example User", "user@example.com", "", "", "")

    assert result is None
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if explicit_override:
        assert written["fieldkit_root"] == str(tmp_path / "explicit-checkout")
    else:
        assert "fieldkit_root" not in written
    assert written["fieldkit_home"] == str(tmp_path / "workspace")


@pytest.mark.parametrize("existing", [None, {}, {"name": "Old Name", "custom": {"enabled": True}}])
def test_production_config_writer_merges_managed_and_operator_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: dict[str, object] | None
) -> None:
    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "workspace"
    root = tmp_path / "repo"
    if existing is not None:
        config_path.write_text(yaml.safe_dump(existing), encoding="utf-8")
    monkeypatch.setattr(setup_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wizard, "discover_source_checkout", lambda _module: root)

    result = _wizard_write_config(data_dir, "New User", "user@example.com", "", "", "")

    assert result is None
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert written["fieldkit_home"] == str(data_dir)
    assert written["fieldkit_root"] == str(root)
    assert written["gmail_db"] == str(data_dir / "data" / "gmail.db")
    assert written["pipeline_db"] == str(data_dir / "data" / "pipeline.db")
    assert written["name"] == "New User"
    if existing is not None and "custom" in existing:
        assert written["custom"] == existing["custom"]


def test_config_writer_preserves_unknown_operator_keys_without_writing_retired_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"custom": {"enabled": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wizard, "discover_source_checkout", lambda _module: None)

    result = _wizard_write_config(tmp_path / "workspace", "Example User", "user@example.com", "", "", "")

    assert result is None
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "gcp_project" not in written
    assert written["custom"] == {"enabled": True}


def test_production_config_writer_preserves_invalid_existing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    original = b"private: [broken\n"
    config_path.write_bytes(original)
    monkeypatch.setattr(setup_config, "CONFIG_PATH", config_path)

    with pytest.raises(setup_config.ConfigError, match="invalid YAML"):
        _wizard_write_config(tmp_path / "workspace", "New User", "user@example.com", "", "", "")

    assert config_path.read_bytes() == original


# ── TestGmailDbPathConsistency (flattened) ──────────────────────────────────


def test_gmail_db_path_consistency_wizard_write_config_includes_gmail_db(tmp_path: Path) -> None:
    """_wizard_write_config writes gmail_db pointing to <data_dir>/data/gmail.db."""
    from unittest.mock import patch

    import yaml

    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-workspace"

    # Patch CONFIG_PATH and config.__file__ so setup writes to our tmp config.
    with (
        patch.object(setup_config, "CONFIG_PATH", config_path),
        patch.object(setup_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="Test User",
            email="test@example.com",  # pii-guard: ignore
            role="",
            company="",
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
    data_dir = tmp_path / "fieldkit-workspace"

    with (
        patch.object(setup_config, "CONFIG_PATH", config_path),
        patch.object(setup_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="",
            email="",
            role="",
            company="",
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
    data_dir = tmp_path / "fieldkit-workspace"

    with (
        patch.object(setup_config, "CONFIG_PATH", config_path),
        patch.object(setup_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="Test User",
            email="test@example.com",  # pii-guard: ignore
            role="",
            company="",
        )

    result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "github_repo" not in result


def test_issues_dir_consistency_wizard_write_config_preserves_custom_github_repo(tmp_path: Path) -> None:
    """Re-running _wizard_write_config does not overwrite a user-customised github_repo."""
    from unittest.mock import patch

    import yaml

    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "fieldkit-workspace"
    custom_repo = "myorg/my-fieldkit"

    # Pre-seed config with a custom github_repo value.
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"github_repo": custom_repo}), encoding="utf-8")

    with (
        patch.object(setup_config, "CONFIG_PATH", config_path),
        patch.object(setup_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="Test User",
            email="test@example.com",  # pii-guard: ignore
            role="",
            company="",
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
    data_dir = tmp_path / "fieldkit-workspace"

    with (
        patch.object(setup_config, "CONFIG_PATH", config_path),
        patch.object(setup_config, "__file__", str(tmp_path / "fieldkit" / "config" / "__init__.py")),
    ):
        _wizard_write_config(
            data_dir=data_dir,
            name="",
            email="",
            role="",
            company="",
        )

    # get_github_repo reads CONFIG_PATH — patch it to point to our written config.
    import fieldkit.config._loader as lib_cfg

    with (
        patch.object(lib_cfg, "CONFIG_PATH", config_path),
        pytest.raises(ConfigError, match="missing required key 'github_repo'"),
    ):
        get_github_repo()
