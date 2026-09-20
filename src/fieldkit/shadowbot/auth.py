"""fieldkit auth shadowbot — Silent Chrome-cookie OIDC token manager.

Acquires a Keycloak access token for the ``shadowbot-ui`` OIDC client by:
  1. Reading Chrome's encrypted cookie DB (Linux GNOME keyring + AES-128-CBC).
  2. Performing a silent OIDC auth-code flow (``prompt=none``) against the configured auth endpoint.
  3. Exchanging the code for tokens and persisting them to disk.

Falls back to ``--refresh-token`` injection when Chrome auth is unavailable
(macOS, SSH/headless, or ``secretstorage`` not installed).

Public API:
  get_state_dir() -> Path           Shared state directory (used by client.py too).
  get_token_path() -> Path          Path to the stored Keycloak token file.
  get_token() -> str                Return cached or freshly-refreshed access token.
  acquire_from_chrome(...) -> None  Acquire token from live Chrome session.
  inject_refresh_token(rt) -> None  Persist a manually-supplied refresh token.
  ShadowbotAuthError                Raised on auth failures.
  TokenCache                        In-memory token cache (240 s TTL).
"""

import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import tempfile
import time
import urllib.parse
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

from fieldkit.config import CONFIG_PATH, TIMEOUT_OIDC_HTTP, get_fieldkit_data, get_fieldkit_home
from fieldkit.config import get_shadowbot_auth_endpoint as _get_auth_endpoint
from fieldkit.config import get_shadowbot_client_id as _get_client_id
from fieldkit.config import get_shadowbot_redirect_uri as _get_redirect_uri
from fieldkit.config import get_shadowbot_token_endpoint as _get_token_endpoint
from fieldkit.config._loader import _read_config_dict
from fieldkit.errors import AuthError, MissingOptionalDependencyError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional chrome-auth extras probe (module-level flag only)
# ---------------------------------------------------------------------------

_CHROME_AUTH_IMPORT_ROOTS = ("cryptography", "secretstorage")


def _has_import_root(import_root: str) -> bool:
    try:
        return importlib.util.find_spec(import_root) is not None
    except ModuleNotFoundError:
        return False


_HAS_CHROME_AUTH = all(_has_import_root(root) for root in _CHROME_AUTH_IMPORT_ROOTS)


def _missing_chrome_auth_roots() -> tuple[str, ...]:
    missing = tuple(root for root in _CHROME_AUTH_IMPORT_ROOTS if not _has_import_root(root))
    return missing or _CHROME_AUTH_IMPORT_ROOTS


# ---------------------------------------------------------------------------
# Constants (implementation note: deployment-specific constants moved to config/_loader.py)
# ---------------------------------------------------------------------------

