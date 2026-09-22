"""Unit tests for fieldkit.cli_exit — the sole sys.exit() boundary.

Covers:
  - handle_cli_exception(): the shared exception→code mapping function
  - cli_main(): the per-command context manager (delegates to handle_cli_exception)

Exit scenarios tested:
  1. Normal execution (no exception) → exit 0 (caller's own sys.exit)
  2. SFAuthError → exit 2
  3. ConfigError → exit 3
  4. FrontmatterStalenessError → exit 1
  5. Generic Exception → exit 3 (EXIT_DATA)
  6. SystemExit(42) → passthrough (exit 42)
  7. KeyboardInterrupt → propagates unhandled (not caught by Exception handler)

historic regression: cli_main() had zero tests; a regression here would break all commands silently.
"""

import pytest

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL, EXIT_SUCCESS, cli_main, handle_cli_exception
from fieldkit.config import ConfigError
from fieldkit.errors import (
    AuthError,
    FieldkitError,
    FrontmatterStalenessError,
    GmailAuthError,
    GmailSyncRestartRequiredError,
    GoogleCredentialRefreshRetryableError,
    LLMError,
    MissingOptionalDependencyError,
    PursuitStaleError,
)
from fieldkit.ingest.docs import DocNotFoundError
from fieldkit.llm._transcribe import TranscribeError
from fieldkit.sf.client import SFAPIError, SFAuthError
from fieldkit.shadowbot.auth import ShadowbotAuthError

pytestmark = pytest.mark.unit


def test_exit_code_constants_form_the_documented_taxonomy() -> None:
    """The public exit-code contract remains a dense 0 through 3 taxonomy."""
    assert (EXIT_SUCCESS, EXIT_PARTIAL, EXIT_AUTH, EXIT_DATA) == (0, 1, 2, 3)


# ── TestCliMain (flattened) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc,expected_code",
    [
        (ConfigError("x"), EXIT_DATA),
        (FrontmatterStalenessError("x"), EXIT_PARTIAL),
        (GoogleCredentialRefreshRetryableError("x"), EXIT_PARTIAL),
        (PursuitStaleError("x"), EXIT_PARTIAL),
        (AuthError("x"), EXIT_AUTH),
        (LLMError("x", category="auth"), EXIT_AUTH),
        (LLMError("x", category="rate-limit"), EXIT_PARTIAL),
        (LLMError("x", category="general"), EXIT_DATA),
        (ValueError("x"), EXIT_DATA),
        (RuntimeError("x"), EXIT_DATA),
        (FieldkitError("x"), EXIT_DATA),
        (MissingOptionalDependencyError("meeting", "google", ("google.auth",)), EXIT_DATA),
    ],
)
def test_cli_main_exit_code_routing_table(exc: Exception, expected_code: int) -> None:
    """Parametrized routing table — authoritative contract for all exit-code paths.

    This is the primary regression anchor for cli_main(). Adding a new exception
    type requires adding a row here. A blank row means a handler gap.
    """
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise exc
    assert exc_info.value.code == expected_code


def test_cli_main_normal_exit_is_zero() -> None:
    """No exception inside the block → context manager returns normally (exit 0)."""
    # cli_main() does NOT call sys.exit() on clean exit — it just returns.
    # The caller's own sys.exit(0) or implicit exit 0 takes effect.
    # We verify no SystemExit is raised by the context manager itself.
    with cli_main():
        pass  # no exception


def test_cli_main_systemexit_zero_passthrough() -> None:
    """SystemExit(0) inside the block passes through unchanged."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise SystemExit(0)
    assert exc_info.value.code == 0


def test_cli_main_sfautherror_is_exit_2() -> None:
    """SFAuthError → EXIT_AUTH (2).

    SFAuthError inherits from AuthError, which is caught by the dedicated
    `except AuthError` clause in cli_main(). No type-name string check used.
    """
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise SFAuthError("session expired")
    assert exc_info.value.code == EXIT_AUTH


def test_cli_main_configerror_is_exit_3() -> None:
    """ConfigError → EXIT_DATA (3) — configuration needs investigation."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise ConfigError("config.yaml not found")
    assert exc_info.value.code == EXIT_DATA


def test_cli_main_frontmatter_staleness_error_is_exit_1() -> None:
    """FrontmatterStalenessError (FieldkitError subclass) → EXIT_PARTIAL (1).

    Caught by a dedicated isinstance clause that precedes AuthError.
    The class lives in fieldkit.errors (zero deps).
    """
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise FrontmatterStalenessError("file modified since last read")
    assert exc_info.value.code == EXIT_PARTIAL


