"""Gmail API retry classification — extracted from commands/gmail/sync.py
(implementation change) so commands/ stays a thin adapter (Constitution Principle I).

Mirrors the equivalent domain-layer predicates in sf/client.py
(_is_sf_transient) and ingest/docs.py (_is_transient_http_error).
"""

import json
import logging
from typing import Any

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from fieldkit.config.retry import RETRY_TRANSIENT_STATUSES, transient_retry
from fieldkit.errors import GmailAuthError

logger = logging.getLogger("gmail-sync")

# Google's classic API error shape puts the specific reason in
# error.errors[].reason for 403s. These reasons indicate quota/rate-limit
# exhaustion (transient) rather than a genuine permission failure.
_GMAIL_QUOTA_REASONS = frozenset({"quotaExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"})


def _normalize_gmail_http_error(exc: HttpError) -> GmailAuthError | None:
    """Return a sanitized auth error for permanent Gmail authentication failures."""
    status = exc.resp.status
    if status == 401:
        return GmailAuthError("Gmail authentication failed (HTTP 401). Re-authenticate and retry.")
    if status == 403 and not _is_gmail_transient(exc):
        return GmailAuthError(
            "Gmail API access denied (HTTP 403). The Gmail API may be disabled for this OAuth client, "
            "or the authenticated account may lack permission. Re-authenticate or verify API access."
        )
    return None


def _is_gmail_transient(exc: BaseException) -> bool:
    """True for retryable Gmail API HTTP errors.

    The status check sources ``RETRY_TRANSIENT_STATUSES`` so it cannot drift from the
    Salesforce and Google Docs predicates.

    A 403 is additionally transient when the body names a quota or rate-limit reason
    (historic regression). That is a Gmail-specific *body* inspection, deliberately not a change to
    the shared status set: 403 stays permanent for Salesforce and Docs, where it means a
    genuine permission failure and retrying it three times just delays the error.
    """
    if isinstance(exc, RefreshError):
        return bool(exc.retryable)
    if not isinstance(exc, HttpError):
        return False
    if exc.resp.status in RETRY_TRANSIENT_STATUSES:
        return True
    if exc.resp.status == 403:
        return _is_gmail_quota_error(exc)
    return False


def _is_gmail_quota_error(exc: HttpError) -> bool:
    try:
        payload = json.loads(exc.content)
        reasons = {e.get("reason") for e in payload.get("error", {}).get("errors", [])}
    except (ValueError, TypeError, AttributeError):
        # Malformed, empty, or non-JSON body — treat as a genuine 403, not a quota
        # blip. Narrowed from suppress(Exception), which would also have swallowed a
        # real defect in this parsing and silently reported "not transient".
        return False
    return bool(reasons & _GMAIL_QUOTA_REASONS)


def warn_recoverable_sync_error(exc: Exception, message: str) -> None:
    """Log a recoverable sync error while preserving auth failures."""
    if isinstance(exc, GmailAuthError):
        raise exc
    logger.warning(message, exc, exc_info=True)


# wait_max=30 overrides the 10s default: a Gmail per-user rate-limit 429 clears on a
# ~30s horizon, so the shorter default ceiling exhausts attempts before the quota
# window reopens. Every call site below is a read (list/get), hence transient_retry.
@transient_retry(_is_gmail_transient, logger, wait_max=30)
def _api_call_with_retry(fn: Any, *args: Any, context: str = "", **kwargs: Any) -> Any:
    """Call a Gmail API function, retrying on transient HTTP errors (implementation note).

    Migrated from manual retry loop to tenacity. Retries on quota/rate-limit 403
    and 429/500/502/503/504. HTTP 401 and permanent 403 errors raise
    GmailAuthError (EXIT_AUTH 2). All other HttpError codes propagate immediately.
    """
    try:
        return fn(*args, **kwargs)
    except RefreshError as exc:
        if exc.retryable:
            raise
        raise GmailAuthError("Gmail authentication refresh failed. Re-authenticate and retry.") from exc
    except HttpError as exc:
        if auth_error := _normalize_gmail_http_error(exc):
            raise auth_error from exc
        raise