# OAuth endpoints, client ID, and redirect URI are read from the explicit optional
# integration configuration. fieldkit carries no organization-specific protocol defaults.
_DEFAULT_COOKIES_PATH = Path("~/.config/google-chrome/Default/Cookies").expanduser()
_SILENT_OIDC_ATTEMPTS = 2
_CHROME_AUTH_COOKIE_NAMES = (
    "AUTH_SESSION_ID",
    "AUTH_SESSION_ID_LEGACY",
    "KEYCLOAK_SESSION",
    "KEYCLOAK_SESSION_LEGACY",
    "KEYCLOAK_IDENTITY",
    "KEYCLOAK_IDENTITY_LEGACY",
    "AWSALB",
    "AWSALBCORS",
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ShadowbotAuthError(AuthError):
    """Raised when the ShadowBot session is missing, expired, or fails to yield a token."""


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------


@dataclass
class TokenCache:
    """In-memory cache for a short-lived access token."""

    token: str
    expires_at: float
    TTL_SECONDS: float = 240.0  # refresh 1 min before Keycloak's 5-min expiry

    def is_valid(self) -> bool:
        """Return True if the cached token has not yet expired."""
        return time.monotonic() < self.expires_at


_cache: TokenCache | None = None

# ---------------------------------------------------------------------------
# State directory resolution
# ---------------------------------------------------------------------------


def get_state_dir() -> Path:
    """Return the directory where shadowbot state files are stored.

    Resolution order:
      1. Parent of ``shadowbot_token`` config key (if set).
      2. ``<fieldkit_data>/``

    Security: validates that the resolved path is within ``get_fieldkit_home()``,
    ``get_fieldkit_data()``, or ``~/.config/fieldkit/`` and that no symlinks are present on the
    unresolved path (pre-resolve check — post-resolve ``.is_symlink()`` is
    always False because the resolver follows links).

    Raises:
        ShadowbotAuthError: If the path fails any security check.
    """
    candidate: Path | None = None

    try:
        data = _read_config_dict(CONFIG_PATH)
        if data is not None and "shadowbot_token" in data:
            token_path = Path(str(data["shadowbot_token"])).expanduser()
            candidate = token_path.parent
    except Exception:  # noqa: BLE001
        pass  # preserve the existing fallback for unreadable config and invalid override values

    if candidate is None:
        return get_fieldkit_data()

    # Security check 1: symlink check BEFORE .resolve()
    if candidate.is_symlink():
        raise ShadowbotAuthError(
            f"shadowbot_token parent directory is a symlink: {candidate}. "
            "Symlinks are not permitted for the state directory."
        )

    # Security check 2: resolve
    resolved = candidate.resolve()

    # Security check 3: verify resolved path is within approved roots.
    # Three roots are checked to support non-standard layouts where fieldkit_data
    # is outside fieldkit_home (e.g. separate drives or explicit config override).
    fieldkit_home = get_fieldkit_home().resolve()
    fieldkit_data = get_fieldkit_data().resolve()
    config_root = Path("~/.config/fieldkit").expanduser().resolve()

    for approved_root in (fieldkit_home, fieldkit_data, config_root):
        try:
            resolved.relative_to(approved_root)
            return resolved
        except ValueError:
            pass

    raise ShadowbotAuthError(
        f"shadowbot_token resolves to {resolved}, which is outside the approved "
        f"roots ({fieldkit_home}, {fieldkit_data}, {config_root}). "
        "Update shadowbot_token in config.yaml."
    )


# ---------------------------------------------------------------------------
# Token file helpers
# ---------------------------------------------------------------------------


def get_token_path() -> Path:
    """Return the path to the stored Keycloak token file.

    This is a public helper exported for use by the CLI layer, which needs
    to display the token file path to the user after a successful injection.
    """
    return get_state_dir() / "shadowbot-token.json"


# Internal alias used within this module to avoid refactoring all internal callers.
_get_token_path = get_token_path


def _load_token_file() -> dict[str, str]:
    """Load the stored token data.

    Returns:
        Dict with token fields.

    Raises:
        ShadowbotAuthError: If the file is missing or unreadable.
    """
    path = _get_token_path()
    if not path.exists():
        raise ShadowbotAuthError(
            f"ShadowBot token file not found: {path}\n"
            "Authenticate with: fieldkit auth shadowbot --refresh-token-file PATH"
        )
    try:
        result: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        return result
    except (json.JSONDecodeError, OSError) as exc:
        raise ShadowbotAuthError(f"Could not read ShadowBot token file {path}: {exc}") from exc


def _save_token_file(data: dict[str, str]) -> None:
    """Write token data to disk atomically with 0o600 permissions.

    Uses tempfile.mkstemp → os.fchmod → write → os.replace to guarantee:
    (a) the file is never world-readable at any point,
    (b) the swap is atomic on POSIX,
    (c) 0o600 is enforced regardless of the destination's prior permissions.
    """
    state_dir = get_state_dir()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_path = _get_token_path()

    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".shadowbot-tmp-")
    tmp_path = Path(tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(token_path)
    except Exception:
        # Clean up the temp file if anything goes wrong after mkstemp
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# OIDC refresh
# ---------------------------------------------------------------------------


def _request_token_grant(
    token_endpoint: str, grant_data: dict[str, str], timeout_message: str, network_message: str
) -> httpx.Response:
    """POST a one-shot token grant without retaining a transport exception."""
    response: httpx.Response | None = None
    error_message: str | None = None
    try:
        response = httpx.post(token_endpoint, data=grant_data, timeout=TIMEOUT_OIDC_HTTP)
    except httpx.TimeoutException:
        error_message = timeout_message
    except httpx.RequestError:
        error_message = network_message

    if error_message is not None:
        raise ShadowbotAuthError(error_message)
    assert response is not None
    return response


def _parse_token_response(response: httpx.Response, invalid_message: str) -> tuple[str, str | None]:
    """Extract consumed token fields without retaining parse exceptions or metadata."""
    response_data: object | None = None
    with contextlib.suppress(Exception):  # response decoder exceptions may retain token bodies
        response_data = response.json()

    if not isinstance(response_data, dict):
        raise ShadowbotAuthError(invalid_message)
    access_token = response_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ShadowbotAuthError("Token response missing access_token.")
    refresh_token = response_data.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        raise ShadowbotAuthError(invalid_message)
    return access_token, refresh_token


def _refresh_diagnostic_context(token_endpoint: urllib.parse.SplitResult, client_id: str) -> str:
    """Return non-secret configuration context for refresh failures."""
    endpoint_origin = f"{token_endpoint.scheme}://{token_endpoint.netloc}"
    client_id_fingerprint = hashlib.sha256(client_id.encode()).hexdigest()[:12]
    return f"token_endpoint_origin={endpoint_origin!r} client_id_sha256={client_id_fingerprint}"


def _refresh_tokens_from_response(
    response: httpx.Response, refresh_token: str, diagnostic_context: str
) -> tuple[str, str]:
    """Validate a refresh response and extract the returned token pair."""
    if response.status_code in (400, 401):
        raise ShadowbotAuthError(
            f"ShadowBot refresh token expired or revoked (HTTP {response.status_code}). "
            f"{diagnostic_context}. "
            "Re-authenticate with: fieldkit auth shadowbot --refresh-token-file PATH"
        )
    if response.status_code != 200:
        raise ShadowbotAuthError(
            f"Token refresh failed (HTTP {response.status_code}). "
            "Re-authenticate with: fieldkit auth shadowbot --refresh-token-file PATH"
        )

    access_token, new_refresh_token = _parse_token_response(response, "Invalid token response.")
    return access_token, refresh_token if new_refresh_token is None else new_refresh_token


def _refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """Exchange a Keycloak refresh token for a fresh access token exactly once."""
    trusted_token_endpoint = _get_trusted_token_endpoint()
    token_endpoint = trusted_token_endpoint.geturl()
    client_id = _get_client_id()
    response = _request_token_grant(
        token_endpoint,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        f"Timed out refreshing ShadowBot token ({TIMEOUT_OIDC_HTTP}s; transport timeout).",
        "Network error refreshing ShadowBot token (transport failure).",
    )
    return _refresh_tokens_from_response(
        response,
        refresh_token,
        _refresh_diagnostic_context(trusted_token_endpoint, client_id),
    )


# ---------------------------------------------------------------------------
# Chrome cookie decryption helpers
# ---------------------------------------------------------------------------


def _decode_cookie_value(encrypted_value: bytes, key: bytes) -> str:
    """Decrypt a single Chrome v11 AES-128-CBC encrypted cookie value.

    Pure crypto helper — accepts key material as a parameter so it can be
    tested without a GNOME keyring (Dependency Inversion Principle).

    Security: exception messages MUST NOT include raw cookie values, key
    material, or decrypted bytes.

    Args:
        encrypted_value: Raw bytes from the Chrome Cookies DB (including the
            3-byte ``v11`` version prefix).
        key: 16-byte AES key derived from the Chrome Safe Storage password.

    Returns:
        Plaintext cookie value (UTF-8, with domain hash prefix stripped).

    Raises:
        ShadowbotAuthError: On unsupported version prefix, decryption failure,
            or decrypted value too short for the domain hash.
    """
    # All cryptography imports inside function body (D3 — lazy import pattern)
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    # Check version prefix
    if not encrypted_value.startswith(b"v11"):
        prefix_str = encrypted_value[:3].decode("latin-1", errors="replace")
        raise ShadowbotAuthError(f"unsupported Chrome cookie version: {prefix_str}")

    ciphertext = encrypted_value[3:]  # strip 3-byte 'v11' prefix
    iv = b" " * 16  # AES-128-CBC IV: 16 space characters (Linux Chrome constant)

    # AES-128-CBC decrypt
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        # PKCS7 unpad
        unpadder = padding.PKCS7(128).unpadder()
        unpadded = unpadder.update(padded) + unpadder.finalize()
    except Exception as exc:
        raise ShadowbotAuthError(f"cookie decryption failed: {type(exc).__name__}") from exc

    # Strip 32-byte domain hash prefix
    if len(unpadded) < 32:
        raise ShadowbotAuthError("cookie decryption failed: decrypted value too short for domain hash")
    return unpadded[32:].decode("utf-8", errors="replace")


def _parse_cookie_row(row: tuple[str, bytes | str]) -> dict[str, str | bytes] | None:
    """Normalise a (name, encrypted_value) row from the Chrome Cookies DB.

    Pure parsing helper — converts the encrypted_value to bytes regardless of
    whether SQLite returned it as bytes or a latin-1 string.  Returns None for
    rows with an unrecognised name so callers can skip them cleanly.

    Extracted from ``_decrypt_chrome_cookies`` to reduce cyclomatic complexity
    (SOLID SRP / Open-Closed Principle).

    Args:
        row: ``(name, encrypted_value)`` tuple from the Cookies DB query.

    Returns:
        ``{"name": str, "raw": bytes}`` or None if the name is not one of the
        target cookie names.
    """
    name, encrypted_value = row
    if name not in _CHROME_AUTH_COOKIE_NAMES:
        return None
    raw: bytes = encrypted_value if isinstance(encrypted_value, bytes) else encrypted_value.encode("latin-1")
    return {"name": name, "raw": raw}


def _get_safe_oidc_endpoint(endpoint: str) -> urllib.parse.SplitResult:
    """Validate a configured OIDC endpoint before it can receive credentials."""
    parsed: urllib.parse.SplitResult | None = None
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname is None
        or not parsed.path
        or not valid_port
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ShadowbotAuthError(
            "Configured OIDC endpoint must be HTTPS with a path and no credentials, query, or fragment."
        )
    return parsed


def _same_origin(left: urllib.parse.SplitResult, right: urllib.parse.SplitResult) -> bool:
    """Return whether two validated HTTPS endpoints have the same configured origin."""
    return (left.scheme, left.hostname, left.port or 443) == (right.scheme, right.hostname, right.port or 443)


def _get_trusted_auth_endpoint() -> urllib.parse.SplitResult:
    """Return the configured authorization endpoint only when it is trusted."""
    return _get_safe_oidc_endpoint(_get_auth_endpoint())


def _get_trusted_token_endpoint() -> urllib.parse.SplitResult:
    """Return the configured token endpoint only when it shares the auth origin."""
    auth_endpoint = _get_trusted_auth_endpoint()
    token_endpoint = _get_safe_oidc_endpoint(_get_token_endpoint())
    if not _same_origin(auth_endpoint, token_endpoint):
        raise ShadowbotAuthError(
            "Configured OIDC token endpoint must share the configured authorization endpoint origin."
        )
    return token_endpoint


def _cookie_path_matches_request_path(cookie_path: str, request_path: str) -> bool:
    """Return whether a Chrome cookie path is applicable to the OIDC request path."""
    if not request_path.startswith(cookie_path):
        return False
    return cookie_path.endswith("/") or len(request_path) == len(cookie_path) or request_path[len(cookie_path)] == "/"


# ---------------------------------------------------------------------------
# Chrome cookie decryption
# ---------------------------------------------------------------------------


def _validate_chrome_cookie_path(cookie_path: Path) -> None:
    """Validate that the configured Chrome cookie database is safe to open."""
    if not _HAS_CHROME_AUTH:
        raise MissingOptionalDependencyError(
            "auth shadowbot",
            "chrome-auth",
            _missing_chrome_auth_roots(),
        )

    # Security: symlink check BEFORE anything else (pre-resolve)
    if cookie_path.is_symlink():
        raise ShadowbotAuthError(
            f"Chrome Cookies path is a symlink: {cookie_path}. Symlinks are not permitted for security reasons."
        )

    if not cookie_path.is_file():
        raise ShadowbotAuthError(
            f"Chrome Cookies file not found or not a regular file: {cookie_path}\n"
            "Set shadowbot.chrome_cookies_path in ~/.config/fieldkit/config.yaml "
            "to point to your Chrome profile's Cookies file."
        )


def _get_chrome_aes_key() -> bytes:
    """Read Chrome's keyring secret and derive its AES key."""
    import secretstorage
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    # Get Chrome Safe Storage key from GNOME keyring
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection.is_locked():
            raise ShadowbotAuthError(
                "GNOME keyring is locked. Unlock it and try again, "
                "or use: fieldkit auth shadowbot --refresh-token-file PATH"
            )
        items = list(collection.search_items({"application": "chrome"}))
        if not items:
            raise ShadowbotAuthError(
                "Chrome Safe Storage key not found in GNOME keyring. "
                "Ensure Chrome is running and logged in, "
                "or use: fieldkit auth shadowbot --refresh-token-file PATH"
            )
        chrome_password = items[0].get_secret()
    except ShadowbotAuthError:
        raise
    except Exception as exc:
        raise ShadowbotAuthError(
            f"Failed to access GNOME keyring: {exc}. "
            "In SSH/headless environments, use: fieldkit auth shadowbot --refresh-token-file PATH"
        ) from exc

    # Derive AES-128 key via PBKDF2-SHA1
    # Linux Chrome: salt=b'saltysalt', iterations=1, keylen=16 (os_crypt_linux.cc)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1,
    )
    return kdf.derive(chrome_password)


