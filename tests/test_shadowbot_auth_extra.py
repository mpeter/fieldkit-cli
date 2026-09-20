"""Additional branch tests for fieldkit.commands.shadowbot.auth._decrypt_chrome_cookies
and _silent_oidc — covers uncovered 20% branches."""

import sqlite3
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import fieldkit.shadowbot.auth as _auth_mod
from fieldkit.shadowbot.auth import (
    ShadowbotAuthError,
    _decrypt_chrome_cookies,
    _silent_oidc,
)

pytestmark = pytest.mark.unit

_FAKE_AUTH_ENDPOINT = "https://your-auth-host.example.com/protocol/openid-connect/auth"
_FAKE_TOKEN_ENDPOINT = "https://your-auth-host.example.com/protocol/openid-connect/token"
_FAKE_REDIRECT_URI = "https://shadowbot.example.test/oauth/callback"
_FAKE_CLIENT_ID = "shadowbot-ui"


@pytest.fixture(autouse=True)
def _configure_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep OIDC tests independent of a user configuration file."""
    monkeypatch.setattr(_auth_mod, "_get_redirect_uri", lambda: _FAKE_REDIRECT_URI)


# ---------------------------------------------------------------------------
# Re-export helper from the main auth test file
# ---------------------------------------------------------------------------


@contextmanager
def _patch_chrome_crypto_imports(mock_ss: MagicMock, chrome_password: bytes) -> Generator[None, None, None]:
    original = sys.modules.get("secretstorage")
    sys.modules["secretstorage"] = mock_ss
    try:
        with patch.object(
            _auth_mod,
            "_get_trusted_auth_endpoint",
            return_value=_auth_mod.urllib.parse.urlsplit("https://auth.example.test/auth/realms/ExampleIDP/"),
        ):
            yield
    finally:
        if original is None:
            sys.modules.pop("secretstorage", None)
        else:
            sys.modules["secretstorage"] = original


def _make_mock_ss(
    is_locked: bool = False,
    has_items: bool = True,
    chrome_password: bytes = b"test-password",
) -> tuple[MagicMock, bytes]:
    mock_ss = MagicMock()
    mock_collection = MagicMock()
    mock_collection.is_locked.return_value = is_locked
    if has_items:
        mock_item = MagicMock()
        mock_item.get_secret.return_value = chrome_password
        mock_collection.search_items.return_value = [mock_item]
    else:
        mock_collection.search_items.return_value = []
    mock_ss.dbus_init.return_value = MagicMock()
    mock_ss.get_default_collection.return_value = mock_collection
    return mock_ss, chrome_password


# ---------------------------------------------------------------------------
# _decrypt_chrome_cookies — no Chrome auth items in keyring
# ---------------------------------------------------------------------------


# ── TestDecryptChromeCookiesNoItems (flattened) ─────────────────────────────


def test_decrypt_chrome_cookies_no_items_no_keyring_items_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    cookies_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    conn.commit()
    conn.close()

    mock_ss, _ = _make_mock_ss(has_items=False)

    with (
        _patch_chrome_crypto_imports(mock_ss, b""),
        pytest.raises(ShadowbotAuthError, match="Safe Storage key not found"),
    ):
        _decrypt_chrome_cookies(cookies_path)


# ---------------------------------------------------------------------------
# _decrypt_chrome_cookies — keyring access exception
# ---------------------------------------------------------------------------


# ── TestDecryptChromeCookiesKeyringException (flattened) ────────────────────


def test_decrypt_chrome_cookies_keyring_exception_generic_keyring_exception_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    cookies_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    conn.commit()
    conn.close()

    mock_ss = MagicMock()
    mock_ss.dbus_init.side_effect = RuntimeError("dbus not available")

    with (
        _patch_chrome_crypto_imports(mock_ss, b""),
        pytest.raises(ShadowbotAuthError, match="GNOME keyring"),
    ):
        _decrypt_chrome_cookies(cookies_path)


# ---------------------------------------------------------------------------
# _decrypt_chrome_cookies — decryption failure (bad padding)
# ---------------------------------------------------------------------------


# ── TestDecryptChromeCookiesDecryptFailure (flattened) ──────────────────────


def test_decrypt_chrome_cookies_decrypt_failure_invalid_ciphertext_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AES decrypt of bad ciphertext raises ShadowbotAuthError without leaking bytes."""
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    cookies_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    # Valid v11 prefix but garbage ciphertext (wrong length for AES block)
    conn.execute(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
        (
            "AUTH_SESSION_ID",
            b"v11" + b"\xab" * 15,
            "auth.example.test",
            "/auth/realms/ExampleIDP/",
            1,
        ),  # not block-aligned  # pii-guard: ignore
    )
    conn.commit()
    conn.close()

    mock_ss, chrome_pw = _make_mock_ss()

    with (
        _patch_chrome_crypto_imports(mock_ss, chrome_pw),
        pytest.raises(ShadowbotAuthError, match="cookie decryption failed"),
    ):
        _decrypt_chrome_cookies(cookies_path)


