"""Unit tests for the pure crypto/parsing helpers extracted from _decrypt_chrome_cookies.

Tests _decode_cookie_value and _parse_cookie_row without requiring a GNOME keyring.
These helpers were extracted to reduce cyclomatic complexity (SOLID SRP).
"""

import pytest

from fieldkit.shadowbot.auth import (
    ShadowbotAuthError,
    _decode_cookie_value,
    _parse_cookie_row,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _decode_cookie_value — pure crypto helper
# ---------------------------------------------------------------------------


def _make_aes_key(password: bytes = b"test-password") -> bytes:
    """Derive a 16-byte AES key using the same PBKDF2 params as Chrome."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1)
    return kdf.derive(password)


def _encrypt_cookie(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt plaintext with AES-128-CBC (Chrome format) and prepend v11 prefix."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = b" " * 16
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    ciphertext = enc.update(padded) + enc.finalize()
    return b"v11" + ciphertext


# ── TestDecodeCookieValue (flattened) ───────────────────────────────────────


def test_decode_cookie_value_valid_cookie_decrypts_correctly() -> None:
    """Round-trip: encrypt then decrypt returns the original plaintext."""
    key = _make_aes_key()
    # Chrome cookies have a 32-byte domain hash prefix before the actual value
    domain_hash = b"\x00" * 32
    plaintext = domain_hash + b"my-session-token"
    blob = _encrypt_cookie(plaintext, key)

    result = _decode_cookie_value(blob, key)

    assert result == "my-session-token"


def test_decode_cookie_value_wrong_version_prefix_raises() -> None:
    """Non-v11 prefix raises ShadowbotAuthError with version info."""
    key = _make_aes_key()
    bad_blob = b"v10" + b"\x00" * 16

    with pytest.raises(ShadowbotAuthError, match="unsupported Chrome cookie version"):
        _decode_cookie_value(bad_blob, key)


def test_decode_cookie_value_no_prefix_raises() -> None:
    """Blob without v11 prefix raises ShadowbotAuthError."""
    key = _make_aes_key()
    bad_blob = b"xyz" + b"\x00" * 16

    with pytest.raises(ShadowbotAuthError, match="unsupported Chrome cookie version"):
        _decode_cookie_value(bad_blob, key)


def test_decode_cookie_value_bad_ciphertext_raises_decryption_error() -> None:
    """Non-block-aligned ciphertext raises ShadowbotAuthError (not raw exception)."""
    key = _make_aes_key()
    # v11 prefix + 15 bytes (not AES block-aligned)
    bad_blob = b"v11" + b"\xab" * 15

    with pytest.raises(ShadowbotAuthError, match="cookie decryption failed"):
        _decode_cookie_value(bad_blob, key)


def test_decode_cookie_value_decrypted_too_short_raises() -> None:
    """Decrypted value shorter than 32 bytes raises ShadowbotAuthError."""
    key = _make_aes_key()
    # Encrypt only 10 bytes — after decryption, 10 < 32 domain hash threshold
    short_plaintext = b"\x00" * 10
    blob = _encrypt_cookie(short_plaintext, key)

    with pytest.raises(ShadowbotAuthError, match="too short for domain hash"):
        _decode_cookie_value(blob, key)


def test_decode_cookie_value_utf8_value_decoded_correctly() -> None:
    """UTF-8 cookie value is decoded without errors."""
    key = _make_aes_key()
    domain_hash = b"\x00" * 32
    plaintext = domain_hash + b"session=abc123&user=test"
    blob = _encrypt_cookie(plaintext, key)

    result = _decode_cookie_value(blob, key)

    assert result == "session=abc123&user=test"


def test_decode_cookie_value_empty_value_after_domain_hash() -> None:
    """Empty value after 32-byte domain hash returns empty string."""
    key = _make_aes_key()
    domain_hash = b"\x00" * 32
    blob = _encrypt_cookie(domain_hash, key)

    result = _decode_cookie_value(blob, key)

    assert result == ""


# ---------------------------------------------------------------------------
# _parse_cookie_row — pure parsing helper
# ---------------------------------------------------------------------------


# ── TestParseCookieRow (flattened) ──────────────────────────────────────────


def test_parse_cookie_row_auth_session_id_bytes_returns_dict() -> None:
    """AUTH_SESSION_ID row with bytes value is parsed correctly."""
    row = ("AUTH_SESSION_ID", b"v11\x00\x01\x02")
    result = _parse_cookie_row(row)

    assert result is not None
    assert result["name"] == "AUTH_SESSION_ID"
    assert result["raw"] == b"v11\x00\x01\x02"


def test_parse_cookie_row_keycloak_session_bytes_returns_dict() -> None:
    """KEYCLOAK_SESSION row with bytes value is parsed correctly."""
    row = ("KEYCLOAK_SESSION", b"v11\xab\xcd")
    result = _parse_cookie_row(row)

    assert result is not None
    assert result["name"] == "KEYCLOAK_SESSION"
    assert result["raw"] == b"v11\xab\xcd"


def test_parse_cookie_row_unknown_name_returns_none() -> None:
    """Row with unrecognised cookie name returns None."""
    row = ("SESSION_ID", b"v11\x00\x01")
    result = _parse_cookie_row(row)

    assert result is None


def test_parse_cookie_row_string_value_converted_to_bytes() -> None:
    """String encrypted_value is encoded as latin-1 bytes."""
    # SQLite may return BLOB as str in some configurations
    row = ("AUTH_SESSION_ID", "v11\x00\x01")
    result = _parse_cookie_row(row)

    assert result is not None
    assert isinstance(result["raw"], bytes)
    assert result["raw"] == "v11\x00\x01".encode("latin-1")


def test_parse_cookie_row_empty_name_returns_none() -> None:
    """Empty cookie name returns None."""
    row = ("", b"v11\x00")
    result = _parse_cookie_row(row)

    assert result is None


def test_parse_cookie_row_other_cookie_name_returns_none() -> None:
    """Other valid-looking cookie names that aren't targets return None."""
    for name in ("JSESSIONID", "PHPSESSID", "_ga", "csrf_token"):
        row = (name, b"v11\x00")
        result = _parse_cookie_row(row)
        assert result is None, f"Expected None for {name}"