def _load_chrome_cookie_rows(cookie_path: Path, auth_host: str) -> list[tuple[str, bytes | str, str]]:
    """Copy Chrome's live database and return only the allowlisted cookie rows."""
    encoded_path = urllib.parse.quote(str(cookie_path), safe="/")
    uri = f"file:{encoded_path}?mode=ro"
    try:
        src_conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.OperationalError as exc:
        raise ShadowbotAuthError(f"Could not open Chrome Cookies database: {exc}") from exc

    mem_conn = sqlite3.connect(":memory:")
    try:
        try:
            src_conn.backup(mem_conn)
        finally:
            src_conn.close()
        cursor = mem_conn.execute(
            "SELECT name, encrypted_value, path FROM cookies "
            "WHERE host_key IN (?, ?) "
            "AND name IN (?, ?, ?, ?, ?, ?, ?, ?) "
            "ORDER BY name, length(path) DESC, creation_utc DESC, rowid DESC",
            (
                auth_host,
                f".{auth_host}",
                *_CHROME_AUTH_COOKIE_NAMES,
            ),
        )
        return cast(list[tuple[str, bytes | str, str]], cursor.fetchall())
    except sqlite3.Error as exc:
        raise ShadowbotAuthError(f"Failed to query Chrome Cookies database: {exc}") from exc
    finally:
        mem_conn.close()


