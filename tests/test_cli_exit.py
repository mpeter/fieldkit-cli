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
  6. Unsupported explicit exit → data error (exit 3)
  7. KeyboardInterrupt → propagates unhandled (not caught by Exception handler)

historic regression: cli_main() had zero tests; a regression here would break all commands silently.
"""

import re
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import click
import pytest

import fieldkit.cli_exit as cli_exit_module
import fieldkit.sf.errors as sf_errors
from fieldkit.__main__ import cli, main
from fieldkit.cli_exit import (
    EXIT_AUTH,
    EXIT_DATA,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    cli_main,
    handle_cli_exception,
    normalize_exit_status,
)
from fieldkit.config import ConfigError
from fieldkit.errors import (
    AuthError,
    FieldkitError,
    FrontmatterStalenessError,
    GitHubCreationUncertainError,
    GitHubDataError,
    GitHubRequestError,
    GmailAuthError,
    GmailSyncRestartRequiredError,
    GoogleCredentialRefreshRetryableError,
    LLMError,
    LLMErrorCategory,
    MissingOptionalDependencyError,
    PursuitStaleError,
    SQLiteSnapshotError,
    SQLiteSnapshotReason,
)
from fieldkit.ingest.docs import DocNotFoundError
from fieldkit.llm._transcribe import TranscribeError
from fieldkit.sf.errors import (
    SFAPIError,
    SFAuthError,
    SFConditionalWriteConflict,
    SFConditionalWriteOutcomeUnknown,
    SFDataAccessError,
    SFNotFoundError,
)
from fieldkit.shadowbot.auth import ShadowbotAuthError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_empty_output_diagnostic_ignores_payload_and_cause(
    cleanup_failed: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.errors import EmptyOutputError

    exc = EmptyOutputError(cleanup_failed=cleanup_failed)
    exc.args = ("hostile-account@example.com /private/report",)
    exc.__cause__ = OSError("private chained cause")
    result = handle_cli_exception(exc)
    assert result == EXIT_DATA
    output = capsys.readouterr().err
    assert output == (
        "[cli_exit] Empty output detected; cleanup failed — inspect the output before retrying.\n"
        if cleanup_failed
        else "[cli_exit] Empty output detected — investigate before retrying.\n"
    )


def test_similar_runtime_error_is_not_empty_output(capsys: pytest.CaptureFixture[str]) -> None:
    result = handle_cli_exception(RuntimeError("Empty output detected"))
    assert result == EXIT_DATA
    assert "Unhandled exception" in capsys.readouterr().err


@pytest.mark.parametrize("pipeline_only", [False, True])
@pytest.mark.parametrize("typed", [False, True])
def test_brief_cli_uses_canonical_empty_output_boundary(pipeline_only: bool, typed: bool) -> None:
    from click.testing import CliRunner

    from fieldkit.commands.brief.cli import cli as brief_cli
    from fieldkit.errors import EmptyOutputError

    error = EmptyOutputError() if typed else RuntimeError("File written as 0 bytes: fictional payload")
    target = "_run_pipeline_only" if pipeline_only else "_run_generate"
    with (
        patch(f"fieldkit.commands.brief.cli.{target}", side_effect=error),
        patch("fieldkit.watch.integration_plan.build_integration_plan"),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
    ):
        result = CliRunner().invoke(brief_cli, ["generate", *(["--pipeline-only"] if pipeline_only else [])])
    assert result.exit_code == EXIT_DATA
    if typed:
        assert result.output == "[cli_exit] Empty output detected — investigate before retrying.\n"
    else:
        assert "Unhandled exception" in result.output
        assert "Fatal:" not in result.output


@pytest.mark.parametrize("stage", ["nested", "root-option", "cleanup"])
@pytest.mark.parametrize("payload", ["none", "false", "true", "string", "subclass", "opaque"])
def test_real_click_dispatcher_rejects_invalid_explicit_exit(
    stage: str, payload: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Status(int):
        def __int__(self) -> int:
            raise AssertionError("conversion hook called")

        def __str__(self) -> str:
            raise AssertionError("render hook called")

        def __repr__(self) -> str:
            raise AssertionError("render hook called")

    class Opaque:
        def __str__(self) -> str:
            raise AssertionError("render hook called")

        def __repr__(self) -> str:
            raise AssertionError("render hook called")

    code = {
        "none": None,
        "false": False,
        "true": True,
        "string": "private-marker",
        "subclass": Status(0),
        "opaque": Opaque(),
    }[payload]
    cleaned: list[str] = []

    def exit_now() -> None:
        raise click.exceptions.Exit(code)

    @click.group()
    def group() -> None:
        pass

    @group.command()
    @click.pass_context
    def leaf(ctx: click.Context) -> None:
        ctx.call_on_close(lambda: cleaned.append("leaf"))
        if stage == "cleanup":
            ctx.find_root().call_on_close(exit_now)
        else:
            exit_now()

    def option_callback(ctx: click.Context, param: click.Parameter, value: bool) -> None:
        del param, value
        ctx.call_on_close(lambda: cleaned.append("root"))
        exit_now()

    monkeypatch.setattr(cli, "get_command", lambda ctx, name: group)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    args = ["synthetic", "leaf"]
    if stage == "root-option":
        monkeypatch.setattr(
            cli, "params", [*cli.params, click.Option(["--synthetic-exit"], is_flag=True, callback=option_callback)]
        )
        args = ["--synthetic-exit"]
    result = main(args)
    assert result == EXIT_DATA
    # Click's parsing scope uses cleanup=False; invocation scopes run cleanup.
    assert cleaned == ([] if stage == "root-option" else ["leaf"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[cli_exit] Invalid exit status — investigation required.\n"


@pytest.mark.parametrize(
    ("behavior", "expected"),
    [
        ("exit-0", 0),
        ("exit-1", 1),
        ("exit-2", 2),
        ("exit-3", 3),
        ("return-false", 0),
        ("return-true", 0),
        ("return-subclass", 0),
        ("return-130", 3),
        ("interrupt", 130),
        ("eof", 1),
        ("auth", 2),
        ("cleanup-failure", 3),
        ("suppressed-exit", 0),
    ],
)
def test_real_click_dispatcher_preserves_results_and_cleanup(
    behavior: str, expected: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cleaned: list[str] = []

    class Status(int):
        def __int__(self) -> int:
            raise AssertionError("conversion hook called")

    def cleanup_failure() -> None:
        raise RuntimeError("synthetic cleanup failure")

    @click.command()
    @click.pass_context
    def leaf(ctx: click.Context) -> object:
        ctx.call_on_close(lambda: cleaned.append("leaf"))
        if behavior.startswith("exit-"):
            raise click.exceptions.Exit(int(behavior[-1]))
        if behavior == "return-false":
            return False
        if behavior == "return-true":
            return True
        if behavior == "return-subclass":
            return Status(0)
        if behavior == "return-130":
            return 130
        if behavior == "interrupt":
            raise KeyboardInterrupt
        if behavior == "eof":
            raise EOFError
        if behavior == "auth":
            raise AuthError("synthetic authentication failure")
        if behavior == "cleanup-failure":
            ctx.find_root().call_on_close(cleanup_failure)
        if behavior == "suppressed-exit":
            ctx.find_root().with_resource(suppress(click.exceptions.Exit))
            error = click.exceptions.Exit()
            monkeypatch.setattr(error, "exit_code", None)
            raise error
        return None

    monkeypatch.setattr(cli, "get_command", lambda ctx, name: leaf)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    result = main(["synthetic"])
    assert result == expected
    assert cleaned == ["leaf"]
    captured = capsys.readouterr()
    if behavior == "cleanup-failure":
        assert "RuntimeError: synthetic cleanup failure" in captured.err
    elif behavior == "suppressed-exit":
        assert captured.err == "[cli_exit] Invalid exit status — investigation required.\n"
    elif behavior.startswith("exit-") or (behavior.startswith("return-") and behavior != "return-130"):
        assert captured.err == ""


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_real_click_eager_options_remain_success(option: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    result = main([option])
    assert result == EXIT_SUCCESS


def test_documentation_error_transcript(capsys: pytest.CaptureFixture[str]) -> None:
    document = Path("docs/reference/exit-codes.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```\n(.*?)```", document, flags=re.DOTALL)
    assert len(blocks) == 2

    result = handle_cli_exception(ConfigError("Config file not found: ~/.config/fieldkit/config.yaml"))
    assert result == EXIT_DATA
    config_output = capsys.readouterr()
    assert config_output.out == ""
    result = handle_cli_exception(LLMError("private-provider-payload", category="auth"))
    assert result == EXIT_AUTH
    result = handle_cli_exception(LLMError("private-provider-payload", category="rate-limit"))
    assert result == EXIT_PARTIAL
    result = handle_cli_exception(LLMError("private-provider-payload", category="general"))
    assert result == EXIT_DATA
    model_output = capsys.readouterr()
    assert model_output.out == ""
    try:
        raise RuntimeError("fictional failure")
    except RuntimeError as error:
        result = handle_cli_exception(error)
    assert result == EXIT_DATA
    unhandled_output = capsys.readouterr()
    assert unhandled_output.out == ""
    trace_prefix = "\n".join(unhandled_output.err.splitlines()[:2])
    assert blocks[1] == config_output.err + model_output.err + trace_prefix + "\n  ...\n"
    assert blocks[0] == "[cli_exit] <category> — <action required>: <detail>\n"


@pytest.mark.parametrize("category,expected", [("auth", 2), ("rate-limit", 1), ("general", 3)])
def test_llm_error_diagnostics_exclude_provider_payload(
    category: LLMErrorCategory, expected: int, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.cli_exit import handle_cli_exception

    error = LLMError("provider-payload-sentinel", category=category)
    error.__cause__ = RuntimeError("provider-cause-sentinel")
    result = handle_cli_exception(error)
    assert result == expected
    output = capsys.readouterr()
    assert "provider-payload-sentinel" not in output.out + output.err
    assert "provider-cause-sentinel" not in output.out + output.err
    assert output.err


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("active", EXIT_PARTIAL), ("journal", EXIT_DATA), ("unverified", EXIT_DATA)],
)
def test_sqlite_snapshot_diagnostics_are_bounded(
    reason: SQLiteSnapshotReason, expected: int, capsys: pytest.CaptureFixture[str]
) -> None:
    private_detail = "/fictional-private/operator/private-customer/cache.db"
    error = SQLiteSnapshotError(private_detail, reason=reason)

    assert handle_cli_exception(error) == expected

    output = capsys.readouterr()
    assert private_detail not in output.err
    assert "Traceback" not in output.err


# ── TestCliMain (flattened) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc,expected_code",
    [
        (ConfigError("x"), EXIT_DATA),
        (FrontmatterStalenessError("x"), EXIT_PARTIAL),
        (GoogleCredentialRefreshRetryableError("x"), EXIT_PARTIAL),
        (GitHubRequestError("x"), EXIT_PARTIAL),
        (GitHubDataError("x"), EXIT_DATA),
        (GitHubCreationUncertainError("x"), EXIT_DATA),
        (SQLiteSnapshotError("active private path", reason="active"), EXIT_PARTIAL),
        (SQLiteSnapshotError("journal private path", reason="journal"), EXIT_DATA),
        (SQLiteSnapshotError("unverified private path", reason="unverified"), EXIT_DATA),
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


@pytest.mark.parametrize("code", [42, -1, 130, "private-exit-marker", False, True])
def test_cli_main_unsupported_explicit_exit_is_data_error(code: int | str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise SystemExit(code)
    assert exc_info.value.code == EXIT_DATA
    assert "private-exit-marker" not in capsys.readouterr().err


@pytest.mark.parametrize("code", [0, 1, 2, 3, None])
def test_cli_main_supported_explicit_exit(code: int | None) -> None:
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise SystemExit(code)
    assert exc_info.value.code == (0 if code is None else code)


@pytest.mark.parametrize(
    "boundary", ["normalizer", "cli-system", "cli-click", "dispatcher-system", "dispatcher-click", "callback"]
)
def test_integer_subclass_exit_is_rejected_without_payload_hooks(
    boundary: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    class Status(int):
        def __int__(self) -> int:
            calls.append("int")
            return 130

        def __le__(self, other: object) -> bool:
            calls.append("le")
            return True

        def __ge__(self, other: object) -> bool:
            calls.append("ge")
            return True

        def __str__(self) -> str:
            calls.append("str")
            return "private-status-marker"

        def __repr__(self) -> str:
            calls.append("repr")
            return "private-status-marker"

    code = Status(0)
    if boundary == "normalizer":
        result = normalize_exit_status(code)
    elif boundary == "callback":
        with patch("fieldkit.__main__.cli.main", return_value=code), patch("fieldkit.__main__.load_dotenv_safe"):
            result = main([])
    else:
        error = click.exceptions.Exit(code) if boundary.endswith("click") else SystemExit(code)
        if boundary.startswith("cli-"):
            with pytest.raises(SystemExit) as exc_info, cli_main():
                raise error
            result = exc_info.value.code
        else:
            with patch("fieldkit.__main__.cli.main", side_effect=error), patch("fieldkit.__main__.load_dotenv_safe"):
                result = main([])
    assert type(result) is int
    assert result == (EXIT_SUCCESS if boundary == "callback" else EXIT_DATA)
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "" if boundary == "callback" else "[cli_exit] Invalid exit status — investigation required.\n"
    )


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_dispatcher_preserves_canonical_callback_status(code: int, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("fieldkit.__main__.cli.main", return_value=code), patch("fieldkit.__main__.load_dotenv_safe"):
        result = main([])
    assert result == code
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("code", [-1, 42, 130])
def test_dispatcher_normalizes_invalid_callback_status(code: int, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("fieldkit.__main__.cli.main", return_value=code), patch("fieldkit.__main__.load_dotenv_safe"):
        result = main([])
    assert result == EXIT_DATA
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[cli_exit] Invalid exit status — investigation required.\n"


@pytest.mark.parametrize("callback_result", [None, False, True, "private-callback-marker", [42], {"status": 42}])
def test_dispatcher_ignores_noninteger_callback_results(
    callback_result: object, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("fieldkit.__main__.cli.main", return_value=callback_result), patch("fieldkit.__main__.load_dotenv_safe"):
        result = main([])
    assert result == EXIT_SUCCESS
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("code", [0, 1, 2, 3, 42, None])
def test_cli_main_click_explicit_exit(monkeypatch: pytest.MonkeyPatch, code: int | None) -> None:
    error = click.exceptions.Exit()
    monkeypatch.setattr(error, "exit_code", code)
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise error
    assert exc_info.value.code == (code if code in (0, 1, 2, 3) else EXIT_DATA)


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
    """Known Salesforce failures get category guidance without payload or traceback."""
    with pytest.raises(SystemExit) as exc_info, cli_main():
        raise SFAPIError("HTTP 500 from Salesforce")
    assert exc_info.value.code == EXIT_DATA
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "HTTP 500 from Salesforce" not in stderr


@pytest.mark.parametrize(
    ("failure", "code", "guidance"),
    [
        (SFAuthError, EXIT_AUTH, "fieldkit auth sf"),
        (SFAPIError, EXIT_DATA, "availability"),
        (SFNotFoundError, EXIT_DATA, "record"),
        (SFDataAccessError, EXIT_DATA, "permissions"),
        (SFConditionalWriteConflict, EXIT_DATA, "reread"),
        (SFConditionalWriteOutcomeUnknown, EXIT_DATA, "may have written"),
    ],
)
def test_known_sf_errors_have_fixed_cli_guidance(
    failure: type[Exception], code: int, guidance: str, capsys: pytest.CaptureFixture[str]
) -> None:
    error = failure("private-error-sentinel")
    error.__cause__ = ValueError("private-cause-sentinel")
    result = handle_cli_exception(error)
    assert result == code
    captured = capsys.readouterr()
    assert captured.out == ""
    stderr = captured.err
    assert guidance in stderr
    assert "Traceback" not in stderr
    assert "private-" not in stderr


def test_cli_exit_uses_the_canonical_salesforce_error_module() -> None:
    """Typed dispatch refers to the leaf's original classes, never aliases."""
    assert vars(cli_exit_module)["sf_errors"] is sf_errors


