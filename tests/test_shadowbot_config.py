"""Unit tests for ShadowBot config accessor functions in config/_shadowbot.py (implementation note).

Uses monkeypatch on _load_raw_config directly to bypass @cache.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.config import (
    get_shadowbot_api_base,
    get_shadowbot_assistant_id,
    get_shadowbot_auth_endpoint,
    get_shadowbot_chrome_cookies_path,
    get_shadowbot_client_id,
    get_shadowbot_redirect_uri,
    get_shadowbot_token_endpoint,
)
from fieldkit.config._loader import ConfigError

pytestmark = pytest.mark.unit

_LOADER_MODULE = "fieldkit.config._loader"


# ---------------------------------------------------------------------------
# No config → defaults returned
# ---------------------------------------------------------------------------


def test_get_shadowbot_api_base_raises_when_no_config() -> None:
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None),
        pytest.raises(ConfigError, match=r"shadowbot\.api_base"),
    ):
        get_shadowbot_api_base()


def test_get_shadowbot_token_endpoint_raises_when_no_config() -> None:
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None),
        pytest.raises(ConfigError, match=r"shadowbot\.token_endpoint"),
    ):
        get_shadowbot_token_endpoint()


def test_get_shadowbot_auth_endpoint_raises_when_no_config() -> None:
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None),
        pytest.raises(ConfigError, match=r"shadowbot\.auth_endpoint"),
    ):
        get_shadowbot_auth_endpoint()


def test_get_shadowbot_redirect_uri_raises_when_no_config() -> None:
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None),
        pytest.raises(ConfigError, match=r"shadowbot\.redirect_uri"),
    ):
        get_shadowbot_redirect_uri()


def test_get_shadowbot_client_id_returns_default_when_no_config() -> None:
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None):
        assert get_shadowbot_client_id() == "shadowbot-ui"  # generic ShadowBot product default


# ---------------------------------------------------------------------------
# Custom config → configured value returned
# ---------------------------------------------------------------------------


def test_get_shadowbot_api_base_returns_configured_value() -> None:
    config = {"shadowbot": {"api_base": "https://staging.example.com/sales-assistant"}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        assert get_shadowbot_api_base() == "https://staging.example.com/sales-assistant"


@pytest.mark.parametrize(
    "api_base",
    [
        "http://api.example.com/sales-assistant",
        "https://api.example.com:8443/sales-assistant",
        "https://user:password@api.example.com/sales-assistant",
        "https://api.example.com/sales-assistant?redirect=https://elsewhere.example",
        "https://localhost/sales-assistant",
        "https://127.0.0.1/sales-assistant",
    ],
)
def test_get_shadowbot_api_base_rejects_unsafe_destination(api_base: str) -> None:
    """A configured API base must not direct bearer tokens to an unsafe origin."""
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"shadowbot": {"api_base": api_base}}),
        pytest.raises(ConfigError, match="HTTPS"),
    ):
        get_shadowbot_api_base()


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://assistant.example.com/oauth/callback",
        "https://assistant.example.com:8443/oauth/callback",
        "https://user:password@assistant.example.com/oauth/callback",
        "https://assistant.example.com/oauth/callback?code=unexpected",
        "https://localhost/oauth/callback",
        "https://127.0.0.1/oauth/callback",
    ],
)
def test_get_shadowbot_redirect_uri_rejects_unsafe_destination(redirect_uri: str) -> None:
    """An OAuth redirect URI must be a safe configured integration URL."""
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"shadowbot": {"redirect_uri": redirect_uri}}),
        pytest.raises(ConfigError, match="public HTTPS"),
    ):
        get_shadowbot_redirect_uri()


def test_get_shadowbot_client_id_returns_configured_value() -> None:
    config = {"shadowbot": {"client_id": "custom-client-id"}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        assert get_shadowbot_client_id() == "custom-client-id"


# ---------------------------------------------------------------------------
# get_shadowbot_assistant_id() is unchanged
# ---------------------------------------------------------------------------


def test_get_shadowbot_assistant_id_unchanged() -> None:
    """get_shadowbot_assistant_id() existed before implementation note and must still work."""
    config = {"shadowbot": {"assistant_id": "sales_assistant_v3"}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        assert get_shadowbot_assistant_id() == "sales_assistant_v3"


def test_get_shadowbot_assistant_id_returns_default_when_absent() -> None:
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None):
        assert get_shadowbot_assistant_id() == "sales_assistant_v2"


# ---------------------------------------------------------------------------
# get_shadowbot_chrome_cookies_path()
# ---------------------------------------------------------------------------


def test_get_shadowbot_chrome_cookies_path_returns_none_when_no_config() -> None:
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None):
        assert get_shadowbot_chrome_cookies_path() is None


def test_get_shadowbot_chrome_cookies_path_returns_none_when_no_shadowbot_section() -> None:
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"fieldkit_home": "/tmp/home"}):
        assert get_shadowbot_chrome_cookies_path() is None


def test_get_shadowbot_chrome_cookies_path_returns_none_when_key_absent() -> None:
    config = {"shadowbot": {"api_base": "https://example.com"}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        assert get_shadowbot_chrome_cookies_path() is None


def test_get_shadowbot_chrome_cookies_path_returns_none_when_empty_string() -> None:
    config = {"shadowbot": {"chrome_cookies_path": "   "}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        assert get_shadowbot_chrome_cookies_path() is None


def test_get_shadowbot_chrome_cookies_path_returns_path_when_configured(tmp_path: Path) -> None:
    cookies_file = tmp_path / "Cookies"
    config = {"shadowbot": {"chrome_cookies_path": str(cookies_file)}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        result = get_shadowbot_chrome_cookies_path()
        assert result == cookies_file


def test_get_shadowbot_chrome_cookies_path_expands_home() -> None:
    config = {"shadowbot": {"chrome_cookies_path": "~/.config/google-chrome/Default/Cookies"}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        result = get_shadowbot_chrome_cookies_path()
        assert result is not None
        assert not str(result).startswith("~")
        assert "google-chrome" in str(result)


def test_get_shadowbot_chrome_cookies_path_returns_none_when_non_string_value() -> None:
    config = {"shadowbot": {"chrome_cookies_path": 42}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        assert get_shadowbot_chrome_cookies_path() is None


# ---------------------------------------------------------------------------
# org-agnostic-config: task 5.4 — endpoint accessors raise ConfigError
# ---------------------------------------------------------------------------


def test_get_shadowbot_api_base_raises_when_section_absent() -> None:
    """ConfigError is raised when the shadowbot: section is entirely absent."""
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"fieldkit_home": "/tmp"}),
        pytest.raises(ConfigError, match=r"shadowbot\.api_base"),
    ):
        get_shadowbot_api_base()


def test_get_shadowbot_token_endpoint_raises_when_section_absent() -> None:
    """ConfigError is raised when the shadowbot: section is entirely absent."""
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"fieldkit_home": "/tmp"}),
        pytest.raises(ConfigError, match=r"shadowbot\.token_endpoint"),
    ):
        get_shadowbot_token_endpoint()


def test_get_shadowbot_auth_endpoint_raises_when_section_absent() -> None:
    """ConfigError is raised when the shadowbot: section is entirely absent."""
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value={"fieldkit_home": "/tmp"}),
        pytest.raises(ConfigError, match=r"shadowbot\.auth_endpoint"),
    ):
        get_shadowbot_auth_endpoint()


def test_get_shadowbot_api_base_raises_when_sub_key_absent() -> None:
    """ConfigError is raised when section exists but api_base sub-key is absent."""
    config = {"shadowbot": {"client_id": "shadowbot-ui"}}
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config),
        pytest.raises(ConfigError, match=r"shadowbot\.api_base"),
    ):
        get_shadowbot_api_base()


def test_get_shadowbot_token_endpoint_raises_when_sub_key_absent() -> None:
    """ConfigError is raised when section exists but token_endpoint sub-key is absent."""
    config = {"shadowbot": {"client_id": "shadowbot-ui"}}
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config),
        pytest.raises(ConfigError, match=r"shadowbot\.token_endpoint"),
    ):
        get_shadowbot_token_endpoint()


def test_get_shadowbot_auth_endpoint_raises_when_sub_key_absent() -> None:
    """ConfigError is raised when section exists but auth_endpoint sub-key is absent."""
    config = {"shadowbot": {"client_id": "shadowbot-ui"}}
    with (
        patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config),
        pytest.raises(ConfigError, match=r"shadowbot\.auth_endpoint"),
    ):
        get_shadowbot_auth_endpoint()


def test_get_shadowbot_client_id_returns_default_when_absent() -> None:
    """client_id still returns default when absent (retains default per task 3.2)."""
    config = {"shadowbot": {"api_base": "https://example.com"}}
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=config):
        assert get_shadowbot_client_id() == "shadowbot-ui"


def test_get_shadowbot_assistant_id_returns_default_when_section_absent() -> None:
    """assistant_id still returns default when absent (retains default per task 3.2)."""
    with patch(f"{_LOADER_MODULE}._load_raw_config", return_value=None):
        assert get_shadowbot_assistant_id() == "sales_assistant_v2"