def _eligible_chrome_cookie(row: tuple[str, bytes | str, str], auth_request_path: str) -> tuple[str, bytes] | None:
    """Return a decrypted-cookie candidate only when its host path is applicable."""
    name, encrypted_value, cookie_path = row
    if not _cookie_path_matches_request_path(cookie_path, auth_request_path):
        return None
    parsed = _parse_cookie_row((name, encrypted_value))
    if parsed is None:
        return None
    cookie_raw = parsed["raw"]
    assert isinstance(cookie_raw, bytes)
    return str(parsed["name"]), cookie_raw


def _decrypt_chrome_cookie_rows(
    rows: list[tuple[str, bytes | str, str]], aes_key: bytes, auth_request_path: str
) -> dict[str, str]:
    """Decrypt the newest applicable value for each allowlisted cookie name."""
    result: dict[str, str] = {}
    for row in rows:
        candidate = _eligible_chrome_cookie(row, auth_request_path)
        if candidate is None:
            continue
        cookie_name, cookie_raw = candidate
        if cookie_name in result:
            continue
        result[cookie_name] = _decode_cookie_value(cookie_raw, aes_key)
    return result


def _decrypt_chrome_cookies(cookie_path: Path) -> dict[str, str]:
    """Decrypt applicable Chrome cookies for the exact trusted OIDC endpoint."""
    _validate_chrome_cookie_path(cookie_path)
    auth_endpoint = _get_trusted_auth_endpoint()
    assert auth_endpoint.hostname is not None
    rows = _load_chrome_cookie_rows(cookie_path, auth_endpoint.hostname)
    return _decrypt_chrome_cookie_rows(rows, _get_chrome_aes_key(), auth_endpoint.path)


