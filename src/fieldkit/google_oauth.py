"""Shared Google OAuth refresh and token-persistence behavior."""

import os
import tempfile
from pathlib import Path
from typing import Any

from fieldkit.errors import GmailAuthError, GoogleCredentialRefreshRetryableError


def write_google_token(token_path: Path, payload: str) -> None:
    """Atomically replace one Google token with owner-only permissions."""
    if token_path.is_symlink():
        raise OSError(f"Refusing to replace symlinked Google token: {token_path}")
    token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if token_path.parent.is_symlink():
        raise OSError(f"Refusing to write Google token through symlinked directory: {token_path.parent}")
    if token_path.exists() and not token_path.is_file():
        raise OSError(f"Google token path must be a regular file: {token_path}")

    fd, temporary_name = tempfile.mkstemp(
        dir=token_path.parent,
        prefix=f".{token_path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as token_file:
            token_file.write(payload)
            token_file.flush()
            os.fsync(token_file.fileno())
        temporary_path.replace(token_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def refresh_google_credentials(credentials: Any, token_path: Path) -> None:
    """Refresh and persist Google OAuth credentials with accurate failure classes."""
    from google.auth.exceptions import RefreshError

    try:
        credentials.refresh(_refresh_request())
    except RefreshError as exc:
        if exc.retryable:
            raise GoogleCredentialRefreshRetryableError(
                "Google credential refresh is temporarily unavailable; retry later."
            ) from exc
        raise GmailAuthError("Google credential refresh failed. Re-authenticate and retry.") from exc
    write_google_token(token_path, credentials.to_json())


def _refresh_request() -> Any:
    """Create the optional Google auth transport only when an OAuth refresh runs."""
    from google.auth.transport.requests import Request

    return Request()