@pytest.mark.parametrize(
    "name",
    (
        "SFAuthError",
        "SFAPIError",
        "SFNotFoundError",
        "SFDataAccessError",
        "SFConditionalWriteConflict",
        "SFConditionalWriteOutcomeUnknown",
    ),
)
@pytest.mark.parametrize("spoof", ("name", "module", "attributes"))
def test_unrelated_sf_named_exceptions_keep_generic_handling(
    name: str, spoof: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Matching names, claimed module identities, and duck attributes grant no category."""
    attributes: dict[str, object] = {"__module__": "fieldkit.sf.errors" if spoof == "module" else __name__}
    if spoof == "attributes":
        attributes.update(category="auth", status_code=401, reauth_hint="fieldkit auth sf")
    unrelated = type(name, (Exception,), attributes)

    result = handle_cli_exception(unrelated("generic-payload-marker"))

    assert result == EXIT_DATA
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Unhandled exception" in captured.err
    assert "generic-payload-marker" in captured.err
    assert "[cli_exit] Salesforce" not in captured.err
    assert "run 'fieldkit auth sf'" not in captured.err


@pytest.mark.parametrize(
    "failure,code,guidance",
    (
        (SFAuthError, EXIT_AUTH, "fieldkit auth sf"),
        (SFAPIError, EXIT_DATA, "availability"),
        (SFNotFoundError, EXIT_DATA, "record"),
        (SFDataAccessError, EXIT_DATA, "permissions"),
        (SFConditionalWriteConflict, EXIT_DATA, "reread"),
        (SFConditionalWriteOutcomeUnknown, EXIT_DATA, "may have written"),
    ),
)
def test_canonical_sf_subclasses_retain_category_and_redact_private_payloads(
    failure: type[Exception], code: int, guidance: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real inheritance preserves each typed route and fixed diagnostic privacy."""
    subtype = type("CanonicalSFSubtype", (failure,), {})
    error = subtype("private-subclass-payload-sentinel")
    error.__cause__ = ValueError("private-subclass-cause-sentinel")

    result = handle_cli_exception(error)

    assert result == code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert guidance in captured.err
    assert "private-" not in captured.err
    assert "Traceback" not in captured.err


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
    assert fieldkit.errors.LLMError is vars(fieldkit.llm.core)["LLMError"], (
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


def test_cli_main_click_exit_none_is_treated_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
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

    from fieldkit.__main__ import main

    with patch("fieldkit.__main__.cli") as mock_cli:
        malformed_exit = click.exceptions.Exit()
        monkeypatch.setattr(malformed_exit, "exit_code", None)
        mock_cli.main.side_effect = malformed_exit
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
        (GitHubRequestError("request failed"), EXIT_PARTIAL),
        (GitHubDataError("invalid provider data"), EXIT_DATA),
        (GitHubCreationUncertainError("creation uncertain"), EXIT_DATA),
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


def test_routing_read_retryable_error_has_clean_partial_diagnostic(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.errors import RoutingReadRetryableError

    result = handle_cli_exception(RoutingReadRetryableError("Cannot scan ingest pursuit directory"))
    assert result == EXIT_PARTIAL
    output = capsys.readouterr()
    assert "Cannot scan ingest pursuit directory" in output.err
    assert "Traceback" not in output.err


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
