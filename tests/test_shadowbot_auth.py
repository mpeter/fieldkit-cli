"""Unit tests for fieldkit.commands.shadowbot.auth.

All tests are @pytest.mark.unit and use tmp_path for filesystem isolation.
No real network calls, Chrome access, or keyring access are made.
"""

import json
import os
import sqlite3
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import fieldkit.shadowbot.auth as auth_mod
from fieldkit.errors import MissingOptionalDependencyError
from fieldkit.shadowbot.auth import (
    ShadowbotAuthError,
    TokenCache,
    _decrypt_chrome_cookies,
    _load_token_file,
    _refresh_access_token,
    _save_token_file,
    _silent_oidc,
    acquire_from_chrome,
    get_state_dir,
    get_token,
    inject_refresh_token,
)

# ---------------------------------------------------------------------------
# Autouse fixture: reset module-level _cache before each test
# ---------------------------------------------------------------------------


_FAKE_TOKEN_ENDPOINT = "https://your-auth-host.example.com/protocol/openid-connect/token"
_FAKE_AUTH_ENDPOINT = "https://your-auth-host.example.com/protocol/openid-connect/auth"
_FAKE_REDIRECT_URI = "https://shadowbot.example.test/oauth/callback"
_FAKE_CLIENT_ID = "shadowbot-ui"


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level _cache to None before each test.

    org-agnostic-config: also patch config accessors so tests don't require
    a real config.yaml (get_shadowbot_*_endpoint now raises ConfigError when absent).
    """
    monkeypatch.setattr("fieldkit.shadowbot.auth._cache", None)
    # org-agnostic-config: patch config accessors so tests don't require a real
    # config.yaml. Use object-form setattr to avoid the private-symbol string-form
    # ratchet (TC-013). auth.py imports these as module-level aliases.
    monkeypatch.setattr(auth_mod, "_get_token_endpoint", lambda: _FAKE_TOKEN_ENDPOINT)
    monkeypatch.setattr(auth_mod, "_get_auth_endpoint", lambda: _FAKE_AUTH_ENDPOINT)
    monkeypatch.setattr(auth_mod, "_get_redirect_uri", lambda: _FAKE_REDIRECT_URI)
    monkeypatch.setattr(auth_mod, "_get_client_id", lambda: _FAKE_CLIENT_ID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token_file(tmp_path: Path, data: dict | None = None) -> Path:
    """Write a token file to tmp_path and return its path."""
    token_file = tmp_path / "shadowbot-token.json"
    payload = data or {
        "access_token": "old-access",
        "refresh_token": "stored-refresh",
        "token_uri": "https://auth.example.test/...",
        "client_id": "shadowbot-ui",
        "captured_at": str(time.time()),
    }
    token_file.write_text(json.dumps(payload), encoding="utf-8")
    return token_file


def _patch_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Patch get_state_dir to return tmp_path."""
    monkeypatch.setattr("fieldkit.shadowbot.auth.get_state_dir", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# TokenCache tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_token_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_token() returns cached token without any network call when cache is valid."""
    future = time.monotonic() + 200.0
    monkeypatch.setattr(
        "fieldkit.shadowbot.auth._cache",
        TokenCache(token="cached-token", expires_at=future),
    )

    with patch("httpx.post") as mock_post:
        result = get_token()

    assert result == "cached-token"
    assert auth_mod._cache is not None
    assert auth_mod._cache.token == "cached-token", "cache hit must not mutate the cached token"
    mock_post.assert_not_called()


@pytest.mark.unit
def test_token_cache_expiry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_token() refreshes when cache is expired (expires_at in the past)."""
    past = time.monotonic() - 1.0
    monkeypatch.setattr(
        "fieldkit.shadowbot.auth._cache",
        TokenCache(token="stale-token", expires_at=past),
    )
    _patch_state_dir(monkeypatch, tmp_path)
    _make_token_file(tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "fresh-token",
        "refresh_token": "new-refresh",
    }

    with patch("httpx.post", return_value=mock_resp):
        result = get_token()

    assert result == "fresh-token"
    assert auth_mod._cache is not None
    assert auth_mod._cache.token == "fresh-token", "expired cache must be replaced with the refreshed token"


# ---------------------------------------------------------------------------
# _refresh_access_token tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_access_token_success() -> None:
    """_refresh_access_token returns (access_token, refresh_token) on success."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
    }

    with patch("httpx.post", return_value=mock_resp):
        access, refresh = _refresh_access_token("old-refresh")

    assert access == "new-access-token"
    assert refresh == "new-refresh-token"


