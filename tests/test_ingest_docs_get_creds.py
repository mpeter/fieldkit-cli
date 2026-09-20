"""Tests for fieldkit.ingest.docs._get_creds — token loading and refresh behavior.

Verifies that:
- token_path=None resolves via get_google_token_path(); an explicit token_path
  never calls it (call-count-0 contract on the `if token_path is None` guard).
- A missing token file raises FileNotFoundError with a helpful message and
  never reaches Credentials.from_authorized_user_file (call-count-0 contract).
- Non-expired credentials are returned as-is without calling .refresh().
- Expired credentials with a refresh_token are refreshed exactly once.
- Expired credentials WITHOUT a refresh_token are NOT refreshed (proves the
  compound `and` condition requires both operands truthy).

All Google auth imports inside ``_get_creds`` are local to the function body,
so patches must target the definition sites (``fieldkit.config.*``,
``google.oauth2.credentials.Credentials.from_authorized_user_file``,
``google.auth.transport.requests.Request``) rather than ``fieldkit.ingest.docs``.
"""

from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from fieldkit.ingest.docs import _get_creds

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_creds(*, expired: bool = False, refresh_token: str | None = "a-refresh-token") -> MagicMock:
    """Build a mock Credentials object with settable expired/refresh_token state."""
    creds = MagicMock()
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.to_json.return_value = "{}"
    return creds


def _make_token_file(tmp_path: Path) -> Path:
    """Create a placeholder token file on disk; content is irrelevant since
    Credentials.from_authorized_user_file is always mocked in these tests."""
    token_file = tmp_path / "token.json"
    token_file.write_text('{"placeholder": true}')
    return token_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_creds_resolves_token_path_from_config_when_none(tmp_path: Path) -> None:
    """token_path=None → resolved via get_google_token_path(); resolved path is used."""
    token_file = _make_token_file(tmp_path)
    mock_creds = _make_creds(expired=False)

    with (
        patch("fieldkit.config.get_google_token_path", return_value=token_file) as mock_get_path,
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds) as mock_load,
    ):
        result = _get_creds()

    mock_get_path.assert_called_once()
    mock_load.assert_called_once_with(str(token_file), scopes=ANY)
    assert result is mock_creds


def test_get_creds_explicit_token_path_never_calls_config_resolver(tmp_path: Path) -> None:
    """token_path=<explicit Path> → get_google_token_path() is NEVER called."""
    token_file = _make_token_file(tmp_path)
    mock_creds = _make_creds(expired=False)

    with (
        patch("fieldkit.config.get_google_token_path") as mock_get_path,
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
    ):
        result = _get_creds(token_path=token_file)

    mock_get_path.assert_not_called()
    assert result is mock_creds


def test_get_creds_missing_token_file_raises_and_never_loads_credentials(tmp_path: Path) -> None:
    """Resolved token_path does not exist → FileNotFoundError; from_authorized_user_file never called."""
    missing_path = tmp_path / "does-not-exist.json"

    with (
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file") as mock_load,
        pytest.raises(FileNotFoundError, match=r"OAuth token not found.*fieldkit gmail sync"),
    ):
        _get_creds(token_path=missing_path)

    mock_load.assert_not_called()


def test_get_creds_non_expired_returns_directly_without_refresh(tmp_path: Path) -> None:
    """Non-expired credentials are returned as-is; .refresh() is never called."""
    token_file = _make_token_file(tmp_path)
    mock_creds = _make_creds(expired=False, refresh_token="a-refresh-token")

    with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds):
        result = _get_creds(token_path=token_file)

    assert result is mock_creds
    mock_creds.refresh.assert_not_called()


def test_get_creds_expired_with_refresh_token_refreshes_once(tmp_path: Path) -> None:
    """Expired creds with a refresh_token → .refresh(Request()) called exactly once."""
    token_file = _make_token_file(tmp_path)
    mock_creds = _make_creds(expired=True, refresh_token="a-refresh-token")

    with (
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds),
        patch("fieldkit.google_oauth._refresh_request") as mock_request,
    ):
        result = _get_creds(token_path=token_file)

    mock_creds.refresh.assert_called_once_with(mock_request.return_value)
    assert result is mock_creds


def test_get_creds_expired_without_refresh_token_does_not_refresh(tmp_path: Path) -> None:
    """Expired creds but falsy refresh_token → .refresh() NOT called (both operands required)."""
    token_file = _make_token_file(tmp_path)
    mock_creds = _make_creds(expired=True, refresh_token=None)

    with patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=mock_creds):
        result = _get_creds(token_path=token_file)

    mock_creds.refresh.assert_not_called()
    assert result is mock_creds