def test_cli_main_generic_exception_is_exit_3() -> None:
    """Unhandled Exception → EXIT_DATA (3) with traceback to stderr."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise RuntimeError("unexpected failure")
    assert exc_info.value.code == EXIT_DATA


def test_cli_main_systemexit_passthrough() -> None:
    """SystemExit(42) inside the block passes through unchanged (not caught)."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise SystemExit(42)
    assert exc_info.value.code == 42


def test_cli_main_keyboard_interrupt_propagates_unhandled() -> None:
    """KeyboardInterrupt propagates out of cli_main() (not caught by Exception handler).

    KeyboardInterrupt is a BaseException subclass, not Exception, so it is NOT
    caught by the broad 'except Exception' handler. It propagates to the caller.
    In production, the shell/OS maps an unhandled KeyboardInterrupt to exit 130
    (SIGINT), but cli_main() itself does not intercept it.
    """
    with pytest.raises(KeyboardInterrupt) as exc_info, cli_main():
        raise KeyboardInterrupt
    assert isinstance(exc_info.value, KeyboardInterrupt)


def test_cli_main_llm_auth_error_is_exit_2() -> None:
    """LLMError(category='auth') → EXIT_AUTH (2)."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise LLMError("auth failed", category="auth")
    assert exc_info.value.code == EXIT_AUTH


def test_cli_main_llm_rate_limit_is_exit_1() -> None:
    """LLMError(category='rate-limit') → EXIT_PARTIAL (1)."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise LLMError("rate limited", category="rate-limit")
    assert exc_info.value.code == EXIT_PARTIAL


def test_cli_main_llm_general_error_is_exit_3() -> None:
    """LLMError(category='general') → EXIT_DATA (3).

    This covers the `else` branch in cli_main()'s LLMError handler.
    The category 'general' is the valid production value for non-auth,
    non-rate-limit LLM failures.
    """
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise LLMError("model error", category="general")
    assert exc_info.value.code == EXIT_DATA


def test_cli_main_plain_value_error_is_exit_3() -> None:
    """Plain ValueError (not FrontmatterStalenessError) → EXIT_DATA (3).

    The `except ValueError: raise` escape hatch was removed. Plain ValueError
    now falls through to the broad Exception handler and exits with EXIT_DATA (3).
    """
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise ValueError("bad value")
    assert exc_info.value.code == EXIT_DATA


def test_cli_main_exit_code_constants() -> None:
    """Verify the exit code constants have the expected values."""
    assert EXIT_SUCCESS == 0
    assert EXIT_PARTIAL == 1
    assert EXIT_AUTH == 2
    assert EXIT_DATA == 3


def test_missing_optional_dependency_is_actionable_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = MissingOptionalDependencyError("meeting", "google", ("google.auth", "googleapiclient"))

    exit_code = handle_cli_exception(error)

    stderr = capsys.readouterr().err
    assert exit_code == EXIT_DATA
    assert "meeting" in stderr
    assert "google" in stderr
    assert "pip install 'fieldkit-cli[google]'" in stderr
    assert "uv tool install 'fieldkit-cli[google]'" in stderr
    assert "Traceback" not in stderr


def test_cli_main_shadowbot_auth_error_is_exit_2() -> None:
    """ShadowbotAuthError → EXIT_AUTH (2) — caught via AuthError base class."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise ShadowbotAuthError("token expired")
    assert exc_info.value.code == EXIT_AUTH


def test_cli_main_sf_api_error_is_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    """SFAPIError → EXIT_DATA (3) with a traceback — it's a FieldkitError subclass, not
    the exact base type, so the clean-one-liner FieldkitError clause must not swallow
    the traceback for a real HTTP 500 failure (implementation note scope guard)."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise SFAPIError("HTTP 500 from Salesforce")
    assert exc_info.value.code == EXIT_DATA
    assert "Traceback" in capsys.readouterr().err


def test_cli_main_transcribe_error_is_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    """TranscribeError → EXIT_DATA (3) with a traceback (implementation note scope guard)."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise TranscribeError("Groq API error", category="general")
    assert exc_info.value.code == EXIT_DATA
    assert "Traceback" in capsys.readouterr().err


def test_cli_main_doc_not_found_error_is_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    """DocNotFoundError → EXIT_DATA (3) with a traceback (implementation note scope guard)."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise DocNotFoundError("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
    assert exc_info.value.code == EXIT_DATA
    assert "Traceback" in capsys.readouterr().err