# ---------------------------------------------------------------------------
# _decrypt_chrome_cookies — empty result (no matching cookies)
# ---------------------------------------------------------------------------


# ── TestDecryptChromeCookiesEmpty (flattened) ───────────────────────────────


def test_decrypt_chrome_cookies_empty_no_matching_cookies_returns_empty_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DB with rows for different host returns empty dict (no AUTH_SESSION_ID rows)."""
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    cookies_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    # Different host — should not be returned by query
    conn.execute(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
        ("SESSION", b"v11" + b"\x00" * 16, "other.example.com", "/", 1),
    )
    conn.commit()
    conn.close()

    mock_ss, chrome_pw = _make_mock_ss()

    with _patch_chrome_crypto_imports(mock_ss, chrome_pw):
        result = _decrypt_chrome_cookies(cookies_path)

    assert result == {}


# ---------------------------------------------------------------------------
# _decrypt_chrome_cookies — SQLite error
# ---------------------------------------------------------------------------


# ── TestDecryptChromeCookiesSQLiteError (flattened) ─────────────────────────


def test_decrypt_chrome_cookies_sq_lite_error_sqlite_query_error_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """sqlite3.Error during backup/query raises ShadowbotAuthError."""
    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    cookies_path = tmp_path / "Cookies"
    # Create a valid SQLite DB but then patch mem_conn.execute to fail
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    conn.commit()
    conn.close()

    mock_ss, chrome_pw = _make_mock_ss()

    import sqlite3 as _sqlite3

    def _bad_execute(*args: object, **kwargs: object) -> None:
        raise _sqlite3.Error("simulated query failure")

    with (
        _patch_chrome_crypto_imports(mock_ss, chrome_pw),
        patch("sqlite3.connect") as mock_connect,
    ):
        # First call returns real src_conn; second call (":memory:") returns a bad conn
        real_src = _sqlite3.connect(str(cookies_path))
        bad_mem = MagicMock()
        bad_mem.execute.side_effect = _sqlite3.Error("simulated query failure")
        mock_connect.side_effect = [real_src, bad_mem]
        # backup will copy from real_src to bad_mem; then bad_mem.execute raises
        with pytest.raises(ShadowbotAuthError, match=r"Failed to query Chrome Cookies database"):
            _decrypt_chrome_cookies(cookies_path)


# ---------------------------------------------------------------------------
# _decrypt_chrome_cookies — decrypted value too short for domain hash
# ---------------------------------------------------------------------------


# ── TestDecryptChromeCookiesTooShort (flattened) ────────────────────────────


def test_decrypt_chrome_cookies_too_short_decrypted_too_short_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Decrypted plaintext shorter than 32 bytes raises ShadowbotAuthError."""
    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    monkeypatch.setattr("fieldkit.shadowbot.auth._HAS_CHROME_AUTH", True)

    chrome_password = b"test-pw"
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1)
    aes_key = kdf.derive(chrome_password)

    # Encrypt 16 bytes (PKCS7 adds another 16 block = 32 total), less than 32-byte domain hash needed
    # Use only 10 bytes so after stripping 32 the unpadded<32 check fires
    # Actually: 10 bytes padded to 16 with PKCS7 → decrypted = 10 bytes < 32 needed
    short_plaintext = b"\x00" * 10
    padder = padding.PKCS7(128).padder()
    padded = padder.update(short_plaintext) + padder.finalize()
    iv = b" " * 16
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    enc = cipher.encryptor()
    ciphertext = enc.update(padded) + enc.finalize()
    blob = b"v11" + ciphertext

    cookies_path = tmp_path / "Cookies"
    conn = sqlite3.connect(str(cookies_path))
    conn.execute(
        "CREATE TABLE cookies (name TEXT, encrypted_value BLOB, host_key TEXT, path TEXT, creation_utc INTEGER)"
    )
    conn.execute(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
        ("AUTH_SESSION_ID", blob, "auth.example.test", "/auth/realms/ExampleIDP/", 1),
    )  # pii-guard: ignore
    conn.commit()
    conn.close()

    mock_ss, _ = _make_mock_ss(chrome_password=chrome_password)

    with (
        _patch_chrome_crypto_imports(mock_ss, chrome_password),
        pytest.raises(ShadowbotAuthError, match="too short for domain hash"),
    ):
        _decrypt_chrome_cookies(cookies_path)


