"""Canonical exceptions for the gmail domain.

Import from this module rather than defining locally in decay_domain or query_domain.
"""

from fieldkit.errors import FieldkitError, GmailSyncPartialError, SQLiteSnapshotError


class GmailDbNotFoundError(GmailSyncPartialError):
    """Raised when the Gmail cache database file does not exist."""


class GmailIndexMissingError(FieldkitError):
    """Raised when the Gmail index has not been built (no indexed threads)."""


class GmailSchemaError(SQLiteSnapshotError):
    """Raised when a Gmail cache lacks schema required by current readers."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="unverified")
