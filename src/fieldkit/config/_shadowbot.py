"""Private configuration accessors for the ShadowBot integration."""

import difflib
import logging
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from fieldkit.config import _loader
from fieldkit.config._schema import _ShadowbotConfig

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


def get_shadowbot_chrome_recovery_enabled() -> bool:
    """Return whether the operator opted in to reading Chrome cookies to recover a rejected session.

    Recovery decrypts the local Chrome cookie store and sends the login domain's
    session cookies to the authorization endpoint, so it stays off unless
    ``shadowbot.chrome_recovery`` is exactly ``true``.
    """
    data = _loader._load_raw_config()
    shadowbot_config = data.get("shadowbot") if data is not None else None
    return isinstance(shadowbot_config, dict) and shadowbot_config.get("chrome_recovery") is True


# Top-level key that relocates the ShadowBot token directory; read by ``shadowbot.auth.get_state_dir``.
SHADOWBOT_TOKEN_KEY = "shadowbot_token"


_TYPO_CUTOFF = 0.8


def _resembled_setting(name: str, known: frozenset[str]) -> str | None:
    """Return the defined setting that ``name`` closely resembles, if any.

    Config keys are user-controlled text: an arbitrary one can carry a personal
    identifier or, with embedded newlines, forge a log line. Warnings therefore
    never print a user key; at most they name the schema field it resembles.
    """
    matches = difflib.get_close_matches(name, known, n=1, cutoff=_TYPO_CUTOFF)
    return matches[0] if matches else None


def _unrecognized_key_warning(subject: str, match: str | None, hint: str) -> str:
    """Describe an ignored key by its schema near-match, never by the key itself."""
    resembles = f" resembles '{match}'; check its spelling" if match else ""
    return f"{subject}{resembles}. It is not a recognized setting and is ignored; {hint}."


def shadowbot_config_warnings(data: Mapping[str, object]) -> tuple[str, ...]:
    """Report ShadowBot keys that are present but ignored.

    Flags unrecognized keys inside the ``shadowbot:`` section, a Chrome cookie
    path that has no effect because ``chrome_recovery`` is not enabled, and top-level
    ``shadowbot_*`` keys other than the honored ``shadowbot_token``. A top-level
    key whose suffix names a defined ShadowBot key points at the expected
    ``shadowbot.<key>`` location. Unrecognized keys are never echoed; a near
    miss is reported by the schema field it resembles. Unknown keys stay valid
    configuration; the result is advisory only.
    """
    known = frozenset(_ShadowbotConfig.model_fields)
    warnings: list[str] = []
    section = data.get("shadowbot")
    if isinstance(section, Mapping):
        for key in section:
            if key in known:
                continue
            match = _resembled_setting(key, known) if isinstance(key, str) else None
            warnings.append(
                _unrecognized_key_warning(
                    "An unrecognized key under 'shadowbot:'", match, "check the setting names in the guide"
                )
            )
        if section.get("chrome_cookies_path") and section.get("chrome_recovery") is not True:
            warnings.append(
                "shadowbot.chrome_cookies_path is set but Chrome recovery is off, so the path is unused; "
                "set shadowbot.chrome_recovery: true to allow fieldkit to read Chrome session cookies."
            )
    prefix = "shadowbot_"
    for key in data:
        if not isinstance(key, str) or not key.startswith(prefix) or key == SHADOWBOT_TOKEN_KEY:
            continue
        suffix = key[len(prefix) :]
        if suffix in known:
            warnings.append(f"{prefix}{suffix} is ignored; the expected location is shadowbot.{suffix}.")
        else:
            warnings.append(
                _unrecognized_key_warning(
                    "An unrecognized top-level shadowbot_ key",
                    _resembled_setting(suffix, known),
                    "ShadowBot settings belong under 'shadowbot:'",
                )
            )
    return tuple(warnings)


def get_shadowbot_config_warnings() -> tuple[str, ...]:
    """Return warnings for the loaded configuration, or none when it is absent."""
    data = _loader._load_raw_config()
    return shadowbot_config_warnings(data) if data is not None else ()


@_loader._config_cache
def log_shadowbot_config_warnings_once() -> None:
    """Log ShadowBot configuration warnings once per process.

    The cache registers with ``clear_config_caches()``, which re-arms the log for tests.
    """
    for warning in get_shadowbot_config_warnings():
        logger.warning("shadowbot config: %s", warning)
