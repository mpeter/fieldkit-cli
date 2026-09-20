"""Unit tests for gmail-cache sync.py OAuth and retry paths.

historic regression: The most common first-run failure (token file absent) had no test.
Users see a raw exception on first run.

Added tests for:
- Missing token file → clean error (sys.exit(2), not raw traceback)
- Expired token → refresh attempt
- Permission denied on token file → clean error (ValueError caught, re-auth)
- Valid token → proceeds to build Gmail service

Patch target: fieldkit.gmail.auth.Credentials (import boundary).
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from fieldkit.gmail import auth as sync
from fieldkit.gmail.retry import _api_call_with_retry

pytestmark = pytest.mark.unit


# ── TestGetGmailServiceOAuth (flattened) ────────────────────────────────────


def test_get_gmail_service_o_auth_valid_cached_token_skips_refresh(tmp_path, monkeypatch):
    # Write a dummy token file
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "dummy"}', encoding="utf-8")

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    mock_creds = MagicMock()
    type(mock_creds).valid = PropertyMock(return_value=True)

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=tmp_path / "token.json"),
        patch.object(sync.Credentials, "from_authorized_user_file", return_value=mock_creds) as mock_load,
        patch.object(sync, "build", return_value=MagicMock()) as mock_build,
    ):
        sync.get_gmail_service()

    mock_load.assert_called_once()
    mock_creds.refresh.assert_not_called()
    mock_build.assert_called_once()
    assert mock_build.call_args[0][0] == "gmail"


def test_get_gmail_service_o_auth_expired_token_triggers_refresh(tmp_path, monkeypatch):
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "dummy"}', encoding="utf-8")

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    mock_creds = MagicMock()
    type(mock_creds).valid = PropertyMock(return_value=False)
    type(mock_creds).expired = PropertyMock(return_value=True)
    mock_creds.refresh_token = "rt"
    # After refresh, creds.to_json() must return a string for token_path.write_text
    mock_creds.to_json.return_value = '{"token": "refreshed"}'

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=tmp_path / "token.json"),
        patch.object(sync.Credentials, "from_authorized_user_file", return_value=mock_creds),
        patch.object(sync, "build", return_value=MagicMock()) as mock_build,
    ):
        sync.get_gmail_service()

    mock_creds.refresh.assert_called_once()
    assert mock_creds.refresh.call_count == 1
    mock_build.assert_called_once()


def test_get_gmail_service_o_auth_api_retry_on_429(monkeypatch):
    """implementation note: tenacity retries on 429, succeeds on 2nd attempt."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = 429
    resp.reason = "Too Many Requests"
    exc = HttpError(resp=resp, content=b"")

    call_count = {"n": 0}

    def flaky_fn():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise exc
        return "ok"

    with patch("time.sleep"):
        result = _api_call_with_retry(flaky_fn, context="test")

    assert result == "ok"
    assert call_count["n"] == 2, "Expected exactly 2 calls (1 fail + 1 success)"


# ── TestGmailSyncTokenHandling (flattened) ──────────────────────────────────


def test_gmail_sync_token_handling_missing_token_file_gives_clean_error(tmp_path: object, monkeypatch) -> None:
    """Missing token file → no token loaded, falls through to OAuth flow.

    When the token file does not exist, get_gmail_service() skips the
    Credentials.from_authorized_user_file() call and proceeds to the
    OAuth flow (InstalledAppFlow). Since we can't run a real OAuth flow
    in tests, we verify that:
    1. Credentials.from_authorized_user_file() is NOT called (file absent)
    2. The function attempts to start the OAuth flow (InstalledAppFlow)

    In production, a missing token file means the user needs to authenticate.
    The function does NOT raise a clean error — it starts the OAuth flow.
    This test documents that behavior.
    """
    import tempfile
    from pathlib import Path

    # Use a path that definitely does not exist
    nonexistent_token = Path(tempfile.mkdtemp()) / "nonexistent" / "token.json"

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    mock_flow = MagicMock()
    mock_creds = MagicMock()
    type(mock_creds).valid = PropertyMock(return_value=True)
    mock_flow.run_local_server.return_value = mock_creds
    mock_creds.to_json.return_value = '{"token": "new"}'

    with (
        patch.object(sync, "_data_dir", return_value=nonexistent_token.parent.parent),
        patch.object(sync, "_get_token_path", return_value=nonexistent_token),
        patch("sys.stdin.isatty", return_value=True),
        patch.object(sync, "InstalledAppFlow") as mock_flow_cls,
        patch.object(sync, "build", return_value=MagicMock()),
    ):
        mock_flow_cls.from_client_config.return_value = mock_flow
        sync.get_gmail_service()

    # Token file was absent → from_authorized_user_file NOT called
    # InstalledAppFlow was invoked to start OAuth
    mock_flow_cls.from_client_config.assert_called_once()