@pytest.mark.unit
def test_refresh_access_token_invalid_grant() -> None:
    """A refresh 400 reports safe endpoint and client diagnostics, not token data."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = '{"error": "invalid_grant"}'

    with (
        patch("httpx.post", return_value=mock_resp),
        pytest.raises(ShadowbotAuthError, match="expired or revoked") as exc_info,
    ):
        _refresh_access_token("bad-refresh")

    message = str(exc_info.value)
    assert "token_endpoint_origin='https://your-auth-host.example.com'" in message
    assert "client_id_sha256=" in message
    assert _FAKE_TOKEN_ENDPOINT not in message
    assert _FAKE_CLIENT_ID not in message
    assert "bad-refresh" not in message


@pytest.mark.unit
def test_refresh_access_token_does_not_retry_transport_error() -> None:
    """A refresh grant is never retried because its token may be consumed."""
    call_count = {"n": 0}

    def _fake_post(*args: object, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        raise httpx.ConnectError("transient connection reset")

    with patch("httpx.post", side_effect=_fake_post), pytest.raises(ShadowbotAuthError, match="Network error"):
        _refresh_access_token("old-refresh")

    assert call_count["n"] == 1


@pytest.mark.unit
def test_refresh_access_token_transport_error_raises_after_one_attempt() -> None:
    """A refresh transport failure surfaces without replaying the grant."""
    call_count = {"n": 0}

    def _fake_post(*args: object, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        raise httpx.ConnectError("persistent connection refused")

    with (
        patch("httpx.post", side_effect=_fake_post),
        pytest.raises(ShadowbotAuthError, match="Network error"),
    ):
        _refresh_access_token("old-refresh")

    assert call_count["n"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://your-auth-host.example.com/protocol/openid-connect/token",
        "https://your-auth-host.example.com:444/protocol/openid-connect/token",
        "https://user:password@your-auth-host.example.com/protocol/openid-connect/token",
        "https://your-auth-host.example.com/protocol/openid-connect/token?query-marker",
        "https://your-auth-host.example.com/protocol/openid-connect/token#fragment-marker",
    ],
)
def test_refresh_access_token_rejects_untrusted_token_endpoint_before_post(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """Refresh grants never post a token to an untrusted endpoint."""
    monkeypatch.setattr(auth_mod, "_get_token_endpoint", lambda: endpoint)

    with patch("httpx.post") as mock_post, pytest.raises(ShadowbotAuthError, match="must be HTTPS"):
        _refresh_access_token("test-refresh-token")

    mock_post.assert_not_called()


@pytest.mark.unit
def test_refresh_access_token_rejects_token_endpoint_with_different_configured_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token grant cannot cross from the configured authorization origin."""
    monkeypatch.setattr(auth_mod, "_get_token_endpoint", lambda: "https://other-auth.example.com/token")

    with patch("httpx.post") as mock_post, pytest.raises(ShadowbotAuthError, match="share the configured"):
        _refresh_access_token("test-refresh-token")

    mock_post.assert_not_called()


@pytest.mark.unit
def test_refresh_access_token_accepts_generic_same_origin_endpoints_with_explicit_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic endpoints may use separate paths and an explicit default HTTPS port."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"access_token": "new-access-token"}
    monkeypatch.setattr(auth_mod, "_get_auth_endpoint", lambda: "https://idp.example.com/authorize")
    monkeypatch.setattr(auth_mod, "_get_token_endpoint", lambda: "https://idp.example.com:443/token")

    with patch("httpx.post", return_value=response) as mock_post:
        access_token, refresh_token = _refresh_access_token("old-refresh-token")

    assert access_token == "new-access-token"
    assert refresh_token == "old-refresh-token"
    assert mock_post.call_args.args[0] == "https://idp.example.com:443/token"


# ---------------------------------------------------------------------------
# _load_token_file / _save_token_file tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_token_file_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_load_token_file raises ShadowbotAuthError when file does not exist."""
    _patch_state_dir(monkeypatch, tmp_path)

    with pytest.raises(ShadowbotAuthError, match="not found"):
        _load_token_file()


@pytest.mark.unit
def test_load_token_file_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_load_token_file returns dict when file exists and is valid JSON."""
    _patch_state_dir(monkeypatch, tmp_path)
    _make_token_file(tmp_path, {"access_token": "tok", "refresh_token": "rt"})

    result = _load_token_file()

    assert result["access_token"] == "tok"
    assert result["refresh_token"] == "rt"


@pytest.mark.unit
def test_save_token_file_permissions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_save_token_file creates a new file with 0o600 permissions."""
    if os.getuid() == 0:
        pytest.skip("Running as root — permission checks are not meaningful")

    _patch_state_dir(monkeypatch, tmp_path)

    _save_token_file({"access_token": "tok", "refresh_token": "rt"})

    token_path = tmp_path / "shadowbot-token.json"
    assert token_path.exists()
    mode = oct(token_path.stat().st_mode & 0o777)
    assert mode == oct(0o600), f"Expected 0o600, got {mode}"


