"""Unit tests for fieldkit.watch.preflight (implementation note).

All tests are isolated: no real filesystem access (tmp_path), no real HTTP
calls (monkeypatch), no live credentials.

historic regression: _check_llm_available() migrated from inline os.environ.get() checks
to llm_disabled() from fieldkit.config.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.watch.preflight import (
    _check_gmail_token,
    _check_llm_available,
    _check_mcp_reachable,
    _check_sf_session,
    preflight_check,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_cookie_file(path: Path, *, has_sid: bool = True) -> None:
    """Write a minimal sf-cookies.json to *path*."""
    cookies = []
    if has_sid:
        cookies.append({"name": "sid", "domain": "my.salesforce.com", "value": "abc123"})
    path.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")


def _write_gmail_token(path: Path, *, expired: bool = False) -> None:
    """Write a minimal google-oauth-token.json to *path*."""
    if expired:
        expiry = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    else:
        expiry = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    path.write_text(json.dumps({"expiry": expiry, "token": "tok"}), encoding="utf-8")


# ---------------------------------------------------------------------------
# _check_sf_session
# ---------------------------------------------------------------------------


def test_sf_session_missing_file(tmp_path: Path) -> None:
    """Returns an error message when the cookie file does not exist."""
    cookie_path = tmp_path / "sf-cookies.json"  # does not exist

    # Patch at the definition site (lazy import inside the function).
    with patch("fieldkit.config.get_cookie_file", return_value=cookie_path):
        result = _check_sf_session()

    assert result is not None
    assert "SF session not found" in result
    assert "auth sf" in result


def test_sf_session_no_sid_cookie(tmp_path: Path) -> None:
    """Returns an error message when the cookie file exists but has no 'sid' entry."""
    cookie_path = tmp_path / "sf-cookies.json"
    _write_cookie_file(cookie_path, has_sid=False)

    with patch("fieldkit.config.get_cookie_file", return_value=cookie_path):
        result = _check_sf_session()

    assert result is not None
    assert "sid" in result
    assert "auth sf" in result


def test_sf_session_valid(tmp_path: Path) -> None:
    """Returns None when the cookie file exists and contains a 'sid' entry."""
    cookie_path = tmp_path / "sf-cookies.json"
    _write_cookie_file(cookie_path, has_sid=True)

    with patch("fieldkit.config.get_cookie_file", return_value=cookie_path):
        result = _check_sf_session()

    assert result is None


def test_sf_session_unreadable_file(tmp_path: Path) -> None:
    """Returns an error message when the cookie file contains invalid JSON."""
    cookie_path = tmp_path / "sf-cookies.json"
    cookie_path.write_text("not-json{{{", encoding="utf-8")

    with patch("fieldkit.config.get_cookie_file", return_value=cookie_path):
        result = _check_sf_session()

    assert result is not None
    assert "unreadable" in result


# ---------------------------------------------------------------------------
# _check_gmail_token
# ---------------------------------------------------------------------------


def test_gmail_token_missing_file(tmp_path: Path) -> None:
    """Returns an error message when the Gmail token file does not exist."""
    token_path = tmp_path / "google-oauth-token.json"

    with patch("fieldkit.config.get_google_token_path", return_value=token_path):
        result = _check_gmail_token()

    assert result is not None
    assert "Gmail OAuth token not found" in result
    assert "doctor google" in result


def test_gmail_token_expired(tmp_path: Path) -> None:
    """Returns an error message when the Gmail token has expired."""
    token_path = tmp_path / "google-oauth-token.json"
    _write_gmail_token(token_path, expired=True)

    with patch("fieldkit.config.get_google_token_path", return_value=token_path):
        result = _check_gmail_token()

    assert result is not None
    assert "expired" in result
    assert "doctor google" in result


def test_gmail_token_valid(tmp_path: Path) -> None:
    """Returns None when the Gmail token exists and has not expired."""
    token_path = tmp_path / "google-oauth-token.json"
    _write_gmail_token(token_path, expired=False)

    with patch("fieldkit.config.get_google_token_path", return_value=token_path):
        result = _check_gmail_token()

    assert result is None


def test_gmail_token_no_expiry_field(tmp_path: Path) -> None:
    """Returns None when the token file has no expiry field (treated as non-expired)."""
    token_path = tmp_path / "google-oauth-token.json"
    token_path.write_text(json.dumps({"token": "tok"}), encoding="utf-8")

    with patch("fieldkit.config.get_google_token_path", return_value=token_path):
        result = _check_gmail_token()

    assert result is None


def test_gmail_token_unreadable(tmp_path: Path) -> None:
    """Returns an error message when the token file contains invalid JSON."""
    token_path = tmp_path / "google-oauth-token.json"
    token_path.write_text("not-json{{{", encoding="utf-8")

    with patch("fieldkit.config.get_google_token_path", return_value=token_path):
        result = _check_gmail_token()

    assert result is not None
    assert "unreadable" in result


# ---------------------------------------------------------------------------
# _check_mcp_reachable
# ---------------------------------------------------------------------------


def test_mcp_connect_error() -> None:
    """Returns an error message when mcpjungle is not reachable (ConnectError)."""
    import httpx

    with patch("httpx.get", side_effect=httpx.ConnectError("connection refused")):
        result = _check_mcp_reachable()

    assert result is not None
    assert "mcpjungle not reachable" in result
    assert "systemctl" in result


def test_mcp_non_200_response() -> None:
    """Returns an error message when mcpjungle responds with a non-200 status."""
    mock_response = MagicMock()
    mock_response.status_code = 503

    with patch("httpx.get", return_value=mock_response):
        result = _check_mcp_reachable()

    assert result is not None
    assert "503" in result


def test_mcp_reachable_200() -> None:
    """Returns None when mcpjungle responds with HTTP 200."""
    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch("httpx.get", return_value=mock_response):
        result = _check_mcp_reachable()

    assert result is None


def test_mcp_unexpected_exception() -> None:
    """Returns an error message (does not raise) on unexpected exceptions."""
    with patch("httpx.get", side_effect=RuntimeError("unexpected")):
        result = _check_mcp_reachable()

    assert result is not None
    assert "mcpjungle check failed" in result


# ---------------------------------------------------------------------------
# _check_llm_available
# ---------------------------------------------------------------------------


def test_llm_no_llm_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns None when NO_LLM env var is set (LLM disabled via llm_disabled())."""
    monkeypatch.setenv("NO_LLM", "1")
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    result = _check_llm_available()

    assert result is None