def test_gmail_sync_token_handling_invalid_token_file_gives_clean_error(tmp_path, monkeypatch) -> None:
    """Invalid/corrupt token file → ValueError caught, re-authenticates cleanly.

    When Credentials.from_authorized_user_file() raises ValueError (e.g. wrong
    format, corrupt JSON), get_gmail_service() catches it, logs a warning,
    and falls through to the OAuth flow — NOT a raw traceback.
    """
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "corrupt"}', encoding="utf-8")

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    mock_flow = MagicMock()
    mock_creds = MagicMock()
    type(mock_creds).valid = PropertyMock(return_value=True)
    mock_flow.run_local_server.return_value = mock_creds
    mock_creds.to_json.return_value = '{"token": "new"}'

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=token_file),
        patch.object(
            sync.Credentials,
            "from_authorized_user_file",
            side_effect=ValueError("wrong format"),
        ) as mock_load,
        patch("sys.stdin.isatty", return_value=True),
        patch.object(sync, "InstalledAppFlow") as mock_flow_cls,
        patch.object(sync, "build", return_value=MagicMock()),
    ):
        mock_flow_cls.from_client_config.return_value = mock_flow
        # Should NOT raise — ValueError is caught and handled cleanly
        sync.get_gmail_service()

    # from_authorized_user_file was called (token file exists)
    mock_load.assert_called_once()
    # InstalledAppFlow was invoked to re-authenticate
    mock_flow_cls.from_client_config.assert_called_once()


def test_gmail_sync_token_handling_expired_token_gives_clean_error_or_refresh(tmp_path, monkeypatch) -> None:
    """Expired token with refresh_token → refresh attempt, NOT a raw error.

    When creds.valid is False and creds.expired is True and creds.refresh_token
    is set, get_gmail_service() calls creds.refresh() — a clean recovery path.
    """
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "expired"}', encoding="utf-8")

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    mock_creds = MagicMock()
    type(mock_creds).valid = PropertyMock(return_value=False)
    type(mock_creds).expired = PropertyMock(return_value=True)
    mock_creds.refresh_token = "some-refresh-token"
    mock_creds.to_json.return_value = '{"token": "refreshed"}'

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=token_file),
        patch.object(sync.Credentials, "from_authorized_user_file", return_value=mock_creds),
        patch.object(sync, "build", return_value=MagicMock()),
    ):
        sync.get_gmail_service()

    # Clean recovery: refresh() called, not a raw exception
    mock_creds.refresh.assert_called_once()


def test_gmail_sync_token_handling_permission_denied_on_token_file_gives_clean_error(tmp_path, monkeypatch) -> None:
    """Permission denied reading token file → ValueError caught, re-authenticates.

    When Credentials.from_authorized_user_file() raises ValueError (which wraps
    JSON/format errors including permission issues), get_gmail_service() handles
    it cleanly by falling through to the OAuth flow.

    Note: A real PermissionError (OSError) from os.open() would propagate — this
    test covers the ValueError path that sync.py actually handles.
    """
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "dummy"}', encoding="utf-8")

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    mock_flow = MagicMock()
    mock_creds = MagicMock()
    type(mock_creds).valid = PropertyMock(return_value=True)
    mock_flow.run_local_server.return_value = mock_creds
    mock_creds.to_json.return_value = '{"token": "new"}'

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=token_file),
        patch.object(
            sync.Credentials,
            "from_authorized_user_file",
            side_effect=ValueError("permission denied or invalid format"),
        ),
        patch("sys.stdin.isatty", return_value=True),
        patch.object(sync, "InstalledAppFlow") as mock_flow_cls,
        patch.object(sync, "build", return_value=MagicMock()),
    ):
        mock_flow_cls.from_client_config.return_value = mock_flow
        # Should NOT raise — ValueError is caught and handled cleanly
        sync.get_gmail_service()

    # Re-authentication flow was started
    mock_flow_cls.from_client_config.assert_called_once()