# ---------------------------------------------------------------------------
# _silent_oidc — token exchange failures
# ---------------------------------------------------------------------------


# ── TestSilentOidcTokenExchange (flattened) ─────────────────────────────────


def test_silent_oidc_token_exchange_token_exchange_non_200_raises() -> None:
    """Non-200 from token exchange endpoint raises ShadowbotAuthError."""
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": "https://shadowbot.example.test/oauth/callback?code=xyz"}  # pii-guard: ignore

    token_resp = MagicMock()
    token_resp.status_code = 400
    token_resp.text = '{"error": "invalid_code"}'

    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post", return_value=token_resp),
        pytest.raises(ShadowbotAuthError, match="Token exchange failed"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


def test_silent_oidc_token_exchange_token_exchange_missing_access_token_raises() -> None:
    """Token exchange returning no access_token raises ShadowbotAuthError."""
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": "https://shadowbot.example.test/oauth/callback?code=xyz"}  # pii-guard: ignore

    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"refresh_token": "rt-only"}

    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post", return_value=token_resp),
        pytest.raises(ShadowbotAuthError, match="access_token"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


def test_silent_oidc_token_exchange_token_exchange_invalid_json_raises() -> None:
    """JSONDecodeError from token exchange raises ShadowbotAuthError."""
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": "https://shadowbot.example.test/oauth/callback?code=xyz"}  # pii-guard: ignore

    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.side_effect = ValueError("bad json")

    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post", return_value=token_resp),
        pytest.raises(ShadowbotAuthError, match="Invalid token exchange"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


def test_silent_oidc_token_exchange_auth_timeout_raises() -> None:
    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", side_effect=httpx.TimeoutException("timeout")),
        pytest.raises(ShadowbotAuthError, match="Timed out"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


def test_silent_oidc_token_exchange_auth_request_error_raises() -> None:
    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", side_effect=httpx.RequestError("connection refused")),
        pytest.raises(ShadowbotAuthError, match="Network error"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


def test_silent_oidc_token_exchange_token_exchange_timeout_raises() -> None:
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": "https://shadowbot.example.test/oauth/callback?code=xyz"}  # pii-guard: ignore

    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post", side_effect=httpx.TimeoutException("timeout")),
        pytest.raises(ShadowbotAuthError, match="Timed out"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


def test_silent_oidc_token_exchange_token_exchange_request_error_raises() -> None:
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": "https://shadowbot.example.test/oauth/callback?code=xyz"}  # pii-guard: ignore

    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post", side_effect=httpx.RequestError("dns fail")),
        pytest.raises(ShadowbotAuthError, match="Network error"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


def test_silent_oidc_token_exchange_other_error_in_qs_raises() -> None:
    """Non-login_required OIDC errors never reflect Location query values."""
    error_marker = "oidc-error-marker"
    state_marker = "oidc-state-marker"
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {
        "Location": f"https://shadowbot.example.test/oauth/callback?error={error_marker}&state={state_marker}"
    }

    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", return_value=auth_resp),
        pytest.raises(ShadowbotAuthError, match="authorization response contained an error") as exc_info,
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    for secret in (error_marker, state_marker):
        assert secret not in str(exc_info.value)
        assert secret not in repr(exc_info.value)


def test_silent_oidc_token_exchange_no_code_in_location_raises() -> None:
    """302 Location with no 'code' param raises ShadowbotAuthError."""
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": "https://shadowbot.example.test/oauth/callback?state=xyz"}  # pii-guard: ignore

    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", return_value=auth_resp),
        pytest.raises(ShadowbotAuthError, match="'code' parameter"),
    ):
        _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})


def test_silent_oidc_token_exchange_returns_empty_refresh_token() -> None:
    """Token exchange with no refresh_token returns empty string for refresh."""
    auth_resp = MagicMock()
    auth_resp.status_code = 302
    auth_resp.headers = {"Location": "https://shadowbot.example.test/oauth/callback?code=xyz"}  # pii-guard: ignore

    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "access-only"}

    with (
        patch.object(_auth_mod, "_get_auth_endpoint", return_value=_FAKE_AUTH_ENDPOINT),
        patch.object(_auth_mod, "_get_token_endpoint", return_value=_FAKE_TOKEN_ENDPOINT),
        patch.object(_auth_mod, "_get_client_id", return_value=_FAKE_CLIENT_ID),
        patch("httpx.get", return_value=auth_resp),
        patch("httpx.post", return_value=token_resp),
    ):
        access, refresh = _silent_oidc({"AUTH_SESSION_ID": "s", "KEYCLOAK_SESSION": "k"})

    assert access == "access-only"
    assert refresh == ""