def test_llm_fieldkit_no_llm_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns None when FIELDKIT_NO_LLM env var is set (primary name via llm_disabled())."""
    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    result = _check_llm_available()

    assert result is None


def test_llm_disabled_via_boundary_patch() -> None:
    """historic regression: _check_llm_available() must gate on llm_disabled(), not inline env checks.

    Patches llm_disabled at the preflight module boundary — proves the call
    site is using the centralised function rather than its own os.environ.get().
    """
    with patch("fieldkit.watch.preflight.llm_disabled", return_value=True):
        result = _check_llm_available()

    assert result is None, (
        "historic regression: _check_llm_available() did not return None when llm_disabled() "
        "returns True — the inline os.environ.get() check was not replaced"
    )


def test_llm_check_uses_llm_disabled_source() -> None:
    """historic regression: _check_llm_available source must reference llm_disabled(), not os.environ."""
    import inspect

    import fieldkit.watch.preflight as _mod

    src = inspect.getsource(_mod._check_llm_available)
    assert "llm_disabled()" in src, (
        "historic regression: _check_llm_available() must call llm_disabled() — inline os.environ.get() check found instead"
    )
    assert 'os.environ.get("FIELDKIT_NO_LLM")' not in src, (
        "historic regression: stale inline FIELDKIT_NO_LLM check still present in _check_llm_available()"
    )
    assert 'os.environ.get("NO_LLM")' not in src, (
        "historic regression: stale inline NO_LLM check still present in _check_llm_available()"
    )


def test_llm_adc_file_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns None when the ADC file exists."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    adc_path = tmp_path / "application_default_credentials.json"
    adc_path.write_text("{}", encoding="utf-8")

    with patch("fieldkit.watch.preflight.Path") as mock_path_cls:
        # Make Path.home() / ... resolve to our tmp file
        mock_home = MagicMock()
        mock_path_cls.home.return_value = mock_home
        mock_home.__truediv__ = lambda self, x: mock_home
        mock_home.exists.return_value = True

        result = _check_llm_available()

    assert result is None


def test_llm_google_credentials_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns None when GOOGLE_APPLICATION_CREDENTIALS is set."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/path/to/creds.json")

    result = _check_llm_available()

    assert result is None


def test_llm_no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Returns an error message when no ADC file and no env var are present."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    # Point ADC path to a non-existent file
    with patch("fieldkit.watch.preflight.Path") as mock_path_cls:
        mock_home = MagicMock()
        mock_path_cls.home.return_value = mock_home
        mock_home.__truediv__ = lambda self, x: mock_home
        mock_home.exists.return_value = False

        result = _check_llm_available()

    assert result is not None
    assert "Application Default Credentials" in result
    assert "gcloud auth" in result


# ---------------------------------------------------------------------------
# preflight_check — integration of all checks
# ---------------------------------------------------------------------------


def test_preflight_dry_run_skips_mcp_and_llm(tmp_path: Path) -> None:
    """dry_run=True skips 'mcp' and 'llm' checks entirely."""
    cookie_path = tmp_path / "sf-cookies.json"
    _write_cookie_file(cookie_path, has_sid=True)
    token_path = tmp_path / "google-oauth-token.json"
    _write_gmail_token(token_path, expired=False)

    with (
        patch("fieldkit.config.get_cookie_file", return_value=cookie_path),
        patch("fieldkit.config.get_google_token_path", return_value=token_path),
        patch("httpx.get", side_effect=AssertionError("httpx.get must not be called in dry_run")),
    ):
        failures = preflight_check(["sf", "gmail", "mcp", "llm"], dry_run=True)

    assert failures == []


def test_preflight_returns_all_failures(tmp_path: Path) -> None:
    """preflight_check collects failures from all failing checks."""
    missing_cookie = tmp_path / "sf-cookies.json"  # does not exist
    missing_token = tmp_path / "google-oauth-token.json"  # does not exist

    with (
        patch("fieldkit.config.get_cookie_file", return_value=missing_cookie),
        patch("fieldkit.config.get_google_token_path", return_value=missing_token),
    ):
        failures = preflight_check(["sf", "gmail"])

    assert len(failures) == 2
    assert any("SF session" in f for f in failures)
    assert any("Gmail OAuth" in f for f in failures)


def test_preflight_unknown_service_ignored() -> None:
    """Unknown service names are silently ignored."""
    failures = preflight_check(["nonexistent-service"])
    assert failures == []


def test_preflight_empty_services() -> None:
    """Empty services list returns no failures."""
    failures = preflight_check([])
    assert failures == []


def test_preflight_all_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns empty list when all checks pass."""
    cookie_path = tmp_path / "sf-cookies.json"
    _write_cookie_file(cookie_path, has_sid=True)
    token_path = tmp_path / "google-oauth-token.json"
    _write_gmail_token(token_path, expired=False)

    monkeypatch.setenv("NO_LLM", "1")

    mock_response = MagicMock()
    mock_response.status_code = 200

    with (
        patch("fieldkit.config.get_cookie_file", return_value=cookie_path),
        patch("fieldkit.config.get_google_token_path", return_value=token_path),
        patch("httpx.get", return_value=mock_response),
    ):
        failures = preflight_check(["sf", "gmail", "mcp", "llm"])

    assert failures == []