@pytest.mark.unit
def test_save_token_file_permissions_overwrite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_save_token_file overwrites an existing 0o644 file and enforces 0o600."""
    if os.getuid() == 0:
        pytest.skip("Running as root — permission checks are not meaningful")

    _patch_state_dir(monkeypatch, tmp_path)

    # Create existing file with broad permissions
    token_path = tmp_path / "shadowbot-token.json"
    token_path.write_text('{"old": "data"}', encoding="utf-8")
    token_path.chmod(0o644)

    _save_token_file({"access_token": "new-tok", "refresh_token": "new-rt"})

    mode = oct(token_path.stat().st_mode & 0o777)
    assert mode == oct(0o600), f"Expected 0o600 after overwrite, got {mode}"
    data = json.loads(token_path.read_text(encoding="utf-8"))
    assert data["access_token"] == "new-tok"


# ---------------------------------------------------------------------------
# inject_refresh_token tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_inject_refresh_token_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """inject_refresh_token saves tokens to disk and clears cache."""
    _patch_state_dir(monkeypatch, tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "injected-access",
        "refresh_token": "injected-refresh",
    }

    with patch("httpx.post", return_value=mock_resp):
        inject_refresh_token("my-refresh-token")

    token_path = tmp_path / "shadowbot-token.json"
    assert token_path.exists()
    data = json.loads(token_path.read_text(encoding="utf-8"))
    assert data["access_token"] == "injected-access"
    assert data["refresh_token"] == "injected-refresh"
    # Cache should be cleared
    assert auth_mod._cache is None


@pytest.mark.unit
def test_inject_refresh_token_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An injected refresh 401 reports safe endpoint and client diagnostics."""
    _patch_state_dir(monkeypatch, tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = '{"error": "invalid_grant"}'

    with (
        patch("httpx.post", return_value=mock_resp),
        pytest.raises(ShadowbotAuthError, match=r"expired or revoked \(HTTP 401\)") as exc_info,
    ):
        inject_refresh_token("bad-token")

    message = str(exc_info.value)
    assert "token_endpoint_origin='https://your-auth-host.example.com'" in message
    assert "client_id_sha256=" in message
    assert _FAKE_TOKEN_ENDPOINT not in message
    assert _FAKE_CLIENT_ID not in message
    assert "bad-token" not in message


# ---------------------------------------------------------------------------
# _decrypt_chrome_cookies tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_decrypt_chrome_cookies_import_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_decrypt_chrome_cookies identifies the missing chrome-auth profile."""
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", False)
    fake_path = tmp_path / "Cookies"
    fake_path.touch()

    with pytest.raises(MissingOptionalDependencyError, match="chrome-auth"):
        _decrypt_chrome_cookies(fake_path)


@pytest.mark.unit
def test_decrypt_chrome_cookies_missing_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_decrypt_chrome_cookies raises ShadowbotAuthError when Cookies file is missing."""
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)
    missing = tmp_path / "nonexistent" / "Cookies"

    with pytest.raises(ShadowbotAuthError, match="not found or not a regular file"):
        _decrypt_chrome_cookies(missing)


@pytest.mark.unit
def test_decrypt_chrome_cookies_not_regular_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_decrypt_chrome_cookies raises ShadowbotAuthError for symlink or directory path."""
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    # Test with a symlink
    real_file = tmp_path / "real_cookies"
    real_file.touch()
    symlink_path = tmp_path / "Cookies_symlink"
    symlink_path.symlink_to(real_file)

    with pytest.raises(ShadowbotAuthError, match="symlink"):
        _decrypt_chrome_cookies(symlink_path)

    # Test with a directory
    dir_path = tmp_path / "Cookies_dir"
    dir_path.mkdir()

    with pytest.raises(ShadowbotAuthError, match="not found or not a regular file"):
        _decrypt_chrome_cookies(dir_path)


@pytest.mark.unit
def test_decrypt_chrome_cookies_keyring_locked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_decrypt_chrome_cookies raises ShadowbotAuthError when keyring is locked."""
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    # Create a real SQLite Cookies DB so the file check passes
    cookies_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    conn.commit()
    conn.close()

    # Mock secretstorage with a locked collection — use _patch_chrome_crypto_imports
    # (same pattern as test_decrypt_chrome_cookies_success) instead of the fragile
    # builtins.__import__ approach which behaves differently across CPython contexts.
    mock_ss = MagicMock()
    mock_collection = MagicMock()
    mock_collection.is_locked.return_value = True
    mock_ss.dbus_init.return_value = MagicMock()
    mock_ss.get_default_collection.return_value = mock_collection

    with (
        _patch_chrome_crypto_imports(mock_ss, b"chrome-password"),
        pytest.raises(ShadowbotAuthError) as exc_info,
    ):
        _decrypt_chrome_cookies(cookies_path)

    msg = str(exc_info.value)
    assert "--refresh-token" in msg
    # Message must NOT contain bytes repr or cookie values
    assert "b'" not in msg
    assert "bytes" not in msg.lower()


@pytest.mark.unit
def test_decrypt_chrome_cookies_unsupported_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_decrypt_chrome_cookies raises ShadowbotAuthError for v20 prefix."""
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    # Create a Cookies DB with a v20-prefixed value
    cookies_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    conn.execute(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
        (
            "AUTH_SESSION_ID",
            b"v20" + b"\x00" * 16,
            "your-auth-host.example.com",
            "/protocol/openid-connect/",
            1,
        ),  # pii-guard: ignore
    )
    conn.commit()
    conn.close()

    # Mock secretstorage: keyring unlocked, returns a key
    mock_ss = MagicMock()
    mock_collection = MagicMock()
    mock_collection.is_locked.return_value = False
    mock_item = MagicMock()
    mock_item.get_secret.return_value = b"chrome-password"
    mock_collection.search_items.return_value = [mock_item]
    mock_ss.dbus_init.return_value = MagicMock()
    mock_ss.get_default_collection.return_value = mock_collection

    with (
        _patch_chrome_crypto_imports(mock_ss, b"chrome-password"),
        pytest.raises(ShadowbotAuthError, match="unsupported Chrome cookie version: v20"),
    ):
        _decrypt_chrome_cookies(cookies_path)


@pytest.mark.unit
def test_decrypt_chrome_cookies_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_decrypt_chrome_cookies decrypts a pre-computed AES-128-CBC fixture correctly.

    Builds a real encrypted cookie blob:
      - PBKDF2-SHA1(password=b'peanut', salt=b'saltysalt', iter=1, keylen=16) → aes_key
      - AES-128-CBC(key=aes_key, iv=b' '*16) encrypt(32-byte-domain-hash + plaintext + PKCS7-pad)
      - Prepend b'v11'
    """
    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    # Build the AES key the same way Chrome does on Linux
    chrome_password = b"test-chrome-password"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1,
    )
    aes_key = kdf.derive(chrome_password)

    def _encrypt_cookie_value(value: str) -> bytes:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(b"\x00" * 32 + value.encode()) + padder.finalize()
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(b" " * 16))
        encryptor = cipher.encryptor()
        return b"v11" + encryptor.update(padded) + encryptor.finalize()

    # Create Cookies DB with this blob
    cookies_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    expected_cookies = {
        "AUTH_SESSION_ID": "test-cookie-01",
        "AUTH_SESSION_ID_LEGACY": "test-cookie-02",
        "KEYCLOAK_SESSION": "test-cookie-03",
        "KEYCLOAK_SESSION_LEGACY": "test-cookie-04",
        "KEYCLOAK_IDENTITY": "test-cookie-05",
        "KEYCLOAK_IDENTITY_LEGACY": "test-cookie-06",
        "AWSALB": "test-cookie-07",
        "AWSALBCORS": "test-cookie-08",
    }
    auth_cookie_path = "/protocol/openid-connect/"
    conn.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
        [
            (
                name,
                _encrypt_cookie_value(value),
                ".your-auth-host.example.com",
                auth_cookie_path if name.startswith(("AUTH_", "KEYCLOAK_")) else "/",
                2,
            )
            for name, value in expected_cookies.items()
        ]
        + [
            (
                "AUTH_SESSION_ID",
                _encrypt_cookie_value("newer-root-cookie"),
                "your-auth-host.example.com",
                "/",
                3,
            ),  # pii-guard: ignore
            (
                "AUTH_SESSION_ID",
                _encrypt_cookie_value("older-cookie"),
                "your-auth-host.example.com",
                auth_cookie_path,
                1,
            ),  # pii-guard: ignore
            (
                "AUTH_SESSION_ID",
                _encrypt_cookie_value("lookalike-cookie"),
                "your-auth-host.example.com.attacker.example",
                auth_cookie_path,
                3,
            ),  # pii-guard: ignore
            (
                "KEYCLOAK_SESSION",
                _encrypt_cookie_value("wrong-path-cookie"),
                "your-auth-host.example.com",
                "/other",
                3,
            ),  # pii-guard: ignore
        ],
    )
    conn.commit()
    conn.close()

    # Mock secretstorage to return our test password
    mock_ss = MagicMock()
    mock_collection = MagicMock()
    mock_collection.is_locked.return_value = False
    mock_item = MagicMock()
    mock_item.get_secret.return_value = chrome_password
    mock_collection.search_items.return_value = [mock_item]
    mock_ss.dbus_init.return_value = MagicMock()
    mock_ss.get_default_collection.return_value = mock_collection

    with _patch_chrome_crypto_imports(mock_ss, chrome_password):
        result = _decrypt_chrome_cookies(cookies_path)

    assert result == expected_cookies


def _patch_chrome_crypto_imports(mock_ss: MagicMock, chrome_password: bytes) -> Generator[None, None, None]:
    """Context manager that patches secretstorage inside _decrypt_chrome_cookies.

    We patch at the function-body import level by replacing sys.modules entries
    for secretstorage while letting cryptography imports pass through to the real library.
    """

    @contextmanager
    def _ctx() -> Generator[None, None, None]:
        original = sys.modules.get("secretstorage")
        sys.modules["secretstorage"] = mock_ss
        try:
            yield
        finally:
            if original is None:
                sys.modules.pop("secretstorage", None)
            else:
                sys.modules["secretstorage"] = original

    return _ctx()


# ---------------------------------------------------------------------------
# _silent_oidc tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_silent_oidc_success() -> None:
    """_silent_oidc returns (access_token, refresh_token) on successful flow."""
    # Mock the GET auth response (302 with Location containing code)
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {
        "Location": "https://shadowbot.example.test/oauth/callback?code=auth-code-123"
    }  # pii-guard: ignore

    # Mock the POST token response
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {
        "access_token": "oidc-access-token",
        "refresh_token": "oidc-refresh-token",
    }

    session_cookies = {
        "AUTH_SESSION_ID": "test-value-01",
        "AUTH_SESSION_ID_LEGACY": "test-value-02",
        "AWSALB": "test-value-03",
        "AWSALBCORS": "test-value-04",
        "KEYCLOAK_IDENTITY": "test-value-05",
        "KEYCLOAK_IDENTITY_LEGACY": "test-value-06",
        "KEYCLOAK_SESSION": "test-value-07",
        "KEYCLOAK_SESSION_LEGACY": "test-value-08",
    }
    with patch("httpx.get", return_value=auth_resp) as mock_get, patch("httpx.post", return_value=token_resp):
        access, refresh = _silent_oidc(session_cookies)

    assert access == "oidc-access-token"
    assert refresh == "oidc-refresh-token"
    # offline_access scope must be present so the returned refresh token is non-expiring
    called_url = mock_get.call_args[0][0]
    assert "offline_access" in called_url, f"offline_access missing from auth URL: {called_url}"
    assert mock_get.call_args.kwargs["headers"] == {
        "Cookie": "AUTH_SESSION_ID=test-value-01; AUTH_SESSION_ID_LEGACY=test-value-02; AWSALB=test-value-03; "
        "AWSALBCORS=test-value-04; KEYCLOAK_IDENTITY=test-value-05; KEYCLOAK_IDENTITY_LEGACY=test-value-06; "
        "KEYCLOAK_SESSION=test-value-07; KEYCLOAK_SESSION_LEGACY=test-value-08"
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://your-auth-host.example.com/protocol/openid-connect/auth",
        "https://your-auth-host.example.com:444/protocol/openid-connect/auth",
        "https://user:password@your-auth-host.example.com/protocol/openid-connect/auth",
        "https://your-auth-host.example.com/protocol/openid-connect/auth?query-marker",
        "https://your-auth-host.example.com/protocol/openid-connect/auth#fragment-marker",
    ],
)
def test_silent_oidc_rejects_untrusted_endpoint_before_sending_cookies(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """Cookies are never forwarded to an unsafe configured auth endpoint."""
    monkeypatch.setattr(auth_mod, "_get_auth_endpoint", lambda: endpoint)

    with patch("httpx.get") as mock_get, pytest.raises(ShadowbotAuthError, match="must be HTTPS"):
        _silent_oidc({"AUTH_SESSION_ID": "test-value"})

    mock_get.assert_not_called()


@pytest.mark.unit
def test_silent_oidc_retries_login_required_then_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    """A transient login_required response gets one sanitized authorization retry."""
    login_required = MagicMock()
    login_required.status_code = 302
    login_required.headers = {
        "Location": "https://shadowbot.example.test/callback?error=login_required&state=url-query-marker",
        "Set-Cookie": "response-header-marker",
    }
    success = MagicMock()
    success.status_code = 302
    success.headers = {"Location": "https://shadowbot.example.test/callback?code=test-code"}
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
    caplog.set_level("WARNING", logger=auth_mod.__name__)

    with (
        patch("httpx.get", side_effect=[login_required, success]) as mock_get,
        patch("httpx.post", return_value=token_resp),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "test-cookie-value"})

    assert mock_get.call_count == 2
    assert caplog.messages == [
        "Silent OIDC authorization diagnostic: "
        "attempt=1 outcome=login_required http_status=302 cookie_count=1 cookie_names=AUTH_SESSION_ID"
    ]
    for secret in ("test-cookie-value", "url-query-marker", "response-header-marker", "test-code"):
        assert secret not in caplog.text


@pytest.mark.unit
def test_silent_oidc_retries_transport_failure_then_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    """A transport failure gets one safe retry of the idempotent authorization GET."""
    success = MagicMock()
    success.status_code = 302
    success.headers = {"Location": "https://shadowbot.example.test/callback?code=test-code"}
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "test-access", "refresh_token": "test-refresh"}
    caplog.set_level("WARNING", logger=auth_mod.__name__)

    with (
        patch("httpx.get", side_effect=[httpx.ConnectError("transport-exception-marker"), success]) as mock_get,
        patch("httpx.post", return_value=token_resp),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "test-cookie-value"})

    assert mock_get.call_count == 2
    assert caplog.messages == [
        "Silent OIDC authorization diagnostic: "
        "attempt=1 outcome=transport error cookie_count=1 cookie_names=AUTH_SESSION_ID"
    ]
    for secret in ("test-cookie-value", "transport-exception-marker", "test-code"):
        assert secret not in caplog.text


@pytest.mark.unit
def test_silent_oidc_retry_exhaustion_has_safe_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    """Exhaustion reports only the transport failure class, never cookie values."""
    caplog.set_level("WARNING", logger=auth_mod.__name__)
    cookie_marker = "fake-cookie-marker"
    request = httpx.Request(
        "GET",
        _FAKE_AUTH_ENDPOINT,
        headers={"Cookie": f"AUTH_SESSION_ID={cookie_marker}"},
    )

    with (
        patch("httpx.get", side_effect=httpx.ConnectError("connection reset", request=request)) as mock_get,
        pytest.raises(ShadowbotAuthError, match=r"after 2 attempts\.") as exc_info,
    ):
        _silent_oidc({"AUTH_SESSION_ID": cookie_marker})

    assert mock_get.call_count == 2
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert cookie_marker not in str(exc_info.value)
    assert cookie_marker not in repr(exc_info.value)
    assert caplog.messages == [
        "Silent OIDC authorization diagnostic: "
        "attempt=1 outcome=transport error cookie_count=1 cookie_names=AUTH_SESSION_ID",
        "Silent OIDC authorization diagnostic: "
        "attempt=2 outcome=transport error cookie_count=1 cookie_names=AUTH_SESSION_ID",
    ]
    assert cookie_marker not in caplog.text


@pytest.mark.unit
def test_silent_oidc_rejects_untrusted_token_endpoint_before_code_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization codes are never posted to an untrusted token endpoint."""
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": "https://shadowbot.example.test/callback?code=test-code"}
    monkeypatch.setattr(
        auth_mod,
        "_get_token_endpoint",
        lambda: "https://other-auth.example.com/protocol/openid-connect/token",
    )

    with (
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post") as mock_post,
        pytest.raises(ShadowbotAuthError, match="share the configured"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "test-cookie-value"})

    mock_post.assert_not_called()


@pytest.mark.unit
def test_silent_oidc_token_exchange_drops_request_exception_chain() -> None:
    """A token-exchange error cannot retain its authorization-code request body."""
    code_marker = "fake-authorization-code-marker"
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": f"https://shadowbot.example.test/callback?code={code_marker}"}
    request = httpx.Request("POST", _FAKE_TOKEN_ENDPOINT, content=f"code={code_marker}")

    with (
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post", side_effect=httpx.ReadTimeout("response lost", request=request)),
        pytest.raises(ShadowbotAuthError, match="Timed out during token exchange") as exc_info,
    ):
        _silent_oidc({"AUTH_SESSION_ID": "test-cookie-value"})

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert code_marker not in str(exc_info.value)
    assert code_marker not in repr(exc_info.value)


@pytest.mark.unit
def test_refresh_invalid_json_drops_response_exception_chain() -> None:
    """A refresh JSON decoder error cannot retain a token-bearing response body."""
    body_marker = "refresh-response-body-marker"
    response = MagicMock()
    response.status_code = 200
    response.json.side_effect = ValueError(body_marker)

    with (
        patch("httpx.post", return_value=response),
        pytest.raises(ShadowbotAuthError, match="Invalid token response") as exc_info,
    ):
        _refresh_access_token("test-refresh-token")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert body_marker not in str(exc_info.value)
    assert body_marker not in repr(exc_info.value)


@pytest.mark.unit
def test_refresh_accepts_numeric_expiry_metadata() -> None:
    """Normal numeric token metadata does not invalidate consumed token fields."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
        "expires_in": 300,
    }

    with patch("httpx.post", return_value=response):
        access_token, refresh_token = _refresh_access_token("old-refresh-token")

    assert access_token == "new-access-token"
    assert refresh_token == "new-refresh-token"


@pytest.mark.unit
def test_refresh_missing_access_token_does_not_disclose_response_keys() -> None:
    """Server-controlled response keys do not escape in missing-token errors."""
    key_marker = "server-controlled-response-key"
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {key_marker: "server-controlled-response-value"}

    with (
        patch("httpx.post", return_value=response),
        pytest.raises(ShadowbotAuthError, match="missing access_token") as exc_info,
    ):
        _refresh_access_token("old-refresh-token")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert key_marker not in str(exc_info.value)
    assert key_marker not in repr(exc_info.value)


@pytest.mark.unit
def test_token_exchange_invalid_json_drops_response_exception_chain() -> None:
    """A code-exchange JSON decoder error cannot retain tokens or response data."""
    code_marker = "authorization-code-marker"
    body_marker = "token-response-body-marker"
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": f"https://shadowbot.example.test/callback?code={code_marker}"}
    response = MagicMock()
    response.status_code = 200
    response.json.side_effect = ValueError(body_marker)

    with (
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post", return_value=response),
        pytest.raises(ShadowbotAuthError, match="Invalid token exchange response") as exc_info,
    ):
        _silent_oidc({"AUTH_SESSION_ID": "test-cookie-value"})

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    for secret in (code_marker, body_marker, "test-cookie-value"):
        assert secret not in str(exc_info.value)
        assert secret not in repr(exc_info.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "variant,setup",
    [
        (
            "login_required",
            {
                "status_code": 302,
                "location": "https://shadowbot.example.test/oauth/callback?error=login_required",
                "match": "login_required",
            },
        ),
        (
            "non_302",
            {
                "status_code": 200,
                "location": "",
                "match": "200",
            },
        ),
        (
            "missing_location",
            {
                "status_code": 302,
                "location": "",
                "match": "Location",
            },
        ),
    ],
)
def test_silent_oidc_error_cases(variant: str, setup: dict) -> None:
    """_silent_oidc raises ShadowbotAuthError for login_required, non-302, and missing Location."""
    auth_resp = MagicMock()
    auth_resp.status_code = setup["status_code"]
    auth_resp.headers = {"Location": setup["location"]} if setup["location"] else {}

    with patch("httpx.get", return_value=auth_resp), pytest.raises(ShadowbotAuthError, match=setup["match"]):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


# ---------------------------------------------------------------------------
# get_token tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_token_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_token() returns cached token without any network call."""
    future = time.monotonic() + 200.0
    monkeypatch.setattr(
        "fieldkit.shadowbot.auth._cache",
        TokenCache(token="cached-access", expires_at=future),
    )

    with patch("httpx.post") as mock_post, patch("httpx.get") as mock_get:
        result = get_token()

    assert result == "cached-access"
    # Contract: get_token must return a non-empty string
    assert isinstance(result, str)
    assert len(result) > 0, "get_token must return a non-empty string"
    assert auth_mod._cache is not None
    assert auth_mod._cache.token == "cached-access", "cache hit must not mutate the cached token"
    mock_post.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.unit
def test_get_token_refresh_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_token() refreshes from disk, returns new access_token, and sets valid cache."""
    _patch_state_dir(monkeypatch, tmp_path)
    _make_token_file(
        tmp_path,
        {
            "access_token": "old-access",
            "refresh_token": "stored-refresh",
            "token_uri": "https://auth.example.test/...",
            "client_id": "shadowbot-ui",
            "captured_at": str(time.time()),
        },
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "refreshed-access",
        "refresh_token": "new-refresh",
    }

    with patch("httpx.post", return_value=mock_resp):
        result = get_token()

    assert result == "refreshed-access"
    assert auth_mod._cache is not None
    assert auth_mod._cache.is_valid()

    # Token file should have been updated
    data = json.loads((tmp_path / "shadowbot-token.json").read_text(encoding="utf-8"))
    assert data["access_token"] == "refreshed-access"
    assert data["refresh_token"] == "new-refresh"


@pytest.mark.unit
def test_get_token_chrome_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_token() falls back to Chrome acquisition when refresh token is invalid_grant."""
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    # Token file with a bad refresh token
    _make_token_file(
        tmp_path,
        {
            "access_token": "old-access",
            "refresh_token": "expired-refresh",
            "token_uri": "https://auth.example.test/...",
            "client_id": "shadowbot-ui",
            "captured_at": str(time.time()),
        },
    )

    # First call: refresh fails with invalid_grant
    refresh_resp = MagicMock()
    refresh_resp.status_code = 400
    refresh_resp.text = '{"error": "invalid_grant"}'

    # After acquire_from_chrome, the token file has a new access token
    new_token_data = {
        "access_token": "chrome-access-token",
        "refresh_token": "chrome-refresh-token",
        "token_uri": "https://auth.example.test/...",
        "client_id": "shadowbot-ui",
        "captured_at": str(time.time()),
    }

    def _mock_acquire_from_chrome(profile_path: Path | None = None) -> None:
        # Write the new token file as acquire_from_chrome would
        token_path = tmp_path / "shadowbot-token.json"
        token_path.write_text(json.dumps(new_token_data), encoding="utf-8")
        auth_mod._cache = None

    monkeypatch.setattr("fieldkit.shadowbot.auth.acquire_from_chrome", _mock_acquire_from_chrome)

    with patch("httpx.post", return_value=refresh_resp):
        result = get_token()

    assert result == "chrome-access-token"
    assert auth_mod._cache is not None
    assert auth_mod._cache.is_valid()


@pytest.mark.unit
def test_get_token_all_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_token() raises ShadowbotAuthError with --refresh-token hint when all paths fail."""
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", False)

    _make_token_file(
        tmp_path,
        {
            "access_token": "old",
            "refresh_token": "bad-refresh",
            "token_uri": "https://auth.example.test/...",
            "client_id": "shadowbot-ui",
            "captured_at": str(time.time()),
        },
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = '{"error": "invalid_grant"}'

    with patch("httpx.post", return_value=mock_resp), pytest.raises(ShadowbotAuthError) as exc_info:
        get_token()

    assert "--refresh-token" in str(exc_info.value)
    assert auth_mod._cache is None, "a failed refresh must not leave a stale cache entry"


@pytest.mark.unit
def test_get_token_no_refresh_token_in_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_token() raises ShadowbotAuthError when the token file has no refresh_token key."""
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", False)

    _make_token_file(tmp_path, {"access_token": "old"})

    with pytest.raises(ShadowbotAuthError, match="No refresh token in token file"):
        get_token()

    assert auth_mod._cache is None, "a missing refresh token must not leave a stale cache entry"


@pytest.mark.unit
def test_get_token_success_then_missing_refresh_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_token() returns the fetched token on success, and raises when no refresh token exists."""
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", False)
    _make_token_file(tmp_path)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "combined-access",
        "refresh_token": "combined-refresh",
    }
    with patch("httpx.post", return_value=mock_resp):
        result = get_token()
    assert result == "combined-access"

    auth_mod._cache = None
    _make_token_file(tmp_path, {"access_token": "old"})
    with pytest.raises(ShadowbotAuthError, match="No refresh token in token file"):
        get_token()


# ---------------------------------------------------------------------------
# acquire_from_chrome tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_acquire_from_chrome_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """acquire_from_chrome writes token file with expected keys and clears cache."""
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    session_cookies = {"AUTH_SESSION_ID": "sess", "KEYCLOAK_SESSION": "kc"}

    with (
        patch("fieldkit.shadowbot.auth._decrypt_chrome_cookies", return_value=session_cookies),
        patch("fieldkit.shadowbot.auth._silent_oidc", return_value=("chrome-access", "chrome-refresh")),
        # No config override → accessor returns None → default Chrome path used
        patch("fieldkit.config.get_shadowbot_chrome_cookies_path", return_value=None),
        # org-agnostic-config: _get_token_endpoint and _get_client_id are patched by autouse fixture
    ):
        acquire_from_chrome()

    token_path = tmp_path / "shadowbot-token.json"
    assert token_path.exists()
    data = json.loads(token_path.read_text(encoding="utf-8"))
    assert data["access_token"] == "chrome-access"
    assert data["refresh_token"] == "chrome-refresh"
    assert "token_uri" in data
    assert "client_id" in data
    assert "captured_at" in data
    # Cache should be cleared
    assert auth_mod._cache is None


@pytest.mark.unit
def test_acquire_from_chrome_uses_configured_cookies_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """acquire_from_chrome uses shadowbot.chrome_cookies_path from config when set."""
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    custom_cookies = tmp_path / "CustomProfile" / "Cookies"
    session_cookies = {"AUTH_SESSION_ID": "sess", "KEYCLOAK_SESSION": "kc"}

    captured: list[Path] = []

    def _capture_path(path: Path) -> dict[str, str]:
        captured.append(path)
        return session_cookies

    with (
        patch("fieldkit.shadowbot.auth._decrypt_chrome_cookies", side_effect=_capture_path),
        patch("fieldkit.shadowbot.auth._silent_oidc", return_value=("access", "refresh")),
        patch("fieldkit.config.get_shadowbot_chrome_cookies_path", return_value=custom_cookies),
        # org-agnostic-config: _get_token_endpoint and _get_client_id are patched by autouse fixture
    ):
        acquire_from_chrome()

    assert captured == [custom_cookies]


@pytest.mark.unit
def test_acquire_from_chrome_decrypt_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """acquire_from_chrome raises ShadowbotAuthError when decryption fails."""
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    with (
        patch(
            "fieldkit.shadowbot.auth._decrypt_chrome_cookies",
            side_effect=ShadowbotAuthError("cookie decryption failed: ValueError"),
        ),
        patch("fieldkit.config.get_shadowbot_chrome_cookies_path", return_value=None),
        pytest.raises(ShadowbotAuthError) as exc_info,
    ):
        acquire_from_chrome()

    msg = str(exc_info.value)
    # Must not contain raw bytes or cookie values
    assert "b'" not in msg


# ---------------------------------------------------------------------------
# get_state_dir tests
# ---------------------------------------------------------------------------


def _patch_get_state_dir_deps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_yaml: str,
    config_exists: bool = True,
) -> None:
    """Patch CONFIG_PATH and get_fieldkit_home in the auth module's namespace.

    auth.py imports CONFIG_PATH and get_fieldkit_home at module level via
    ``from fieldkit.config import CONFIG_PATH, get_fieldkit_home``. Python binds these
    names in the fieldkit.commands.shadowbot.auth namespace at import time, so patches
    must target that namespace directly — patching fieldkit.config.* has no effect
    on the already-bound module-level names.
    """
    mock_cp = MagicMock()
    mock_cp.exists.return_value = config_exists
    mock_cp.read_text.return_value = config_yaml
    monkeypatch.setattr("fieldkit.shadowbot.auth.CONFIG_PATH", mock_cp)
    monkeypatch.setattr("fieldkit.shadowbot.auth.get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.shadowbot.auth.get_fieldkit_data", lambda: tmp_path / "data")


@pytest.mark.unit
def test_get_state_dir_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_state_dir raises ShadowbotAuthError when config value parent is a symlink."""
    # Create a real dir and a symlink to it
    real_dir = tmp_path / "real_state"
    real_dir.mkdir()
    symlink_dir = tmp_path / "symlink_state"
    symlink_dir.symlink_to(real_dir)

    # The token path parent (symlink_dir) is a symlink
    token_path = symlink_dir / "shadowbot-token.json"
    fake_yaml = f"shadowbot_token: {token_path}\n"

    _patch_get_state_dir_deps(monkeypatch, tmp_path, fake_yaml)

    with pytest.raises(ShadowbotAuthError, match="symlink"):
        get_state_dir()


@pytest.mark.unit
def test_get_state_dir_config_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_state_dir returns parent of shadowbot_token config value when within data_root."""
    state_dir = tmp_path / "data"
    state_dir.mkdir()
    token_path = state_dir / "shadowbot-token.json"
    fake_yaml = f"shadowbot_token: {token_path}\n"

    _patch_get_state_dir_deps(monkeypatch, tmp_path, fake_yaml)

    result = get_state_dir()

    assert result == state_dir.resolve()
    assert isinstance(result, Path), "get_state_dir must return a Path"


@pytest.mark.unit
def test_get_state_dir_config_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_state_dir returns <data-root>/data/ when shadowbot_token is not in config."""
    _patch_get_state_dir_deps(monkeypatch, tmp_path, "", config_exists=False)

    result = get_state_dir()

    assert isinstance(result, Path)
    assert result == tmp_path / "data"


@pytest.mark.unit
def test_get_state_dir_invalid_user_override_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unexpandable shadowbot_token override retains the default-directory fallback."""
    _patch_get_state_dir_deps(monkeypatch, tmp_path, "shadowbot_token: ~unknown-fieldkit-user/token.json\n")

    result = get_state_dir()

    assert result == tmp_path / "data"


@pytest.mark.unit
def test_get_state_dir_invalid_utf8_config_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An undecodable config file retains the default-directory fallback."""
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"\xff")
    monkeypatch.setattr(auth_mod, "CONFIG_PATH", config_path)
    monkeypatch.setattr(auth_mod, "get_fieldkit_data", lambda: tmp_path / "data")

    result = get_state_dir()

    assert result == tmp_path / "data"


