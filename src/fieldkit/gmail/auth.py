"""Google OAuth credentials and Gmail API service construction."""

import contextlib
import logging
import sys
from functools import cache
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from fieldkit.config import GOOGLE_OAUTH_SCOPES, get_fieldkit_home, get_google_token_path, resolve_oauth_credentials
from fieldkit.config.dotenv import load_dotenv_safe as load_dotenv
from fieldkit.errors import GmailAuthError
from fieldkit.google_oauth import build_google_service, refresh_google_credentials, write_google_token

SCOPES = GOOGLE_OAUTH_SCOPES
log = logging.getLogger("gmail-sync")


@cache
def _data_dir() -> Path:
    return get_fieldkit_home() / "data"


def _get_token_path() -> Path:
    return get_google_token_path()


def get_gmail_service() -> Any:
    """Authenticate with Google OAuth and return a Gmail API service."""
    with contextlib.suppress(Exception):
        load_dotenv(dotenv_path=_data_dir().parent / ".env", override=False)
    load_dotenv(override=False)
    token_path = _get_token_path()
    creds: Credentials | None = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)  # type: ignore[no-untyped-call]
        except ValueError:
            log.warning("Google token file is invalid or has the wrong format; authorization is required.")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing access token…")
            refresh_google_credentials(creds, token_path)
        else:
            client_id, client_secret = resolve_oauth_credentials()
            if not client_id or not client_secret:
                raise GmailAuthError(
                    "Gmail OAuth credentials not configured. "
                    "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET "
                    "in the configured workspace environment or run 'fieldkit init' to configure them."
                )
            if not sys.stdin.isatty():
                raise GmailAuthError(
                    "Gmail OAuth requires an interactive terminal to authorize, but stdin is not a TTY "
                    "(running as a systemd unit, cron job, or piped context). Run 'fieldkit gmail sync' "
                    "in an interactive terminal — a URL will be printed for you to open in a browser to "
                    "complete the consent flow. Then re-run your automated context."
                )
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0, open_browser=False)  # pyright: ignore[reportAssignmentType]
            write_google_token(token_path, creds.to_json())  # pyright: ignore[reportOptionalMemberAccess]
            log.info("Google token cached.")

    return build_google_service("gmail", "v1", creds)
