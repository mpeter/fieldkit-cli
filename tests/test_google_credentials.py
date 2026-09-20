"""Regression coverage for shared Google OAuth credential handling."""

import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError

from fieldkit.errors import GmailAuthError, GoogleCredentialRefreshRetryableError
from fieldkit.google_oauth import refresh_google_credentials, write_google_token

pytestmark = pytest.mark.unit


def test_write_google_token_replaces_existing_content_with_private_mode(tmp_path: Path) -> None:
    """Refreshing a token replaces a formerly permissive file with mode 0600."""
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token": "old"}', encoding="utf-8")
    token_path.chmod(0o644)

    write_google_token(token_path, '{"token": "new"}')

    assert token_path.read_text(encoding="utf-8") == '{"token": "new"}'
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_write_google_token_rejects_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    """A configured token symlink cannot redirect a credential refresh write."""
    target = tmp_path / "target.json"
    target.write_text('{"token": "original"}', encoding="utf-8")
    token_path = tmp_path / "token.json"
    token_path.symlink_to(target)

    with pytest.raises(OSError, match="symlink"):
        write_google_token(token_path, '{"token": "replacement"}')

    assert target.read_text(encoding="utf-8") == '{"token": "original"}'


def test_write_google_token_preserves_existing_content_when_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed refresh write leaves the last known-good token intact."""
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token": "old"}', encoding="utf-8")

    monkeypatch.setattr("fieldkit.google_oauth.os.fsync", lambda _: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        write_google_token(token_path, '{"token": "new"}')

    assert token_path.read_text(encoding="utf-8") == '{"token": "old"}'
    assert list(tmp_path.glob(".token.json.*.tmp")) == []


def test_refresh_google_credentials_maps_permanent_refresh_failure_to_auth_error() -> None:
    """A revoked OAuth refresh token requires re-authentication, never a data-error exit."""
    credentials = MagicMock()
    credentials.refresh.side_effect = RefreshError("invalid_grant", retryable=False)

    with pytest.raises(GmailAuthError, match="Re-authenticate"):
        refresh_google_credentials(credentials, Path("token.json"))


def test_refresh_google_credentials_preserves_retryable_failures_for_a_retry_exit() -> None:
    """Temporary refresh failures are distinct from a revoked credential."""
    credentials = MagicMock()
    credentials.refresh.side_effect = RefreshError("temporarily unavailable", retryable=True)

    with pytest.raises(GoogleCredentialRefreshRetryableError, match="retry"):
        refresh_google_credentials(credentials, Path("token.json"))