def test_cli_main_pursuit_stale_error_is_exit_1() -> None:
    """PursuitStaleError → EXIT_PARTIAL (1).

    cli_main() catches PursuitStaleError via a dedicated `except PursuitStaleError`
    clause. PursuitStaleError lives in fieldkit.errors (zero deps) and is imported
    directly — no type-name string check needed.
    """
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise PursuitStaleError("No arguments provided")
    assert exc_info.value.code == EXIT_PARTIAL


def test_cli_main_llmerror_canonical_home_is_fieldkit_errors() -> None:
    """Error types have a sole canonical home in fieldkit.errors; no compat re-export survives.

    cli_exit.py imports LLMError from fieldkit.errors directly. fieldkit.llm.core
    imports it from fieldkit.errors for use (raising it), so that binding must be
    the same object. Per R25, no package re-exports these error types anymore:
    LLMError / LLMErrorCategory must not be reachable via fieldkit.llm, and
    FrontmatterStalenessError must not be reachable via fieldkit.pursuit.
    """
    import fieldkit.errors
    import fieldkit.llm
    import fieldkit.llm.core
    import fieldkit.pursuit

    # core uses the canonical class (so `except LLMError` in synthesize() matches
    # what callers catch); attribute access, not a from-import that blesses a path.
    assert fieldkit.errors.LLMError is fieldkit.llm.core.LLMError, (
        "fieldkit.errors.LLMError is not the same object as fieldkit.llm.core.LLMError."
    )

    # R25: the three error types are gone from every non-canonical package surface.
    for module, name in (
        (fieldkit.llm, "LLMError"),
        (fieldkit.llm, "LLMErrorCategory"),
        (fieldkit.pursuit, "FrontmatterStalenessError"),
    ):
        assert not hasattr(module, name), (
            f"{module.__name__} must NOT re-export {name} (R25: no backward-compat shims). "
            "Import it from fieldkit.errors."
        )
        assert name not in getattr(module, "__all__", ()), (
            f"{name} must not appear in {module.__name__}.__all__ (R25 shim removed)."
        )


def test_cli_main_click_exit_none_is_treated_as_error() -> None:
    """click.exceptions.Exit(None) must not silently return exit code 0.

    NOTE: This test exercises fieldkit.__main__.main(), not cli_main() directly.
    It lives here because it is closely related to the exit-code contract and was
    added alongside the cli_main() tests. A future refactor may move it to a
    dedicated TestMain class.

    When a Click plugin raises Exit(None), main() receives a None exit code.
    Passing None to sys.exit() is equivalent to exit 0, masking the error.
    main() must coerce None to a non-zero code.
    """
    from unittest.mock import patch

    import click

    from fieldkit.__main__ import main

    with patch("fieldkit.__main__.cli") as mock_cli:
        mock_cli.main.side_effect = click.exceptions.Exit(None)
        result = main()

    # None exit code must NOT be returned as-is (which sys.exit(None) treats as 0)
    assert result is not None, (
        "main() returned None for click.exceptions.Exit(None). "
        "This would be treated as exit 0 by sys.exit(), silently masking the error."
    )
    assert isinstance(result, int), f"main() must return an int, got {type(result)}"
    assert result != 0, (
        f"main() returned {result!r} for click.exceptions.Exit(None). "
        "A None click exit should be treated as a non-zero failure."
    )


# ── TestHandleCliException (flattened) ──────────────────────────────────────


@pytest.mark.parametrize(
    "exc,expected_code",
    [
        (ConfigError("missing config"), EXIT_DATA),
        (FrontmatterStalenessError("file changed"), EXIT_PARTIAL),
        # AuthError subclasses — all route to EXIT_AUTH via isinstance(AuthError)
        (AuthError("auth failure"), EXIT_AUTH),
        (SFAuthError("sf session expired"), EXIT_AUTH),
        (GmailAuthError("gmail 403 access denied"), EXIT_AUTH),
        (ShadowbotAuthError("shadowbot token expired"), EXIT_AUTH),
        (PursuitStaleError("pursuit stale"), EXIT_PARTIAL),
        (LLMError("llm auth", category="auth"), EXIT_AUTH),
        (LLMError("rate limited", category="rate-limit"), EXIT_PARTIAL),
        (LLMError("model error", category="general"), EXIT_DATA),
        (ValueError("plain value error"), EXIT_DATA),
        (RuntimeError("unexpected crash"), EXIT_DATA),
        (FieldkitError("base abort message"), EXIT_DATA),
    ],
)
def test_handle_cli_exception_routing_table(exc: Exception, expected_code: int) -> None:
    """Parametrized routing table — every handle_cli_exception() branch.

    Adding a new exception type to the taxonomy requires a row here.
    A value mismatch is a routing bug; a missing row is a coverage gap.
    """
    code = handle_cli_exception(exc)
    assert code == expected_code, (
        f"handle_cli_exception({type(exc).__name__}) returned {code}, expected {expected_code}"
    )


