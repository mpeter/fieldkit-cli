"""Bind the public exit-code tables to the canonical process boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import click
import pytest

from fieldkit.__main__ import main
from fieldkit.cli_exit import (
    EXIT_AUTH,
    EXIT_DATA,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    handle_cli_exception,
    normalize_explicit_exit,
)
from fieldkit.config import ConfigError
from fieldkit.errors import (
    AuthError,
    EmptyOutputError,
    FieldkitError,
    FrontmatterStalenessError,
    GitHubCreationUncertainError,
    GitHubDataError,
    GitHubRequestError,
    GmailSyncPartialError,
    GmailSyncRestartRequiredError,
    GoogleCredentialRefreshRetryableError,
    LLMError,
    MissingOptionalDependencyError,
    PursuitStaleError,
    RoutingReadRetryableError,
    SalesforceSyncPartialError,
    SQLiteSnapshotError,
)
from fieldkit.sf import errors as sf_errors
from scripts.markdown_tables import parse_markdown_tables

pytestmark = pytest.mark.unit

_REFERENCE = Path("docs/reference/exit-codes.md")


def _table(section: str) -> list[list[str]]:
    content = _REFERENCE.read_text(encoding="utf-8")
    body = content.split(f"## {section}\n", 1)[1].split("\n## ", 1)[0]
    tables = parse_markdown_tables(body)
    assert len(tables) == 1, f"Expected one exit-code table in {section}"
    return [[cell.replace("`", "").replace("**", "") for cell in row] for row in tables[0].rows]


def test_code_table_uses_exact_canonical_constants() -> None:
    """Derive the documented integer contract from the runtime constants."""
    rows = _table("Code Table")
    observed = {row[1]: int(row[0]) for row in rows}
    assert observed == {
        "Success": EXIT_SUCCESS,
        "Partial, retryable, or policy failure": EXIT_PARTIAL,
        "Auth failure": EXIT_AUTH,
        "Data error": EXIT_DATA,
    }


def _handler_examples() -> dict[str, BaseException]:
    return {
        "ConfigError": ConfigError("invalid configuration"),
        "EmptyOutputError": EmptyOutputError(),
        "MissingOptionalDependencyError": MissingOptionalDependencyError("meeting", "google", ("google.auth",)),
        'SQLiteSnapshotError(reason="active")': SQLiteSnapshotError("private", reason="active"),
        'SQLiteSnapshotError(reason="journal" or "unverified")': SQLiteSnapshotError("private", reason="journal"),
        "GitHubRequestError": GitHubRequestError("request failed"),
        "GitHubDataError": GitHubDataError("invalid data"),
        "GitHubCreationUncertainError": GitHubCreationUncertainError("uncertain create"),
        "FrontmatterStalenessError": FrontmatterStalenessError("stale"),
        "GmailSyncPartialError": GmailSyncPartialError("partial sync"),
        "GmailSyncRestartRequiredError": GmailSyncRestartRequiredError("restart required"),
        "GoogleCredentialRefreshRetryableError": GoogleCredentialRefreshRetryableError("retry refresh"),
        "SalesforceSyncPartialError": SalesforceSyncPartialError("partial sync"),
        "RoutingReadRetryableError": RoutingReadRetryableError("retry routing read"),
        "SFAuthError": sf_errors.SFAuthError("private-diagnostic-sentinel"),
        "SFConditionalWriteConflict": sf_errors.SFConditionalWriteConflict("private-diagnostic-sentinel"),
        "SFConditionalWriteOutcomeUnknown": sf_errors.SFConditionalWriteOutcomeUnknown("private-diagnostic-sentinel"),
        "SFNotFoundError": sf_errors.SFNotFoundError("private-diagnostic-sentinel"),
        "SFDataAccessError": sf_errors.SFDataAccessError("private-diagnostic-sentinel"),
        "SFAPIError": sf_errors.SFAPIError("private-diagnostic-sentinel"),
        "AuthError and subclasses": AuthError("credentials required"),
        "PursuitStaleError": PursuitStaleError("stale pursuit"),
        'LLMError(category="auth")': LLMError("private", category="auth"),
        'LLMError(category="rate-limit")': LLMError("private", category="rate-limit"),
        'LLMError(category="general")': LLMError("private", category="general"),
        "exact FieldkitError": FieldkitError("bounded abort"),
        "Any other Exception": RuntimeError("unexpected"),
    }


def test_exception_table_exhaustively_routes_handler_rows() -> None:
    """Every published handler row has exactly one executable example."""
    rows = _table("Exception-to-Code Mapping")
    handler_rows = {row[0]: row for row in rows if row[1] == "handler"}
    examples = _handler_examples()
    assert set(handler_rows) == set(examples)


@pytest.mark.parametrize(("label", "exception"), _handler_examples().items(), ids=_handler_examples())
def test_documented_handler_row_routes_its_exception(
    label: str, exception: BaseException, capsys: pytest.CaptureFixture[str]
) -> None:
    """Use each published code, preserving fixed Salesforce diagnostics."""
    rows = _table("Exception-to-Code Mapping")
    handler_rows = {row[0]: row for row in rows if row[1] == "handler"}

    assert handle_cli_exception(exception) == int(handler_rows[label][3])
    captured = capsys.readouterr()
    if exception.__class__.__module__ == "fieldkit.sf.errors":
        assert "Salesforce" in captured.err
        assert "private-diagnostic-sentinel" not in captured.out + captured.err
        assert "Traceback" not in captured.err


@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_empty_report_mapping_preserves_fixed_recovery_guidance(
    cleanup_failed: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = {row[0]: row for row in _table("Exception-to-Code Mapping")}
    error = EmptyOutputError(cleanup_failed=cleanup_failed)

    assert handle_cli_exception(error) == int(rows["EmptyOutputError"][3])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Empty output" in captured.err
    assert "Traceback" not in captured.err


def test_exception_table_routes_dispatcher_rows() -> None:
    """Exercise Click usage and explicit-exit normalization at the public dispatcher."""
    rows = _table("Exception-to-Code Mapping")
    dispatcher_rows = {row[0]: row for row in rows if row[1] == "dispatcher"}
    assert set(dispatcher_rows) == {
        "click.ClickException",
        "click.exceptions.Exit or SystemExit",
        "SystemExit(None)",
        "Invalid explicit exit payload",
        "Exact built-in integer callback return",
        "Noninteger callback return",
    }
    assert main(["--unknown-option"]) == int(dispatcher_rows["click.ClickException"][3])
    assert dispatcher_rows["click.exceptions.Exit or SystemExit"][3] == "0 through 3"
    assert dispatcher_rows["click.exceptions.Exit or SystemExit"][2] == (
        "Unsuppressed exit with exact built-in integer status 0 through 3 is preserved"
    )
    assert dispatcher_rows["Exact built-in integer callback return"][3] == "0 through 3"


@pytest.mark.parametrize("exit_kind", ["click", "system"])
@pytest.mark.parametrize("code", [0, 1, 2, 3, None, -1, 42, 130, False, True, "private-exit-marker"])
def test_documented_explicit_exit_rows_at_dispatcher(
    exit_kind: str, code: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = {row[0]: row for row in _table("Exception-to-Code Mapping")}
    if exit_kind == "system":
        error: BaseException = SystemExit(code)
    else:
        error = click.exceptions.Exit()
        monkeypatch.setattr(error, "exit_code", code)
    if type(code) is int and code in (0, 1, 2, 3):
        expected = code
        assert rows["click.exceptions.Exit or SystemExit"][3] == "0 through 3"
    elif exit_kind == "system" and code is None:
        expected = int(rows["SystemExit(None)"][3])
    else:
        expected = int(rows["Invalid explicit exit payload"][3])
    with patch("fieldkit.__main__.cli.main", side_effect=error), patch("fieldkit.__main__.load_dotenv_safe"):
        result = main([])
    assert result == expected
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "[cli_exit] Invalid exit status — investigation required.\n" if expected == 3 and code != 3 else ""
    )


@pytest.mark.parametrize("callback_result", [0, 1, 2, 3, -1, 42, 130, None, False, True, "private-callback-marker"])
def test_documented_callback_return_rows_at_dispatcher(
    callback_result: object, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = {row[0]: row for row in _table("Exception-to-Code Mapping")}
    if type(callback_result) is int:
        expected = callback_result if callback_result in (0, 1, 2, 3) else EXIT_DATA
        assert rows["Exact built-in integer callback return"][3] == "0 through 3"
    else:
        expected = int(rows["Noninteger callback return"][3])
    with patch("fieldkit.__main__.cli.main", return_value=callback_result), patch("fieldkit.__main__.load_dotenv_safe"):
        result = main([])
    assert result == expected
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "[cli_exit] Invalid exit status — investigation required.\n"
        if type(callback_result) is int and callback_result not in (0, 1, 2, 3)
        else ""
    )


def test_troubleshooting_table_has_the_canonical_exit_owner() -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    records = contract["documents"]["docs/reference/troubleshooting.md"]["tables"]
    assert len(records) == 1
    assert records[0]["id"] == "docs.reference.troubleshooting.md.table-1"
    assert records[0]["classification"] == "structural_assertion"
    assert records[0]["verification_id"] == "automated.exit-code-contract"


@pytest.mark.parametrize(
    "code,meaning,next_step,exception",
    [
        (EXIT_SUCCESS, "Selected work succeeded", "No repair needed", None),
        (
            EXIT_PARTIAL,
            "Partial result; retry may help",
            "Inspect the named record or provider failure",
            GmailSyncPartialError("partial sync"),
        ),
        (
            EXIT_AUTH,
            "Authentication needs user action",
            "Reauthenticate through the documented provider flow",
            AuthError("credentials required"),
        ),
        (
            EXIT_DATA,
            "Invalid or incomplete data/configuration",
            "Correct the named input; retrying unchanged input will not help",
            ConfigError("invalid configuration"),
        ),
    ],
)
def test_troubleshooting_table_matches_runtime_and_recovery(
    code: int, meaning: str, next_step: str, exception: BaseException | None
) -> None:
    tables = parse_markdown_tables(Path("docs/reference/troubleshooting.md").read_text(encoding="utf-8"))
    assert len(tables) == 1
    assert tables[0].header == ("Exit", "Meaning", "Next step")
    assert len(tables[0].rows) == 4
    assert tables[0].rows[code] == (f"`{code}`", meaning, next_step)
    result = normalize_explicit_exit(SystemExit(None)) if exception is None else handle_cli_exception(exception)
    assert result == code
