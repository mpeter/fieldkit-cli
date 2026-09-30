"""Ownership-sensitive accounts configuration never uses an error fallback."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.config import ConfigError, clear_config_caches, get_accounts_config

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_cache() -> Iterator[None]:
    clear_config_caches()
    yield
    clear_config_caches()


@pytest.mark.parametrize("contents", ["accounts: [\n", "- not-a-mapping\n", ""])
def test_strict_accounts_rejects_corrupt_input(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(contents, encoding="utf-8")
    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=path),
        pytest.raises(ConfigError, match=r"accounts\.yaml") as caught,
    ):
        get_accounts_config(strict=True)
    assert str(tmp_path) not in str(caught.value)


def test_strict_accounts_read_error_is_not_empty_config(tmp_path: Path) -> None:
    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=tmp_path / "accounts.yaml"),
        patch("fieldkit.config._loader.read_text_snapshot", side_effect=PermissionError("private path")),
        pytest.raises(ConfigError, match=r"Invalid or unreadable accounts\.yaml") as caught,
    ):
        get_accounts_config(strict=True)
    assert "private path" not in str(caught.value)


def test_strict_accounts_allows_missing_file(tmp_path: Path) -> None:
    with patch("fieldkit.config._accounts.get_config_path", return_value=tmp_path / "missing.yaml"):
        result = get_accounts_config(strict=True)
    assert result == {}


def test_strict_accounts_rejects_unavailable_location() -> None:
    with (
        patch("fieldkit.config._accounts.get_config_path", side_effect=ConfigError("private location")),
        pytest.raises(ConfigError, match=r"Cannot locate accounts\.yaml") as caught,
    ):
        get_accounts_config(strict=True)
    assert "private location" not in str(caught.value)


def test_strict_accounts_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_bytes(b"\xff")
    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=path),
        pytest.raises(ConfigError, match=r"Invalid or unreadable accounts\.yaml"),
    ):
        get_accounts_config(strict=True)


def test_strict_accounts_does_not_reuse_permissive_fallback(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text("invalid: [", encoding="utf-8")
    with patch("fieldkit.config._accounts.get_config_path", return_value=path):
        assert get_accounts_config() == {}
        with pytest.raises(ConfigError, match=r"Invalid or unreadable accounts\.yaml"):
            get_accounts_config(strict=True)
        path.write_text("accounts: {acme: {}}", encoding="utf-8")
        assert get_accounts_config(strict=True) == {"accounts": {"acme": {}}}
