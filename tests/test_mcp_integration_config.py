"""Typed configuration contracts for optional MCP-backed workflows."""

from collections.abc import Generator
from pathlib import Path

import pytest

import fieldkit.config._loader as config_loader
from fieldkit.config import ConfigError, McpEndpointName, clear_config_caches, get_mcp_endpoint

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_config_cache() -> Generator[None, None, None]:
    clear_config_caches()
    yield
    clear_config_caches()


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)


def test_mcp_endpoint_is_disabled_when_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, monkeypatch, "fieldkit_home: /tmp/fieldkit-example\n")

    assert get_mcp_endpoint("backstory") is None


@pytest.mark.parametrize("name", ["backstory", "calendar", "draft_queue"])
def test_mcp_endpoint_returns_exact_operator_supplied_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: McpEndpointName
) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        f"mcp_endpoints:\n  {name}: https://gateway.example.com/custom/{name}/\n",
    )

    assert get_mcp_endpoint(name) == f"https://gateway.example.com/custom/{name}"


@pytest.mark.parametrize(
    "endpoint",
    [
        "gateway.example.com/mcp",
        "ftp://gateway.example.com/mcp",
        "https://user:secret@gateway.example.com/mcp",
        "https://gateway.example.com/mcp?token=secret",
        "https://gateway.example.com/mcp#fragment",
    ],
)
def test_mcp_endpoint_rejects_unsafe_or_ambiguous_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    _write_config(tmp_path, monkeypatch, f"mcp_endpoints:\n  backstory: {endpoint}\n")

    with pytest.raises(ConfigError, match=r"mcp_endpoints\.backstory"):
        get_mcp_endpoint("backstory")


def test_mcp_endpoint_rejects_invalid_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, monkeypatch, "mcp_endpoints:\n  backstory: https://gateway.example.com:not-a-port/mcp\n")

    with pytest.raises(ConfigError, match=r"mcp_endpoints\.backstory"):
        get_mcp_endpoint("backstory")


def test_mcp_endpoint_schema_rejects_unknown_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(
        tmp_path,
        monkeypatch,
        "mcp_endpoints:\n  private_sales_alias: https://gateway.example.com/mcp\n",
    )

    with pytest.raises(ConfigError, match="invalid values"):
        get_mcp_endpoint("backstory")
