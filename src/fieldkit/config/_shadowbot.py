"""Private configuration accessors for the ShadowBot integration."""

import logging
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from fieldkit.config import _loader

logger = logging.getLogger(__name__)

_SHADOWBOT_DEFAULT_ASSISTANT_ID = "sales_assistant_v2"
SHADOWBOT_DEFAULT_CLIENT_ID = "shadowbot-ui"


def get_shadowbot_assistant_id() -> str:
    """Return the configured ShadowBot assistant ID or its product default."""
    data = _loader._load_raw_config()
    if data is None:
        return _SHADOWBOT_DEFAULT_ASSISTANT_ID
    shadowbot_config = data.get("shadowbot")
    if not isinstance(shadowbot_config, dict):
        return _SHADOWBOT_DEFAULT_ASSISTANT_ID
    assistant_id = shadowbot_config.get("assistant_id")
    if not isinstance(assistant_id, str) or not assistant_id.strip():
        return _SHADOWBOT_DEFAULT_ASSISTANT_ID
    value = assistant_id.strip()
    logger.debug("shadowbot: assistant_id=%s (from config)", value)
    return value


def _get_shadowbot_str_key(key: str, default: str | None = None) -> str:
    """Return a nonblank ShadowBot setting or its explicit default."""
    data = _loader._load_raw_config()
    shadowbot_config = data.get("shadowbot") if data is not None else None
    if isinstance(shadowbot_config, dict):
        value = shadowbot_config.get(key)
        if isinstance(value, str) and value.strip():
            logger.debug("shadowbot: %s=%s (from config)", key, value.strip())
            return value.strip()
    if default is not None:
        return default
    raise _loader.ConfigError(
        f"shadowbot.{key} is not configured. Add a 'shadowbot:' section with '{key}:' to config.yaml."
    )


def _get_public_https_url(key: str) -> str:
    """Return a public HTTPS integration URL that cannot carry credentials."""
    raw = _get_shadowbot_str_key(key)
    try:
        parsed = urlsplit(raw)
        port_is_safe = parsed.port in (None, 443)
    except ValueError:
        parsed = None
        port_is_safe = False
    hostname = parsed.hostname if parsed is not None else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or hostname is None
        or not parsed.path
        or not port_is_safe
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _is_loopback_or_private_host(hostname)
    ):
        raise _loader.ConfigError(
            f"shadowbot.{key} must be a public HTTPS URL with a path and no credentials, query, or fragment."
        )
    return parsed.geturl()


def get_shadowbot_api_base() -> str:
    """Return a safe HTTPS ShadowBot API base before it receives bearer tokens."""
    return _get_public_https_url("api_base").rstrip("/")


def _is_loopback_or_private_host(hostname: str) -> bool:
    """Return whether a literal host points at local or non-public address space."""
    if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        return True
    try:
        address = ip_address(hostname)
    except ValueError:
        return False
    return not address.is_global


def get_shadowbot_token_endpoint() -> str:
    """Return the required ShadowBot token endpoint URL."""
    return _get_shadowbot_str_key("token_endpoint")


def get_shadowbot_redirect_uri() -> str:
    """Return the safe redirect URI registered for the configured OAuth client."""
    return _get_public_https_url("redirect_uri")


def get_shadowbot_auth_endpoint() -> str:
    """Return the required ShadowBot authorization endpoint URL."""
    return _get_shadowbot_str_key("auth_endpoint")


def get_shadowbot_client_id() -> str:
    """Return the configured ShadowBot OAuth client ID or its product default."""
    return _get_shadowbot_str_key("client_id", SHADOWBOT_DEFAULT_CLIENT_ID)


def get_shadowbot_chrome_cookies_path() -> Path | None:
    """Return the configured Chrome cookie path, if one is provided."""
    data = _loader._load_raw_config()
    if data is None:
        return None
    shadowbot_config = data.get("shadowbot")
    if not isinstance(shadowbot_config, dict):
        return None
    value = shadowbot_config.get("chrome_cookies_path")
    if not isinstance(value, str) or not value.strip():
        return None
    resolved = Path(value.strip()).expanduser()
    logger.debug("shadowbot: chrome_cookies_path=%s (from config)", resolved)
    return resolved