# ---------------------------------------------------------------------------
# Silent OIDC flow
# ---------------------------------------------------------------------------


def _log_silent_oidc_diagnostic(
    attempt: int,
    outcome: str,
    cookie_names: tuple[str, ...],
    status_code: int | None = None,
) -> None:
    """Log only the approved metadata for a failed silent-authorization attempt."""
    status = f" http_status={status_code}" if status_code is not None else ""
    logger.warning(
        "Silent OIDC authorization diagnostic: attempt=%d outcome=%s%s cookie_count=%d cookie_names=%s",
        attempt,
        outcome,
        status,
        len(cookie_names),
        ",".join(cookie_names),
    )


def _request_silent_oidc_authorization(auth_url: str, session_cookies: dict[str, str]) -> httpx.Response:
    """Get an authorization response, retrying only safe transient failures."""
    cookie_header = "; ".join(f"{name}={value}" for name, value in session_cookies.items())
    cookie_names = tuple(sorted(session_cookies))
    final_outcome: str | None = None
    for attempt in range(1, _SILENT_OIDC_ATTEMPTS + 1):
        try:
            response = httpx.get(
                auth_url,
                headers={"Cookie": cookie_header},
                follow_redirects=False,
                timeout=TIMEOUT_OIDC_HTTP,
            )
        except httpx.TimeoutException:
            _log_silent_oidc_diagnostic(attempt, "timeout", cookie_names)
            if attempt == _SILENT_OIDC_ATTEMPTS:
                final_outcome = "timeout"
                break
        except httpx.RequestError:
            _log_silent_oidc_diagnostic(attempt, "transport error", cookie_names)
            if attempt == _SILENT_OIDC_ATTEMPTS:
                final_outcome = "transport error"
                break
        else:
            location = response.headers.get("Location", "")
            query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
            if query.get("error") != ["login_required"]:
                return response
            _log_silent_oidc_diagnostic(attempt, "login_required", cookie_names, response.status_code)
            if attempt == _SILENT_OIDC_ATTEMPTS:
                return response

    if final_outcome == "timeout":
        raise ShadowbotAuthError(f"Timed out waiting for OIDC authorization after {_SILENT_OIDC_ATTEMPTS} attempts.")
    if final_outcome == "transport error":
        raise ShadowbotAuthError(f"Network error during OIDC authorization after {_SILENT_OIDC_ATTEMPTS} attempts.")
    raise AssertionError("silent OIDC retry loop must return or raise")  # pragma: no cover


