"""Canonical account reads retain the caller's selected workspace authority."""

from pathlib import Path
from unittest.mock import Mock

import pytest

import fieldkit.__main__ as main_module
from fieldkit.brief import collect as brief_collect
from fieldkit.config import (
    ConfigError,
    _accounts,
    _loader,
    _paths,
    clear_config_caches,
    get_accounts_config,
    get_config_path,
)
from fieldkit.pipeline import collect as pipeline_collect

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["../outside.yaml", "/outside.yaml", "nested\\identity.yaml"])
def test_explicit_configuration_name_cannot_redirect(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="confined"):
        get_config_path(name, workspace_root=tmp_path)


def test_explicit_identity_configuration_uses_canonical_workspace_path(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    result = get_config_path("identity.yaml", workspace_root=alias)
    assert result == root / "config" / "identity.yaml"
    assert not result.exists()
    assert not (root / "config").exists()


def write_accounts(root: Path, content: str) -> Path:
    path = root / "config/accounts.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_explicit_workspace_never_reads_configured_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default = write_accounts(tmp_path / "configured", "accounts:\n  default-corp: {}\n")
    selected = tmp_path / "selected"
    write_accounts(selected, "accounts:\n  selected-corp: {}\n")
    monkeypatch.setattr(_paths, "get_fieldkit_home", lambda: default.parent.parent)

    result = get_accounts_config(workspace_root=selected, strict=True)

    assert result == {"accounts": {"selected-corp": {}}}


def test_absent_explicit_workspace_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default = write_accounts(tmp_path / "configured", "accounts:\n  default-corp: {}\n")
    monkeypatch.setattr(_paths, "get_fieldkit_home", lambda: default.parent.parent)

    result = get_accounts_config(workspace_root=tmp_path / "absent", strict=True)

    assert result == {}


def test_explicit_workspace_reads_are_fresh(tmp_path: Path) -> None:
    path = write_accounts(tmp_path, "accounts:\n  first-corp: {}\n")
    assert get_accounts_config(workspace_root=tmp_path, strict=True) == {"accounts": {"first-corp": {}}}
    path.write_text("accounts:\n  second-corp: {}\n", encoding="utf-8")
    result = get_accounts_config(workspace_root=tmp_path, strict=True)
    assert result == {"accounts": {"second-corp": {}}}


def test_explicit_workspace_without_strict_still_validates(tmp_path: Path) -> None:
    write_accounts(tmp_path, "accounts: []\n")
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        get_accounts_config(workspace_root=tmp_path)


def test_default_account_cache_joins_canonical_invalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_accounts(tmp_path, "accounts:\n  first-corp: {}\n")
    monkeypatch.setattr(_accounts, "get_config_path", lambda _name: path)
    clear_config_caches()
    assert get_accounts_config() == {"accounts": {"first-corp": {}}}
    path.write_text("accounts:\n  second-corp: {}\n", encoding="utf-8")
    assert get_accounts_config() == {"accounts": {"first-corp": {}}}
    clear_config_caches()
    result = get_accounts_config()
    assert result == {"accounts": {"second-corp": {}}}


@pytest.mark.parametrize(
    "payload",
    [
        "- private-payload-sentinel\n",
        "accounts: []\n",
        "accounts:\n  private-payload-sentinel: []\n",
        "accounts:\n  private-payload-sentinel: {}\n  private-payload-sentinel: {}\n",
        "accounts:\n  selected-corp:\n    domains: []\n    domains: [private-payload-sentinel]\n",
    ],
)
def test_explicit_workspace_invalid_config_is_sanitized(tmp_path: Path, payload: str) -> None:
    write_accounts(tmp_path, payload)
    with pytest.raises(ConfigError, match=r"accounts\.yaml") as error:
        get_accounts_config(workspace_root=tmp_path, strict=True)
    assert "private-payload-sentinel" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_explicit_workspace_reader_retains_existing_byte_bound(tmp_path: Path) -> None:
    path = write_accounts(tmp_path, "accounts: {}\n")
    with path.open("wb") as stream:
        stream.truncate(_loader.MAX_CONFIG_UPDATE_BYTES + 1)
    with pytest.raises(ConfigError, match=r"accounts\.yaml") as error:
        get_accounts_config(workspace_root=tmp_path, strict=True)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("collector", ["brief", "pipeline"])
def test_account_collector_binds_strict_read_to_its_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector: str
) -> None:
    if collector == "brief":
        monkeypatch.setattr(brief_collect, "_gmail_db_exists", lambda: True)
        monkeypatch.setattr(brief_collect, "get_gmail_db_path", lambda: tmp_path / "gmail.db")
        monkeypatch.setattr(brief_collect, "_gmail_connect", Mock())
        reader = Mock(return_value={})
        monkeypatch.setattr(brief_collect, "get_accounts_config", reader, raising=False)
        result = brief_collect.collect_decay_signals(tmp_path)
        assert isinstance(result, str)
    else:
        reader = Mock(return_value={})
        monkeypatch.setattr(pipeline_collect, "get_accounts_config", reader, raising=False)
        blindspots = pipeline_collect.collect_blindspot_data(tmp_path)
        assert blindspots == []
    reader.assert_called_once_with(workspace_root=tmp_path, strict=True)


def test_pipeline_invalid_selected_accounts_stops_before_report_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_accounts(tmp_path, "accounts: []\n")
    monkeypatch.setattr(main_module, "load_dotenv_safe", lambda: None)
    monkeypatch.setattr(pipeline_collect, "get_gmail_db_path", lambda: tmp_path / "absent.db")
    result = main_module.main(["pipeline", "--data-root", str(tmp_path), "--no-llm"])
    assert result == 3
    assert "Invalid or unreadable accounts.yaml" in capsys.readouterr().err
    assert not list((tmp_path / "briefs").glob("*.md"))


@pytest.mark.parametrize("collector", ["brief", "pipeline"])
def test_account_collectors_propagate_invalid_selected_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector: str
) -> None:
    write_accounts(tmp_path, "accounts:\n  selected-corp: {}\n  selected-corp: {}\n")
    if collector == "brief":
        monkeypatch.setattr(brief_collect, "_gmail_db_exists", lambda: True)
        monkeypatch.setattr(brief_collect, "get_gmail_db_path", lambda: tmp_path / "gmail.db")
        connection = Mock()
        monkeypatch.setattr(brief_collect, "_gmail_connect", connection)
        with pytest.raises(ConfigError, match=r"accounts\.yaml"):
            brief_collect.collect_decay_signals(tmp_path)
        connection.assert_not_called()
    else:
        with pytest.raises(ConfigError, match=r"accounts\.yaml"):
            pipeline_collect.collect_blindspot_data(tmp_path)
