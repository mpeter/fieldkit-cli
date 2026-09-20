"""fieldkit.cli_exit — Canonical exit-code taxonomy for all fieldkit pipeline entry points.

Exit Code Taxonomy
------------------
All pipeline CLIs MUST exit with one of these four codes so that agents and
orchestrators can classify failures without parsing stdout.

    EXIT_SUCCESS  (0) — Operation completed successfully, or nothing to do.
    EXIT_PARTIAL  (1) — Partial success: some records processed, some failed.
                        Retry may help; investigation is optional.
    EXIT_AUTH     (2) — Authentication / credential failure.
                        User action required before retry (re-login, refresh token, etc.).
    EXIT_DATA     (3) — Data or validation error.
                        Investigation required; retrying without a fix will not help.

Exit-Code Boundary (two-layer model)
-------------------------------------
The exit-code guarantee is enforced at two layers:

1. Per-command: ``cli_main()`` context manager wraps a command's body and converts
   any exception to ``sys.exit(code)`` before it reaches the dispatcher.

2. Dispatcher backstop: ``__main__.main()`` contains a terminal
   ``except Exception`` that calls ``handle_cli_exception(exc)`` and returns the
   code for any exception that escapes a leaf command not wrapped in ``cli_main()``.

Both layers delegate to ``handle_cli_exception()`` — the single source of truth
for the exception→code mapping. ``KeyboardInterrupt`` (``BaseException``) is
intentionally not caught by either layer; the shell maps it to exit 130.

Usage
-----
    from fieldkit.cli_exit import cli_main, EXIT_SUCCESS, EXIT_AUTH, EXIT_DATA

    def main() -> None:
        with cli_main():
            # your pipeline logic here
            ...

    if __name__ == "__main__":
        main()

The ``cli_main`` context manager catches known exception types and maps them to
the appropriate exit code before calling sys.exit().  All CLI entry points SHOULD
use ``cli_main()`` as a context manager; the dispatcher backstop guarantees the
taxonomy even for groups that do not (per AGENTS.md two-layer boundary).
"""

import sys
import traceback
from collections.abc import Generator
from contextlib import contextmanager

from fieldkit.config import ConfigError
from fieldkit.errors import (
    AuthError,
    FieldkitError,
    FrontmatterStalenessError,
    GmailSyncPartialError,
    GmailSyncRestartRequiredError,
    GoogleCredentialRefreshRetryableError,
    LLMError,
    MissingOptionalDependencyError,
    PursuitStaleError,
)

# ---------------------------------------------------------------------------
# Exit code constants
# ---------------------------------------------------------------------------

EXIT_SUCCESS: int = 0
"""Operation completed successfully, or there was nothing to do."""

EXIT_PARTIAL: int = 1
"""Partial success: some records processed, some failed.
Retry may help; investigation is optional."""

EXIT_AUTH: int = 2
"""Authentication or credential failure.
User action is required before a retry will succeed (re-login, token refresh, etc.)."""

EXIT_DATA: int = 3
"""Data or validation error.
Investigation is required; retrying without a fix will not help."""

# ---------------------------------------------------------------------------
# Shared exception→code mapping (single source of truth)
# ---------------------------------------------------------------------------


def _config_error_prefix(exc: BaseException) -> str:
    """Keep optional-profile guidance clean while retaining config diagnostics."""
    if isinstance(exc, MissingOptionalDependencyError):
        return ""
    return "Config error — investigation required: "


def _partial_result_message(
    exc: FrontmatterStalenessError | GmailSyncPartialError | GoogleCredentialRefreshRetryableError,
) -> str:
    """Render a partial-result diagnostic without making domain code CLI-aware."""
    if isinstance(exc, GmailSyncRestartRequiredError):
        return str(exc)
    return f"[cli_exit] Partial result — retry may help: {exc}"


