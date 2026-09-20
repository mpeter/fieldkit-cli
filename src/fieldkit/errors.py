"""Shared exception hierarchy for all fieldkit domain errors.

All domain exceptions inherit from FieldkitError so callers can catch
the entire hierarchy with a single except clause. Auth-specific failures
inherit from AuthError, which maps to EXIT_AUTH (2) in cli_exit.py.

Import hierarchy
----------------
This module has zero dependencies — it imports nothing from fieldkit or
any third-party library. Any domain module may safely import from here.
"""

from typing import Literal


class FieldkitError(Exception):
    """Base class for all fieldkit domain exceptions."""


class MissingOptionalDependencyError(FieldkitError):
    """Raised before dispatch when a command's declared install profile is incomplete."""

    def __init__(self, command: str, profile: str, missing_import_roots: tuple[str, ...]) -> None:
        self.command = command
        self.profile = profile
        self.missing_import_roots = missing_import_roots
        missing = ", ".join(missing_import_roots)
        super().__init__(
            f"Command 'fieldkit {command}' requires the '{profile}' optional profile "
            f"(missing imports: {missing}).\n"
            f"Install it with one of:\n"
            f"  pip install 'fieldkit-cli[{profile}]'\n"
            f"  uv tool install 'fieldkit-cli[{profile}]'"
        )


class AuthError(FieldkitError):
    """Raised when authentication or authorisation fails.

    Caught by cli_exit.cli_main() and mapped to EXIT_AUTH (2), which
    signals orchestrators to schedule a re-auth flow rather than treating
    the failure as a data error requiring investigation.
    """


class WebDataError(FieldkitError):
    """Raised when a local web or CLI-backed data provider cannot produce its payload."""


class GmailAuthError(AuthError):
    """Raised when Gmail API access is denied (HTTP 403 with quota/permission error).

    Caught by cli_exit.cli_main() and mapped to EXIT_AUTH (2).
    Replaces the previous sys.exit(2) call in gmail/sync.py (implementation note/CR-001 fix).
    """


class GmailSyncPartialError(FieldkitError):
    """Raised after Gmail sync persists all safe work but omits one or more messages."""


class GmailSyncRestartRequiredError(GmailSyncPartialError):
    """Raised after resetting an expired forced sync to a safe retry boundary."""


class GoogleCredentialRefreshRetryableError(FieldkitError):
    """Raised when a Google credential refresh failed but a retry may succeed."""


class PursuitStaleError(FieldkitError):
    """Raised when a pursuit is found to be stale. Maps to EXIT_PARTIAL (1)."""


class FrontmatterStalenessError(FieldkitError):
    """Raised when a pursuit frontmatter file is stale relative to the SF live record.

    Maps to EXIT_PARTIAL (1) in cli_main() — the agent should schedule a retry
    after triggering a fresh SF sync.  Do NOT confuse with data errors (EXIT_DATA=3).

    The exception message identifies the file path so callers can surface it
    to the user.
    """


# ---------------------------------------------------------------------------
# LLM error types (canonical home; import from fieldkit.errors — no re-exports)
# ---------------------------------------------------------------------------

LLMErrorCategory = Literal["auth", "rate-limit", "general"]


class LLMError(FieldkitError):
    """Uniform error wrapper for all LLM provider failures.

    Attributes:
        category: one of "auth", "rate-limit", "general"
        original: the underlying exception, if any
    """

    def __init__(self, message: str, category: LLMErrorCategory = "general", original: Exception | None = None):
        super().__init__(message)
        self.category: LLMErrorCategory = category
        self.original = original

    def __str__(self) -> str:
        return f"[LLMError/{self.category}] {super().__str__()}"
