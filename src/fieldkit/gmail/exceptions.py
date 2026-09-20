"""Canonical exceptions for the gmail domain.

Import from this module rather than defining locally in decay_domain or query_domain.
"""

from fieldkit.errors import FieldkitError


class GmailDbNotFoundError(FieldkitError):
    """Raised when the Gmail cache database file does not exist."""


class GmailIndexMissingError(FieldkitError):
    """Raised when the Gmail index has not been built (no indexed threads)."""