def _authorization_code_from_response(response: httpx.Response) -> str:
    """Validate a redirect response and return its authorization code without reflecting query values."""
    if response.status_code != 302:
        raise ShadowbotAuthError(
            f"OIDC auth endpoint returned unexpected status {response.status_code} "
            "(expected 302). The Chrome session may have expired."
        )

    location = response.headers.get("Location", "")
    if not location:
        raise ShadowbotAuthError("OIDC auth endpoint returned 302 but no Location header.")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    error_values = query.get("error", [])
    if error_values:
        if error_values == ["login_required"]:
            raise ShadowbotAuthError(
                "Chrome session has expired (login_required). "
                "Log into your ShadowBot URL in Chrome (Default profile or configured path), "
                "then retry. Or use: fieldkit auth shadowbot --refresh-token-file PATH"
            )
        raise ShadowbotAuthError(
            "OIDC authorization response contained an error. "
            "Re-authenticate with: fieldkit auth shadowbot --refresh-token-file PATH"
        )

    codes = query.get("code", [])
    if not codes:
        raise ShadowbotAuthError("OIDC auth 302 Location did not contain a 'code' parameter.")
    code = codes[0]
    if not isinstance(code, str):
        raise ShadowbotAuthError("OIDC auth 302 Location did not contain a valid 'code' parameter.")
    return code


