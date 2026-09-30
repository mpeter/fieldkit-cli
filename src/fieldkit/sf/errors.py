"""Salesforce exceptions and configuration-independent reauthentication guidance."""

from fieldkit.errors import AuthError, FieldkitError


class SFAuthError(AuthError):
    """Raised when the SF session ID is expired or invalid (HTTP 401).

    The caller should prompt the user to run ``fieldkit auth sf``
    to inject a fresh sid cookie from browser DevTools at yourorg.my.salesforce.com.
    """


class SFNotFoundError(FieldkitError):
    """Raised when the requested SF record does not exist (HTTP 404)."""


class SFDataAccessError(FieldkitError):
    """Raised when Salesforce denies access to a requested record (HTTP 403)."""


class SFAPIError(FieldkitError):
    """Raised on non-auth HTTP errors, connection failures, or unexpected responses."""


class SFConditionalWriteConflict(FieldkitError):
    """Raised when Salesforce rejects a guarded mutation as stale (HTTP 412)."""


class SFConditionalWriteOutcomeUnknown(FieldkitError):
    """Raised when a one-shot guarded mutation may have reached Salesforce."""


def reauth_hint_message() -> str:
    """Return fixed reauthentication guidance without reading private configuration."""
    return "run 'fieldkit auth sf' — copy the 'sid' cookie from your Salesforce org's browser DevTools"