@pytest.mark.unit
def test_get_state_dir_path_escape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_state_dir raises ShadowbotAuthError when config value resolves outside approved roots."""
    # Use /tmp/evil as a path outside both data_root and ~/.config/fieldkit/
    escaped_path = Path("/tmp/evil/shadowbot-token.json")
    fake_yaml = f"shadowbot_token: {escaped_path}\n"

    _patch_get_state_dir_deps(monkeypatch, tmp_path, fake_yaml)

    with pytest.raises(ShadowbotAuthError, match="outside the approved roots"):
        get_state_dir()


@pytest.mark.unit
def test_shadowbot_token_path_accepted_when_fieldkit_data_outside_fieldkit_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-standard layout: fieldkit_data outside fieldkit_home is still an approved root."""
    home_dir = tmp_path / "home"
    data_dir = tmp_path / "data-elsewhere"  # outside home_dir
    home_dir.mkdir()
    data_dir.mkdir()
    # No shadowbot_token in config → get_state_dir() returns get_fieldkit_data()
    _patch_get_state_dir_deps(monkeypatch, tmp_path, "", config_exists=False)
    monkeypatch.setattr("fieldkit.shadowbot.auth.get_fieldkit_home", lambda: home_dir)
    monkeypatch.setattr("fieldkit.shadowbot.auth.get_fieldkit_data", lambda: data_dir)

    result = get_state_dir()

    assert result == data_dir


