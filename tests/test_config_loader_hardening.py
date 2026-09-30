"""Tests for configuration integration accessors and loader contracts.

Covers:
- get_sf_rest_base_url(): Lightning transform, my.salesforce.com passthrough,
  unrecognised format raises ConfigError, empty returns "".
- get_sf_session_id(): routes through _load_raw_config() cache.
- get_fieldkit_home(): missing canonical workspace key fails closed.
- get_llm_model(): llm_model without vertex_ai/ prefix raises ConfigError.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# get_sf_rest_base_url()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_sf_rest_base_url_lightning_transforms() -> None:
    """Lightning URL should be transformed to my.salesforce.com."""
    from fieldkit.config._integrations import get_sf_rest_base_url
    from fieldkit.config._loader import clear_config_caches

    clear_config_caches()
    with (
        patch(
            "fieldkit.config._loader._load_raw_config",
            return_value={"sf_org_url": "https://acme.lightning.force.com"},
        ),
        patch(
            "fieldkit.config._accounts.get_salesforce_org_url",
            return_value="",
        ),
    ):
        result = get_sf_rest_base_url()
    assert result == "https://acme.my.salesforce.com"


@pytest.mark.unit
def test_get_sf_rest_base_url_my_salesforce_passthrough() -> None:
    """my.salesforce.com URL should pass through unchanged."""
    from fieldkit.config._integrations import get_sf_rest_base_url
    from fieldkit.config._loader import clear_config_caches

    clear_config_caches()
    with (
        patch(
            "fieldkit.config._loader._load_raw_config",
            return_value={"sf_org_url": "https://acme.my.salesforce.com"},
        ),
        patch(
            "fieldkit.config._accounts.get_salesforce_org_url",
            return_value="",
        ),
    ):
        result = get_sf_rest_base_url()
    assert result == "https://acme.my.salesforce.com"


@pytest.mark.unit
def test_get_sf_rest_base_url_unrecognised_raises() -> None:
    """Unrecognised URL format should raise ConfigError."""
    from fieldkit.config import ConfigError
    from fieldkit.config._integrations import get_sf_rest_base_url
    from fieldkit.config._loader import clear_config_caches

    clear_config_caches()
    with (
        patch(
            "fieldkit.config._loader._load_raw_config",
            return_value={"sf_org_url": "https://example.com"},
        ),
        patch(
            "fieldkit.config._accounts.get_salesforce_org_url",
            return_value="",
        ),
        pytest.raises(ConfigError, match=r"Unrecognised"),
    ):
        get_sf_rest_base_url()


@pytest.mark.unit
@pytest.mark.parametrize(
    "org_url",
    [
        "https://private-url-sentinel.example.com",
        "https://private-user-sentinel:private-password-sentinel@acme.my.salesforce.com",  # pii-guard: ignore — synthetic credential-userinfo rejection fixture
        "https://[private-url-sentinel",
    ],
    ids=["unsupported-host", "credential-userinfo", "malformed-address"],
)
def test_invalid_sf_org_url_has_fixed_cli_diagnostic(
    org_url: str, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.config._loader import clear_config_caches

    clear_config_caches()
    with (
        patch("fieldkit.__main__.load_dotenv_safe"),
        patch("fieldkit.config._loader._load_raw_config", return_value={"sf_org_url": org_url}),
        patch("fieldkit.commands.sf.schema.get_sf_session_id", return_value="fictional-session"),
        patch("fieldkit.commands.sf.schema.SFDirectClient") as client,
    ):
        result = main(["sf", "schema", "Account", "--record-id", "001000000000001"])

    assert result == 3
    captured = capsys.readouterr()
    assert "Unrecognised Salesforce org URL format" in captured.err
    assert "private-" not in captured.out + captured.err + caplog.text
    assert "Traceback" not in captured.err
    client.assert_not_called()


@pytest.mark.unit
def test_get_sf_rest_base_url_empty_returns_empty() -> None:
    """Empty sf_org_url should return empty string (no config set)."""
    from fieldkit.config._integrations import get_sf_rest_base_url
    from fieldkit.config._loader import clear_config_caches

    clear_config_caches()
    with (
        patch(
            "fieldkit.config._loader._load_raw_config",
            return_value={},
        ),
        patch(
            "fieldkit.config._accounts.get_salesforce_org_url",
            return_value="",
        ),
    ):
        result = get_sf_rest_base_url()
    assert result == ""


@pytest.mark.unit
def test_get_sf_rest_base_url_trailing_slash_stripped() -> None:
    """Trailing slash should be stripped from my.salesforce.com URLs."""
    from fieldkit.config._integrations import get_sf_rest_base_url
    from fieldkit.config._loader import clear_config_caches

    clear_config_caches()
    with (
        patch(
            "fieldkit.config._loader._load_raw_config",
            return_value={"sf_org_url": "https://acme.my.salesforce.com/"},
        ),
        patch(
            "fieldkit.config._accounts.get_salesforce_org_url",
            return_value="",
        ),
    ):
        result = get_sf_rest_base_url()
    assert result == "https://acme.my.salesforce.com"


# ---------------------------------------------------------------------------
# get_sf_session_id() — routes through _load_raw_config() cache
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_sf_session_id_uses_load_raw_config() -> None:
    """get_sf_session_id() should use _load_raw_config() (cacheable)."""
    from fieldkit.config._integrations import get_sf_session_id
    from fieldkit.config._loader import clear_config_caches

    clear_config_caches()
    with patch(
        "fieldkit.config._loader._load_raw_config",
        return_value={"sf_session_id": "test-sid-xyz"},
    ):
        result = get_sf_session_id()
    assert result == "test-sid-xyz"


@pytest.mark.unit
def test_get_sf_session_id_falls_back_to_cookie_file(tmp_path: Path) -> None:
    """get_sf_session_id() falls back to cookie file when config has no sf_session_id."""
    import json

    from fieldkit.config._integrations import get_sf_session_id
    from fieldkit.config._loader import clear_config_caches

    clear_config_caches()
    cookie_file = tmp_path / "sf-cookies.json"
    cookie_data = {
        "cookies": [
            {
                "name": "sid",
                "domain": "my.salesforce.com",
                "value": "cookie-sid-abc",
            }
        ]
    }
    cookie_file.write_text(json.dumps(cookie_data), encoding="utf-8")

    with (
        patch(
            "fieldkit.config._loader._load_raw_config",
            return_value={},
        ),
        patch(
            "fieldkit.config._integrations.get_cookie_file",
            return_value=cookie_file,
        ),
    ):
        result = get_sf_session_id()
    assert result == "cookie-sid-abc"


@pytest.mark.unit
def test_get_cookie_file_follows_config_directory(tmp_path: Path) -> None:
    """Salesforce cookies stay beside the selected global config."""
    import fieldkit.config._integrations as integrations_config
    import fieldkit.config._loader as config_loader

    with patch.object(config_loader, "CONFIG_PATH", tmp_path / "config" / "config.yaml"):
        result = integrations_config.get_cookie_file()

    assert result == tmp_path / "config" / "sf-cookies.json"


# ---------------------------------------------------------------------------
# get_fieldkit_home() — canonical workspace key is required
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_noncanonical_workspace_key_is_not_an_alias() -> None:
    from fieldkit.config import ConfigError
    from fieldkit.config._loader import clear_config_caches
    from fieldkit.config._paths import get_fieldkit_home

    clear_config_caches()
    with (
        patch(
            "fieldkit.config._loader._load_raw_config_uncached",
            return_value={"data_repo": "/tmp/test-fieldkit-home"},
        ),
        pytest.raises(ConfigError, match="missing required key 'fieldkit_home'"),
    ):
        get_fieldkit_home()


# ---------------------------------------------------------------------------
# get_llm_model() — G3c: vertex_ai/ prefix validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_model_without_prefix_raises_config_error() -> None:
    """llm_model without vertex_ai/ prefix must raise ConfigError at config load."""
    from fieldkit.config import ConfigError
    from fieldkit.config._loader import clear_config_caches
    from fieldkit.config._settings import get_llm_model

    clear_config_caches()
    with (
        patch(
            "fieldkit.config._loader._load_raw_config",
            return_value={"llm_model": "claude-sonnet-4-6"},
        ),
        pytest.raises(ConfigError, match=r"vertex_ai/"),
    ):
        get_llm_model()


@pytest.mark.unit
def test_llm_model_with_prefix_returns_value() -> None:
    """llm_model with vertex_ai/ prefix should be returned as-is."""
    from fieldkit.config._loader import clear_config_caches
    from fieldkit.config._settings import get_llm_model

    clear_config_caches()
    with patch(
        "fieldkit.config._loader._load_raw_config",
        return_value={"llm_model": "vertex_ai/claude-sonnet-4-6"},
    ):
        result = get_llm_model()
    assert result == "vertex_ai/claude-sonnet-4-6"


@pytest.mark.unit
def test_llm_model_absent_returns_none() -> None:
    """Absent llm_model key should return None (not raise)."""
    from fieldkit.config._loader import clear_config_caches
    from fieldkit.config._settings import get_llm_model

    clear_config_caches()
    with patch(
        "fieldkit.config._loader._load_raw_config",
        return_value={},
    ):
        result = get_llm_model()
    assert result is None


# ---------------------------------------------------------------------------
# get_mcp_gateway_url() — historic regression
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mcp_gateway_base_compatibility_alias_is_removed() -> None:
    """The public config surface exposes only the canonical gateway accessor."""
    import fieldkit.config

    assert not hasattr(fieldkit.config, "get_mcp_gateway_base")


@pytest.mark.unit
def test_get_mcp_gateway_url_returns_default() -> None:
    """Without env var, the hardcoded default is returned."""
    import os

    from fieldkit.config._integrations import get_mcp_gateway_url

    env = {k: v for k, v in os.environ.items() if k != "FIELDKIT_MCP_GATEWAY_URL"}
    with patch.dict(os.environ, env, clear=True):
        result = get_mcp_gateway_url()
    assert result == "http://127.0.0.1:8080"


@pytest.mark.unit
def test_get_mcp_gateway_url_env_override() -> None:
    """FIELDKIT_MCP_GATEWAY_URL env var overrides the default."""
    import os

    from fieldkit.config._integrations import get_mcp_gateway_url

    with patch.dict(os.environ, {"FIELDKIT_MCP_GATEWAY_URL": "http://localhost:9090"}, clear=False):
        result = get_mcp_gateway_url()
    assert result == "http://localhost:9090"


@pytest.mark.unit
def test_get_mcp_gateway_url_strips_trailing_slash() -> None:
    """Trailing slash is stripped from the env var value."""
    import os

    from fieldkit.config._integrations import get_mcp_gateway_url

    with patch.dict(os.environ, {"FIELDKIT_MCP_GATEWAY_URL": "http://localhost:9090/"}, clear=False):
        result = get_mcp_gateway_url()
    assert result == "http://localhost:9090"


@pytest.mark.unit
def test_get_mcp_gateway_url_empty_env_uses_default() -> None:
    """An empty FIELDKIT_MCP_GATEWAY_URL falls back to the hardcoded default."""
    import os

    from fieldkit.config._integrations import get_mcp_gateway_url

    with patch.dict(os.environ, {"FIELDKIT_MCP_GATEWAY_URL": ""}, clear=False):
        result = get_mcp_gateway_url()
    assert result == "http://127.0.0.1:8080"