def test_handle_cli_exception_returns_int_not_raises() -> None:
    """handle_cli_exception() returns an int and does not raise or call sys.exit()."""
    exc = RuntimeError("test")
    result = handle_cli_exception(exc)
    assert isinstance(result, int), f"Expected int, got {type(result)}"


def test_handle_cli_exception_frontmatter_staleness_before_broad_fallthrough() -> None:
    """FrontmatterStalenessError (FieldkitError subclass) must route to EXIT_PARTIAL, not EXIT_DATA.

    The isinstance check must appear before the broad FieldkitError fallthrough so
    staleness maps to EXIT_PARTIAL (1), not the generic EXIT_DATA (3).
    """
    exc = FrontmatterStalenessError("stale")
    result = handle_cli_exception(exc)
    assert result == EXIT_PARTIAL


def test_handle_cli_exception_preserves_expired_gmail_sync_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = GmailSyncRestartRequiredError(
        "Gmail changed too far back to finish the refresh; retry to restart the full scan."
    )

    exit_code = handle_cli_exception(error)

    captured = capsys.readouterr()
    assert exit_code == EXIT_PARTIAL
    assert captured.out == ""
    assert captured.err == "Gmail changed too far back to finish the refresh; retry to restart the full scan.\n"


def test_handle_cli_exception_sfautherror_routes_via_autherror_base() -> None:
    """SFAuthError inherits from AuthError — routed correctly without a dedicated clause."""
    result = handle_cli_exception(SFAuthError("expired"))
    assert result == EXIT_AUTH


def test_handle_cli_exception_shadowbot_auth_error_routes_via_autherror_base() -> None:
    """ShadowbotAuthError inherits from AuthError — routed correctly without a dedicated clause."""
    result = handle_cli_exception(ShadowbotAuthError("token gone"))
    assert result == EXIT_AUTH


def test_handle_cli_exception_llm_auth_returns_exit_auth() -> None:
    """LLMError(category='auth') → EXIT_AUTH."""
    result = handle_cli_exception(LLMError("auth", category="auth"))
    assert result == EXIT_AUTH


def test_handle_cli_exception_llm_rate_limit_returns_exit_partial() -> None:
    """LLMError(category='rate-limit') → EXIT_PARTIAL."""
    result = handle_cli_exception(LLMError("rate", category="rate-limit"))
    assert result == EXIT_PARTIAL


def test_handle_cli_exception_llm_general_returns_exit_data() -> None:
    """LLMError(category='general') → EXIT_DATA (covers the else branch)."""
    result = handle_cli_exception(LLMError("general", category="general"))
    assert result == EXIT_DATA


def test_handle_cli_exception_plain_value_error_returns_exit_data() -> None:
    """Plain ValueError (not FrontmatterStalenessError) → EXIT_DATA via broad fallthrough."""
    result = handle_cli_exception(ValueError("bad input"))
    assert result == EXIT_DATA


def test_handle_cli_exception_fieldkiterror_prints_no_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    """Base FieldkitError → EXIT_DATA with a clean one-liner, no traceback (implementation note)."""
    exc = FieldkitError("Empty SF payload {} would wipe all sf_ fields — aborting write")
    result = handle_cli_exception(exc)
    captured = capsys.readouterr()

    assert result == EXIT_DATA
    assert "Traceback" not in captured.err
    assert "Empty SF payload {} would wipe all sf_ fields — aborting write" in captured.err


def test_handle_cli_exception_frontmatter_staleness_still_wins_over_fieldkiterror_clause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FrontmatterStalenessError keeps its EXIT_PARTIAL route — the new FieldkitError
    clause must not shadow it (clause-order regression guard)."""
    exc = FrontmatterStalenessError("stale")
    result = handle_cli_exception(exc)
    captured = capsys.readouterr()

    assert result == EXIT_PARTIAL
    assert "Traceback" not in captured.err
