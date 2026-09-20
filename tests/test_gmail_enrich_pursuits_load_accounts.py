"""Tests for fieldkit.commands.gmail.enrich_pursuits.load_accounts.

Covers the get_account_names()-first, accounts.yaml-fallback logic:
1. get_account_names() short-circuits when it returns a non-empty list.
2. Falls back to reading CONFIG_PATH via yaml when get_account_names() is empty.
3. Falls back to [] when CONFIG_PATH does not exist.
4. Falls back to [] without opening the file when yaml is unavailable.
5. Falls back to [] when the config file has no "accounts" key.
"""

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _make_config_file(tmp_path: Path, content: str) -> Path:
    """Write a YAML config file under tmp_path and return its path."""
    config_path = tmp_path / "accounts.yaml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def _load_accounts() -> list[str]:
    """Call the currently registered module after lazy-import tests replace it."""
    module = importlib.import_module("fieldkit.commands.gmail.enrich_pursuits")
    return module.load_accounts()


@pytest.mark.unit
def test_load_accounts_returns_get_account_names_without_touching_config(tmp_path: Path) -> None:
    """When get_account_names() returns a non-empty list, it is returned directly."""
    # This file contains DIFFERENT accounts than get_account_names() below — if
    # load_accounts ever read it, the returned list would include "should-not-be-read".
    decoy_config = _make_config_file(tmp_path, "accounts:\n  should-not-be-read: {}\n")

    with (
        patch(
            "fieldkit.commands.gmail.enrich_pursuits.get_account_names",
            return_value=["acme-corp", "globalpay"],
        ),
        patch("fieldkit.commands.gmail.enrich_pursuits.CONFIG_PATH", str(decoy_config)),
        patch("pathlib.Path.exists") as mock_exists,
    ):
        result = _load_accounts()

    assert result == ["acme-corp", "globalpay"]
    mock_exists.assert_not_called()


@pytest.mark.unit
def test_load_accounts_falls_back_to_yaml_config_file(tmp_path: Path) -> None:
    """When get_account_names() is empty, accounts are read from CONFIG_PATH in file order."""
    config_path = _make_config_file(
        tmp_path,
        "accounts:\n  zeta-corp: {}\n  acme-corp: {}\n  beta-inc: {}\n",
    )

    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.get_account_names", return_value=[]),
        patch("fieldkit.commands.gmail.enrich_pursuits.CONFIG_PATH", str(config_path)),
    ):
        result = _load_accounts()

    assert result == ["zeta-corp", "acme-corp", "beta-inc"]


@pytest.mark.unit
def test_load_accounts_returns_empty_when_config_path_missing(tmp_path: Path) -> None:
    """When get_account_names() is empty and CONFIG_PATH does not exist, returns []."""
    missing_path = tmp_path / "does-not-exist.yaml"

    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.get_account_names", return_value=[]),
        patch("fieldkit.commands.gmail.enrich_pursuits.CONFIG_PATH", str(missing_path)),
    ):
        result = _load_accounts()

    assert result == []


@pytest.mark.unit
def test_load_accounts_returns_empty_when_yaml_unavailable(tmp_path: Path) -> None:
    """When yaml is unavailable, returns [] without opening CONFIG_PATH even if it exists."""
    config_path = _make_config_file(tmp_path, "accounts:\n  acme-corp: {}\n")

    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.get_account_names", return_value=[]),
        patch("fieldkit.commands.gmail.enrich_pursuits.CONFIG_PATH", str(config_path)),
        patch("fieldkit.commands.gmail.enrich_pursuits.yaml", None),
        patch("pathlib.Path.open") as mock_open,
    ):
        result = _load_accounts()

    assert result == []
    mock_open.assert_not_called()


@pytest.mark.unit
def test_load_accounts_returns_empty_when_accounts_key_missing(tmp_path: Path) -> None:
    """When the config file has no 'accounts' key, cfg.get default avoids a KeyError."""
    config_path = _make_config_file(tmp_path, "other_key: value\n")

    with (
        patch("fieldkit.commands.gmail.enrich_pursuits.get_account_names", return_value=[]),
        patch("fieldkit.commands.gmail.enrich_pursuits.CONFIG_PATH", str(config_path)),
    ):
        result = _load_accounts()

    assert result == []