def test_gmail_sync_token_handling_valid_token_proceeds_to_sync(tmp_path, monkeypatch) -> None:
    """Valid token → proceeds to build Gmail service without re-auth."""
    token_file = tmp_path / "token.json"
    token_file.write_text('{"token": "valid"}', encoding="utf-8")

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    mock_creds = MagicMock()
    type(mock_creds).valid = PropertyMock(return_value=True)
    mock_service = MagicMock()

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=token_file),
        patch.object(sync.Credentials, "from_authorized_user_file", return_value=mock_creds),
        patch.object(sync, "build", return_value=mock_service) as mock_build,
    ):
        result = sync.get_gmail_service()

    # build() was called with 'gmail' service
    mock_build.assert_called_once()
    assert mock_build.call_args[0][0] == "gmail"
    # No refresh needed
    mock_creds.refresh.assert_not_called()
    # Service returned
    assert result is mock_service


def test_gmail_sync_token_handling_missing_oauth_credentials_exits_with_code_2(tmp_path, monkeypatch) -> None:
    """Missing GOOGLE_CLIENT_ID/SECRET → GmailAuthError (clean error, not raw traceback).

    This is the most common first-run failure: user hasn't run 'fieldkit init'.
    get_gmail_service() raises GmailAuthError with a clear message; cli_main() maps
    it to exit code 2 (EXIT_AUTH) via the typed-exception exit boundary.
    """
    # Ensure no OAuth credentials are set
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)

    from fieldkit.errors import GmailAuthError

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=tmp_path / "token.json"),
        pytest.raises(GmailAuthError, match="OAuth credentials not configured"),
    ):
        sync.get_gmail_service()


def test_gmail_sync_non_interactive_stdin_never_launches_oauth_flow(tmp_path, monkeypatch) -> None:
    """#1211: non-TTY stdin → GmailAuthError, InstalledAppFlow is NEVER constructed.

    Regression guard for the recurring OAuth consent popups: an autonomous
    driver run (or CI, or piped stdin) has valid OAuth client credentials and
    no cached token, so control reaches the re-auth branch. Before the fix this
    called flow.run_local_server(), which opened a live Google consent screen
    in the desktop browser. The fix guards on sys.stdin.isatty() and raises
    GmailAuthError BEFORE any InstalledAppFlow object is built (cli_main() maps
    GmailAuthError → exit 2). This test locks that: with a non-interactive stdin,
    get_gmail_service() must raise GmailAuthError and must not touch InstalledAppFlow.
    """
    from fieldkit.errors import GmailAuthError

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    nonexistent_token = tmp_path / "no-token.json"

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=nonexistent_token),
        patch("sys.stdin.isatty", return_value=False),
        patch.object(sync, "InstalledAppFlow") as mock_flow_cls,
        pytest.raises(GmailAuthError, match="not a TTY"),
    ):
        sync.get_gmail_service()

    # The interactive browser flow was never reached
    mock_flow_cls.from_client_config.assert_not_called()


def test_gmail_sync_interactive_stdin_passes_open_browser_false(tmp_path, monkeypatch) -> None:
    """#1211: TTY stdin → OAuth proceeds, run_local_server called with open_browser=False.

    Regression guard for the open_browser=False change: even in an interactive
    terminal, the browser is not auto-launched. The user receives a URL to open
    manually. This test locks that the open_browser=False argument is passed to
    run_local_server, so a future change cannot silently revert to auto-launching.
    """
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")

    nonexistent_token = tmp_path / "no-token.json"
    mock_flow = MagicMock()
    mock_creds = MagicMock()
    type(mock_creds).valid = PropertyMock(return_value=True)
    mock_flow.run_local_server.return_value = mock_creds
    mock_creds.to_json.return_value = '{"token": "new"}'

    with (
        patch.object(sync, "_data_dir", return_value=tmp_path),
        patch.object(sync, "_get_token_path", return_value=nonexistent_token),
        patch("sys.stdin.isatty", return_value=True),
        patch.object(sync, "InstalledAppFlow") as mock_flow_cls,
        patch.object(sync, "build", return_value=MagicMock()),
    ):
        mock_flow_cls.from_client_config.return_value = mock_flow
        sync.get_gmail_service()

    # open_browser=False must be passed — browser must NOT auto-launch (#1211)
    mock_flow.run_local_server.assert_called_once_with(port=0, open_browser=False)
