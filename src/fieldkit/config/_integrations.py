"""Private configuration accessors for external integrations."""

import os
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

from fieldkit.config import _accounts, _loader
from fieldkit.config._paths import get_fieldkit_data
from fieldkit.config.salesforce_cookie import read_salesforce_cookie

IntegrationConfigurationState: TypeAlias = Literal["disabled", "enabled", "invalid"]
McpEndpointName: TypeAlias = Literal["backstory", "calendar", "draft_queue"]

_SF_URL_FORMAT_ERROR = (
    "Unrecognised Salesforce org URL format. Expected .lightning.force.com or .my.salesforce.com HTTPS URL."
)

#: All Google API scopes required by fieldkit's Python code (non-MCP paths).
#: Single auth flow → single token file → all fieldkit commands work.
#: Documents and Drive are read/write to support fieldkit meeting workflows.
GOOGLE_OAUTH_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


@_loader._config_cache
def get_google_token_path() -> Path:
    """Return the configured or default unified Google OAuth token path."""
    data = _loader._load_raw_config()
    if data is not None and "gmail_token" in data:
        raw = str(data["gmail_token"]).strip()
        if not raw:
            raise _loader.ConfigError("Config key 'gmail_token' must not be empty or whitespace")
        return Path(raw).expanduser().resolve()
    return get_fieldkit_data() / "google-oauth-token.json"


def _normalize_sf_rest_url(raw: str) -> str:
    """Validate and normalize one explicitly configured Salesforce HTTPS URL."""
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname or ""
    except ValueError:
        raise _loader.ConfigError(_SF_URL_FORMAT_ERROR) from None
    valid_shape = (
        parsed.scheme == "https"
        and bool(hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )
    if valid_shape and hostname.endswith(".lightning.force.com"):
        instance = hostname.removesuffix(".lightning.force.com")
        return f"https://{instance}.my.salesforce.com"
    if valid_shape and (hostname.endswith(".my.salesforce.com") or hostname.endswith(".salesforce.com")):
        return f"https://{hostname}"
    raise _loader.ConfigError(_SF_URL_FORMAT_ERROR) from None


def get_sf_rest_base_url() -> str:
    """Return the REST-compatible Salesforce base URL from either config source."""
    raw_config = _loader._load_raw_config()
    raw = str(raw_config.get("sf_org_url", "")).strip() if raw_config else ""
    if not raw:
        raw = _accounts.get_salesforce_org_url().rstrip("/")
    return _normalize_sf_rest_url(raw) if raw else ""


def resolve_oauth_credentials() -> tuple[str | None, str | None]:
    """Resolve the canonical Google OAuth client settings."""
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or None, os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or None


def get_integration_configuration_state(
    service: Literal["sf", "google", "shadowbot"],
) -> IntegrationConfigurationState:
    """Return whether an integration is absent, selected, or incomplete."""
    loaded = _loader._load_raw_config()
    if loaded is None and _loader.CONFIG_PATH.exists():
        return "invalid"
    data = loaded or {}
    if service == "sf":
        try:
            return "enabled" if get_sf_rest_base_url() else "disabled"
        except _loader.ConfigError:
            return "invalid"

    if service == "google":
        if "gmail_token" in data:
            value = data["gmail_token"]
            return "enabled" if isinstance(value, str) and value.strip() else "invalid"
        client_id, client_secret = resolve_oauth_credentials()
        if bool(client_id) != bool(client_secret):
            return "invalid"
        return "enabled" if client_id else "disabled"

    shadowbot = data.get("shadowbot")
    if shadowbot is None:
        return "disabled"
    if not isinstance(shadowbot, dict) or not shadowbot:
        return "invalid"
    required = ("api_base", "token_endpoint", "auth_endpoint")
    complete = all(isinstance(shadowbot.get(key), str) and str(shadowbot[key]).strip() for key in required)
    return "enabled" if complete else "invalid"


def get_cookie_file() -> Path:
    """Return the canonical path to the Salesforce browser cookie file."""
    return _loader.CONFIG_PATH.parent / "sf-cookies.json"


def get_sf_session_id() -> str | None:
    """Return the configured or browser-cookie Salesforce session ID."""
    raw_config = _loader._load_raw_config()
    if raw_config is not None:
        sid_from_config = raw_config.get("sf_session_id")
        if isinstance(sid_from_config, str) and sid_from_config.strip():
            return sid_from_config.strip()

    cookie_path = get_cookie_file()
    try:
        cookie = read_salesforce_cookie(cookie_path)
    except (ValueError, OSError):
        return None
    return cookie.sid if cookie is not None else None


_MCP_GATEWAY_DEFAULT_BASE = "http://127.0.0.1:8080"


def _load_mcp_gateway_config(*, strict: bool) -> dict[str, object] | None:
    """Select the fresh strict or cached permissive config read."""
    if strict:
        return _loader._load_raw_config_uncached(strict=True)
    try:
        return _loader._load_raw_config()
    except _loader.ConfigError:
        return None


def get_mcp_gateway_url(*, strict: bool = False) -> str:
    """Return the configured MCP gateway URL, then the environment fallback or default."""
    data = _load_mcp_gateway_config(strict=strict)
    if data is not None:
        value = data.get("mcp_gateway_url", "")
        if value and isinstance(value, str):
            return value.strip().rstrip("/")

    environment_value = os.environ.get("FIELDKIT_MCP_GATEWAY_URL", "").strip().rstrip("/")
    return environment_value or _MCP_GATEWAY_DEFAULT_BASE


def _normalize_mcp_endpoint(name: McpEndpointName, raw: object) -> str:
    """Validate one explicit MCP endpoint without retaining credentials."""
    if not isinstance(raw, str) or not raw.strip():
        raise _loader.ConfigError(f"Config key 'mcp_endpoints.{name}' must be a non-empty URL")
    value = raw.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port_is_valid = parsed.port is None or 0 < parsed.port <= 65535
    except ValueError:
        raise _loader.ConfigError(f"Config key 'mcp_endpoints.{name}' must be a valid http(s) URL") from None
    valid = (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and port_is_valid
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
    if not valid:
        raise _loader.ConfigError(
            f"Config key 'mcp_endpoints.{name}' must be an http(s) URL without credentials, query, or fragment"
        )
    return value


def get_mcp_endpoint(name: McpEndpointName) -> str | None:
    """Return an explicitly configured full endpoint for one optional workflow."""
    data = _loader._load_raw_config()
    endpoints = data.get("mcp_endpoints") if data is not None else None
    if endpoints is None:
        return None
    if not isinstance(endpoints, dict):
        raise _loader.ConfigError("Config key 'mcp_endpoints' must be a mapping")
    raw = endpoints.get(name)
    return None if raw is None else _normalize_mcp_endpoint(name, raw)