def _authorization_tokens_from_response(response: httpx.Response) -> tuple[str, str]:
    """Validate a code-exchange response and extract its token pair."""
    if response.status_code != 200:
        raise ShadowbotAuthError(
            f"Token exchange failed (HTTP {response.status_code}). "
            "Re-authenticate with: fieldkit auth shadowbot --refresh-token-file PATH"
        )
    access_token, refresh_token = _parse_token_response(response, "Invalid token exchange response.")
    return access_token, refresh_token or ""


def _silent_oidc(session_cookies: dict[str, str]) -> tuple[str, str]:
    """Perform a silent OIDC auth-code flow using Chrome Keycloak session cookies.

    Sends a ``GET /auth?...&prompt=none`` request with the Chrome session cookies
    and ``follow_redirects=False``. Keycloak recognises the live session and
    returns 302 to the registered redirect_uri with ``?code=...``.

    Args:
        session_cookies: Decrypted allowlisted Chrome cookies for the OIDC request.

    Returns:
        ``(access_token, refresh_token)``

    Raises:
        ShadowbotAuthError: On non-302 response, missing Location, error param,
            or token exchange failure.
    """
    redirect_uri = _get_redirect_uri()
    auth_params = urllib.parse.urlencode(
        {
            "client_id": _get_client_id(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access",
            "prompt": "none",
        }
    )
    auth_endpoint = _get_trusted_auth_endpoint()
    auth_url = f"{auth_endpoint.geturl()}?{auth_params}"

    auth_resp = _request_silent_oidc_authorization(auth_url, session_cookies)
    code = _authorization_code_from_response(auth_resp)
    token_response = _request_token_grant(
        _get_trusted_token_endpoint().geturl(),
        {
            "grant_type": "authorization_code",
            "client_id": _get_client_id(),
            "code": code,
            "redirect_uri": redirect_uri,
        },
        f"Timed out during token exchange ({TIMEOUT_OIDC_HTTP}s; transport timeout).",
        "Network error during token exchange (transport failure).",
    )
    return _authorization_tokens_from_response(token_response)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def acquire_from_chrome(profile_path: Path | None = None) -> None:
    """Acquire a ShadowBot token from the live Chrome session.

    Reads the Chrome Cookies file (from config or default), decrypts the
    Keycloak session cookies, performs a silent OIDC flow, and persists the
    resulting tokens to disk.

    Args:
        profile_path: Unused; kept for API compatibility. Cookie path is
            resolved from ``shadowbot.chrome_cookies_path`` config key.

    Raises:
        ShadowbotAuthError: If Chrome auth is unavailable or the flow fails.
    """
    global _cache  # noqa: PLW0603

    if not _HAS_CHROME_AUTH:
        raise MissingOptionalDependencyError(
            "auth shadowbot",
            "chrome-auth",
            _missing_chrome_auth_roots(),
        )

    # Resolve cookie path: config accessor (shadowbot.chrome_cookies_path) or default.
    from fieldkit.config import get_shadowbot_chrome_cookies_path

    configured_path = get_shadowbot_chrome_cookies_path()
    cookies_path = configured_path if configured_path is not None else _DEFAULT_COOKIES_PATH

    try:
        session_cookies = _decrypt_chrome_cookies(cookies_path)
    except ShadowbotAuthError:
        raise
    except Exception as exc:
        raise ShadowbotAuthError(f"cookie decryption failed: {type(exc).__name__}") from exc

    access_token, refresh_token = _silent_oidc(session_cookies)

    # Load existing token data to preserve any extra fields
    existing: dict[str, str] = {}
    with contextlib.suppress(ShadowbotAuthError):
        existing = _load_token_file()

    existing.update(
        {
            "access_token": access_token,
            "refresh_token": refresh_token if refresh_token else existing.get("refresh_token", ""),
            "token_uri": _get_token_endpoint(),
            "client_id": _get_client_id(),
            "captured_at": str(time.time()),
        }
    )
    _save_token_file(existing)

    # Clear in-memory cache on success
    _cache = None


def inject_refresh_token(refresh_token: str) -> None:
    """Persist a manually-supplied Keycloak refresh token.

    Exchanges the refresh token for an access token, saves both to disk,
    and clears the in-memory cache.

    Args:
        refresh_token: A valid Keycloak refresh token.

    Raises:
        ShadowbotAuthError: If the token is invalid or the exchange fails.
    """
    global _cache  # noqa: PLW0603

    access_token, new_rt = _refresh_access_token(refresh_token)

    existing: dict[str, str] = {}
    with contextlib.suppress(ShadowbotAuthError):
        existing = _load_token_file()

    existing.update(
        {
            "access_token": access_token,
            "refresh_token": new_rt,
            "token_uri": _get_token_endpoint(),
            "client_id": _get_client_id(),
            "captured_at": str(time.time()),
        }
    )
    _save_token_file(existing)
    _cache = None


def get_token() -> str:
    """Return a valid ShadowBot access token, refreshing if needed.

    Resolution order:
      1. In-memory cache (240 s TTL).
      2. Refresh token from disk file.
      3. Chrome cookie acquisition (if ``_HAS_CHROME_AUTH`` and refresh fails
         with ``invalid_grant``).

    Returns:
        A valid access token string.

    Raises:
        ShadowbotAuthError: If all auth paths are exhausted.
    """
    global _cache  # noqa: PLW0603

    # 1. Cache hit
    if _cache is not None and _cache.is_valid():
        return _cache.token

    # 2. Try refresh from disk
    try:
        token_data = _load_token_file()
        stored_rt = token_data.get("refresh_token", "")
        if not stored_rt:
            raise ShadowbotAuthError("No refresh token in token file.")

        access_token, new_rt = _refresh_access_token(stored_rt)

        token_data["access_token"] = access_token
        token_data["refresh_token"] = new_rt
        token_data["captured_at"] = str(time.time())
        with contextlib.suppress(OSError):
            _save_token_file(token_data)

        _cache = TokenCache(
            token=access_token,
            expires_at=time.monotonic() + TokenCache.TTL_SECONDS,
        )
        return access_token

    except ShadowbotAuthError as refresh_exc:
        # Check if it's an invalid_grant error (token expired)
        exc_str = str(refresh_exc)
        is_invalid_grant = "expired or revoked" in exc_str or "invalid_grant" in exc_str

        # 3. Try Chrome fallback if refresh token is invalid/expired
        if is_invalid_grant and _HAS_CHROME_AUTH:
            try:
                acquire_from_chrome()
                # acquire_from_chrome saves to disk and clears cache;
                # reload from disk to get the new token
                token_data = _load_token_file()
                access_token = token_data.get("access_token", "")
                if access_token:
                    _cache = TokenCache(
                        token=access_token,
                        expires_at=time.monotonic() + TokenCache.TTL_SECONDS,
                    )
                    return access_token
            except ShadowbotAuthError as chrome_exc:
                warnings.warn(
                    f"Chrome cookie auth fallback failed: {chrome_exc}",
                    stacklevel=2,
                )

        # historic regression: the inner exception already contains the "Re-authenticate with:" hint
        # (from _refresh_access_token). Wrapping it again produces a duplicate.
        # Keep only the outer context, relying on the inner message for the hint.
        raise ShadowbotAuthError(f"ShadowBot authentication failed: {refresh_exc}") from refresh_exc