def handle_cli_exception(exc: BaseException) -> int:
    """Map any exception to a canonical exit code and write a diagnostic to stderr.

    This is the single source of truth for the exception→code taxonomy. Both
    ``cli_main()`` and the ``__main__.main()`` dispatcher backstop delegate here
    so the mapping cannot drift between the two call sites.

    The clause order is significant and must not be changed:
    - Partial-result ``FieldkitError`` subclasses are tested before ``AuthError``
      so that they map to EXIT_PARTIAL rather than falling through to EXIT_DATA.
    - ``AuthError`` must precede ``PursuitStaleError`` and ``LLMError`` so that
      auth failures from any domain are caught at the right code.
    - The base ``FieldkitError`` clause must be last among the isinstance checks
      (before the broad ``else``) and uses an exact-type check (``type(exc) is
      FieldkitError``), not ``isinstance()``, so only literal ``FieldkitError(...)``
      instances raised directly at a call site get the clean one-liner; every other
      ``FieldkitError`` subclass (including ones with no dedicated clause above,
      like ``SFAPIError`` or ``DocNotFoundError``) falls through to the broad
      ``else`` and keeps its traceback.

    Args:
        exc: The exception to map. Accepts ``BaseException`` so the caller can
            pass any caught value, but only ``Exception`` subclasses produce a
            structured code — callers should not pass ``KeyboardInterrupt``
            here; let it propagate to exit 130.

    Returns:
        The canonical exit code (0-3) for the exception. Does NOT call
        ``sys.exit()`` — the caller decides whether to exit or return the code.

    Note:
        Always writes an appropriate diagnostic line or traceback to stderr
        as a side effect. Uses ``print()`` rather than ``click.echo()``
        intentionally — ``cli_exit.py`` does not import ``click`` to keep
        this module's dependency footprint minimal (only ``fieldkit.config``
        and ``fieldkit.errors``).
    """
    if isinstance(exc, (MissingOptionalDependencyError, ConfigError)):
        print(f"[cli_exit] {_config_error_prefix(exc)}{exc}", file=sys.stderr)
        return EXIT_DATA
    elif isinstance(exc, (FrontmatterStalenessError, GmailSyncPartialError, GoogleCredentialRefreshRetryableError)):
        print(_partial_result_message(exc), file=sys.stderr)
        return EXIT_PARTIAL
    elif isinstance(exc, AuthError):
        # AuthError and all subclasses (SFAuthError, GmailAuthError, ShadowbotAuthError, etc.) → EXIT_AUTH (2).
        # Placed before PursuitStaleError and LLMError so auth failures from any domain are caught here.
        print(f"[cli_exit] Auth error — user action required: {exc}", file=sys.stderr)
        return EXIT_AUTH
    elif isinstance(exc, PursuitStaleError):
        # PursuitStaleError: pursuit is stale relative to SF live record → EXIT_PARTIAL (1).
        print(f"[cli_exit] Pursuit stale check error: {exc}", file=sys.stderr)
        return EXIT_PARTIAL
    elif isinstance(exc, LLMError):
        if exc.category == "auth":
            print(f"[cli_exit] Auth failure — user action required: {exc}", file=sys.stderr)
            return EXIT_AUTH
        elif exc.category == "rate-limit":
            print(f"[cli_exit] Rate limit — partial failure: {exc}", file=sys.stderr)
            return EXIT_PARTIAL
        else:
            print("[cli_exit] LLM error — investigation required:", file=sys.stderr)
            traceback.print_exception(exc, file=sys.stderr)
            return EXIT_DATA
    elif type(exc) is FieldkitError:
        # Exact base FieldkitError (not a subclass): a deliberate, descriptive,
        # user-facing abort raised directly as `FieldkitError(...)` at the call site
        # (e.g. the implementation note empty-payload wipe guard, implementation note duplicate-key guard).
        # Print the message cleanly — no traceback — so these call sites get a clean
        # one-liner without hand-rolling click.echo() + SystemExit. Deliberately an
        # exact-type check, not isinstance(): subclasses like SFAPIError,
        # TranscribeError, and DocNotFoundError represent real failures (e.g. an
        # HTTP 500) that still need their traceback for debugging, so they must fall
        # through to the broad `else` below. Must stay after every more specific
        # FieldkitError subclass clause above (ConfigError is not a FieldkitError
        # subclass and is unaffected; dedicated partial-result subclasses are
        # tested earlier and keep their EXIT_PARTIAL route).
        print(f"[cli_exit] {exc}", file=sys.stderr)
        return EXIT_DATA
    else:
        # Broad fallthrough — any exception not matched above.
        # Guarantees a structured exit code (EXIT_DATA=3) and a traceback on stderr.
        print("[cli_exit] Unhandled exception — investigation required:", file=sys.stderr)
        traceback.print_exception(exc, file=sys.stderr)
        return EXIT_DATA


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@contextmanager
def cli_main() -> Generator[None, None, None]:
    """Context manager that maps exceptions to canonical exit codes.

    Catches:
        ConfigError                      → EXIT_DATA (3) — missing/invalid config
        FrontmatterStalenessError        → EXIT_PARTIAL (1) — file modified since last read
        GmailSyncPartialError            → EXIT_PARTIAL (1) — messages omitted during sync
        AuthError (and subclasses)       → EXIT_AUTH (2) — covers SFAuthError, ShadowbotAuthError, etc.
        PursuitStaleError                → EXIT_PARTIAL (1) — pursuit stale check failed
        LLMError(category='auth')        → EXIT_AUTH (2)
        LLMError(category='rate-limit')  → EXIT_PARTIAL (1)
        LLMError(any other category)     → EXIT_DATA (3) with traceback to stderr
        FieldkitError (exact type only)  → EXIT_DATA (3) with a clean one-liner, no traceback
        Any other Exception              → EXIT_DATA (3) with traceback to stderr

    On clean exit (no exception), the context manager returns normally and the
    caller's own sys.exit() (or implicit exit 0) takes effect.

    Example::

        def main() -> None:
            with cli_main():
                run_pipeline()

    Raises:
        SystemExit — always raised on any exception path inside the block.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001  # broad catch is intentional: this is the per-command boundary
        sys.exit(handle_cli_exception(exc))