# ── acquire_from_chrome: _HAS_CHROME_AUTH=False branch (012-gazecrap-reduction) ──


@pytest.mark.unit
def test_acquire_from_chrome_raises_when_secretstorage_unavailable() -> None:
    """acquire_from_chrome identifies the missing chrome-auth profile.

    This covers the guard branch (line 631) without requiring a live Chrome session
    or GNOME keyring. The rest of the function (cookie decrypt, OIDC flow, token
    write) is genuine integration territory and remains untested here.
    """
    import fieldkit.shadowbot.auth as auth_mod

    with (
        patch.object(auth_mod, "_HAS_CHROME_AUTH", False),
        pytest.raises(
            MissingOptionalDependencyError,
            match="chrome-auth",
        ),
    ):
        acquire_from_chrome()


@pytest.mark.unit
def test_refresh_access_token_does_not_retry_lost_response() -> None:
    """The credential-burn guard — the reason this path uses connect_retry.

    The realm rotates refresh tokens: the grant consumes the token it is given, and
    hands back a new one (see the fixture in the retry-success test, which returns
    "new-refresh-token"). So a request that reached Keycloak and lost its response has
    already spent the old token.

    Retrying would send a consumed token, get invalid_grant, and force the user to
    re-authenticate — the retry would destroy the credential it exists to protect,
    converting a recoverable network blip into manual re-auth.
    """
    call_count = {"n": 0}

    def _fake_post(*args: object, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        raise httpx.ReadTimeout("response lost after transmission")

    with patch("httpx.post", side_effect=_fake_post), patch("time.sleep"), pytest.raises(ShadowbotAuthError):
        _refresh_access_token("old-refresh")

    assert call_count["n"] == 1, (
        "a lost response may have consumed the refresh token — retrying burns it and forces re-auth"
    )


@pytest.mark.unit
def test_refresh_transport_failure_drops_request_exception_chain() -> None:
    """A refresh error cannot retain the request body containing the refresh token."""
    refresh_marker = "fake-refresh-token-marker"
    request = httpx.Request(
        "POST",
        _FAKE_TOKEN_ENDPOINT,
        content=f"refresh_token={refresh_marker}",
    )

    with (
        patch("httpx.post", side_effect=httpx.ReadTimeout("response lost", request=request)),
        pytest.raises(ShadowbotAuthError, match="Timed out refreshing") as exc_info,
    ):
        _refresh_access_token(refresh_marker)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert refresh_marker not in str(exc_info.value)
    assert refresh_marker not in repr(exc_info.value)


@pytest.mark.unit
def test_refresh_access_token_does_not_retry_server_error() -> None:
    """A 5xx proves Keycloak received the grant, so the token may already be spent."""
    call_count = {"n": 0}

    def _fake_post(*args: object, **kwargs: object) -> MagicMock:
        call_count["n"] += 1
        resp = MagicMock()
        resp.status_code = 503
        resp.text = "service unavailable"
        return resp

    with patch("httpx.post", side_effect=_fake_post), patch("time.sleep"), pytest.raises(ShadowbotAuthError):
        _refresh_access_token("old-refresh")

    assert call_count["n"] == 1
