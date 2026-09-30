"""Canonical Salesforce errors and configuration-independent reauthentication."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fieldkit.config.retry import RETRY_MAX_ATTEMPTS
from fieldkit.sf.errors import reauth_hint_message

pytestmark = pytest.mark.unit
COLD_IMPORT_TIMEOUT_SECONDS = 15


_COLD_IMPORT_PROBE = """
import importlib.abc
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
target = sys.argv[2]
negative_control = sys.argv[3] == "caught-import"
assert not any(name == "fieldkit" or name.startswith("fieldkit.") for name in sys.modules)
sys.path.insert(0, str(source))
attempts = []
application_imports = []
sdk_roots = {"httpx", "tenacity", "requests", "simple_salesforce", "salesforce", "salesforce_bulk"}

def forbidden(name):
    return (
        (name.startswith("fieldkit.sf.") and name != "fieldkit.sf.errors")
        or name.split(".")[0] in sdk_roots
        or (target == "sf.errors" and (name == "fieldkit.config" or name.startswith("fieldkit.config.")))
    )

class RejectOptionalImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("fieldkit."):
            application_imports.append(fullname)
        if forbidden(fullname):
            attempts.append(fullname)
            raise ModuleNotFoundError("forbidden cold import: " + fullname, name=fullname)
        return None

sys.meta_path.insert(0, RejectOptionalImports())
if negative_control:
    try:
        __import__("httpx")
    except ModuleNotFoundError:
        pass

if target == "cli_exit":
    import fieldkit.cli_exit as boundary
    assert application_imports[0] == "fieldkit.cli_exit", repr(application_imports)
else:
    import fieldkit.sf.errors as errors

import fieldkit
import fieldkit.sf
import fieldkit.sf.errors as errors
from fieldkit.errors import AuthError, FieldkitError
assert Path(fieldkit.__file__).resolve() == source / "fieldkit/__init__.py"
assert Path(fieldkit.sf.__file__).resolve() == source / "fieldkit/sf/__init__.py"
assert Path(errors.__file__).resolve() == source / "fieldkit/sf/errors.py"
assert issubclass(errors.SFAuthError, AuthError)
for name in (
    "SFNotFoundError", "SFDataAccessError", "SFAPIError",
    "SFConditionalWriteConflict", "SFConditionalWriteOutcomeUnknown",
):
    error_type = getattr(errors, name)
    assert error_type.__bases__ == (FieldkitError,)
    assert error_type.__module__ == "fieldkit.sf.errors"
assert errors.SFAuthError.__bases__ == (AuthError,)
assert "fieldkit auth sf" in errors.reauth_hint_message()
if target == "cli_exit":
    assert Path(boundary.__file__).resolve() == source / "fieldkit/cli_exit.py"
    assert boundary.sf_errors is errors
    assert "fieldkit.config" in sys.modules
assert not attempts, "forbidden import attempts: " + repr(attempts)
assert not any(forbidden(name) for name in sys.modules), "forbidden module was loaded"
"""


def _cold_import_probe(target: str, *, negative_control: bool = False) -> subprocess.CompletedProcess[str]:
    """Launch a source-pinned isolated interpreter with the blocker installed first."""
    source = Path(__file__).resolve().parents[1] / "src"
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _COLD_IMPORT_PROBE,
            str(source),
            target,
            "caught-import" if negative_control else "normal",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=COLD_IMPORT_TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize("target", ("sf.errors", "cli_exit"))
def test_sf_errors_import_without_transport_or_client(target: str) -> None:
    """Both fresh imports use this checkout and attempt no forbidden dependency."""
    result = _cold_import_probe(target)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("target", ("sf.errors", "cli_exit"))
def test_cold_import_guard_detects_a_caught_forbidden_import(target: str) -> None:
    """A swallowed import failure still fails the guard through its attempt ledger."""
    result = _cold_import_probe(target, negative_control=True)

    assert result.returncode != 0
    assert "forbidden import attempts: ['httpx']" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "name",
    [
        "SFAuthError",
        "SFNotFoundError",
        "SFDataAccessError",
        "SFAPIError",
        "SFConditionalWriteConflict",
        "SFConditionalWriteOutcomeUnknown",
        "reauth_hint_message",
        "SFDirectClient",
    ],
)
def test_sf_errors_have_no_old_client_or_package_aliases(name: str) -> None:
    import fieldkit.sf
    from fieldkit.sf import client

    assert hasattr(client, name) is (name == "SFDirectClient")
    assert not hasattr(fieldkit.sf, name)


@pytest.mark.parametrize(
    "name",
    [
        "_is_sf_transient",
        "_raw_sf_request",
        "_sf_request",
        "_sf_request_idempotent",
        "_sf_request_unsafe",
        "_parse_json_response",
        "_safe_error_detail",
        "_ui_api_record_collection",
        "_is_value_object",
        "_IDEMPOTENT_METHODS",
        "_SPLIT_FIELD_NAMES",
    ],
)
def test_sf_private_helpers_have_no_old_client_aliases(name: str) -> None:
    from fieldkit.sf import client

    assert not hasattr(client, name)


@pytest.mark.parametrize(("status", "expected_exit"), [(401, 2), (400, 1)])
def test_sf_schema_diagnostics_do_not_reflect_private_inputs(
    status: int, expected_exit: int, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    from fieldkit.__main__ import main

    response = httpx.Response(
        status,
        json=[{"message": "private-message-sentinel", "errorCode": "private-code-sentinel"}],
        headers={"content-type": "private-header-sentinel"},
    )
    transport = MagicMock()
    transport.request.return_value = response
    with (
        patch("httpx.Client", return_value=transport),
        patch("fieldkit.commands.sf.schema.get_sf_session_id", return_value="private-sid-sentinel"),
        patch(
            "fieldkit.commands.sf.schema.get_sf_rest_base_url", return_value="https://private-host-sentinel.example.com"
        ),
    ):
        result = main(["sf", "schema", "Account", "--record-id", "001000000000001"])
    assert result == expected_exit
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err + caplog.text
    assert "Traceback" not in captured.err
    assert ("fieldkit auth sf" if status == 401 else str(status)) in captured.err
    assert transport.request.call_count == 1


def test_reauth_hint_message_with_configured_url() -> None:
    """Configured hostnames must not appear in authentication guidance."""
    with patch("fieldkit.config.get_salesforce_org_url", return_value="https://myorg.my.salesforce.com") as get_org_url:
        result = reauth_hint_message()
    assert result == "run 'fieldkit auth sf' — copy the 'sid' cookie from your Salesforce org's browser DevTools"
    get_org_url.assert_not_called()


def test_reauth_hint_message_unconfigured_returns_generic() -> None:
    """When SF org URL is empty, the hint uses generic phrasing."""
    with patch("fieldkit.config.get_salesforce_org_url", return_value=""):
        result = reauth_hint_message()
    assert "your Salesforce org" in result
    assert "auth sf" in result


def test_reauth_hint_message_does_not_raise_when_unconfigured() -> None:
    """reauth_hint_message() must not raise under any config state."""
    with patch("fieldkit.config.get_salesforce_org_url", return_value=""):
        result = reauth_hint_message()
    assert isinstance(result, str)


def test_reauth_hint_message_does_not_read_configuration() -> None:
    """Reauthentication guidance remains usable when configuration is invalid."""
    with patch(
        "fieldkit.config.get_salesforce_org_url", side_effect=ValueError("private-config-sentinel")
    ) as get_org_url:
        result = reauth_hint_message()
    assert result == "run 'fieldkit auth sf' — copy the 'sid' cookie from your Salesforce org's browser DevTools"
    get_org_url.assert_not_called()


@pytest.mark.parametrize(
    ("status", "failure", "expected_exit", "attempts", "programming_bug"),
    [
        (401, None, 2, 1, False),
        (400, None, 1, 1, False),
        (500, None, 1, RETRY_MAX_ATTEMPTS, False),
        (200, httpx.ConnectError, 1, RETRY_MAX_ATTEMPTS, False),
        (200, httpx.ReadTimeout, 1, RETRY_MAX_ATTEMPTS, False),
        (200, httpx.RequestError, 1, 1, False),
        (200, ValueError, 1, 1, False),
        (200, RuntimeError, 3, 1, True),
    ],
    ids=["auth", "rejected", "http-exhausted", "connect", "read-timeout", "request", "malformed-json", "decoder-bug"],
)
def test_sf_schema_cli_preserves_private_failure_and_bug_boundaries(
    status: int,
    failure: type[Exception] | None,
    expected_exit: int,
    attempts: int,
    programming_bug: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.sf import _transport

    with_policy = vars(_transport._sf_request_idempotent)["with_policy"]
    assert callable(with_policy)
    request = with_policy(wait_min=0, wait_max=0)
    assert callable(request)
    monkeypatch.setattr(_transport, "_sf_request_idempotent", request)
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.headers = {"content-type": "private-header-sentinel"}
    response.text = "private-body-sentinel"
    response.json.return_value = {"private-field-sentinel": "private-value-sentinel"}
    transport = MagicMock(spec=httpx.Client)
    transport.request.return_value = response
    if failure is not None:
        error = failure("decoder-programming-bug" if programming_bug else "private-exception-sentinel")
        error.__cause__ = ValueError("decoder-root-bug" if programming_bug else "private-chain-sentinel")
        if issubclass(failure, httpx.RequestError):
            transport.request.side_effect = error
        else:
            response.json.side_effect = error
    with (
        patch("httpx.Client", return_value=transport),
        patch("fieldkit.commands.sf.schema.get_sf_session_id", return_value="private-credential-sentinel"),
        patch("fieldkit.commands.sf.schema.get_sf_rest_base_url", return_value="https://private-host.example.com"),
    ):
        result = main(["sf", "schema", "Account", "--record-id", "001000000000001"])
    assert result == expected_exit
    output = capsys.readouterr()
    if programming_bug:
        assert "RuntimeError: decoder-programming-bug" in output.err
        assert "Traceback" in output.err
    else:
        assert "private-" not in output.out + output.err + caplog.text
        assert "Traceback" not in output.err
    assert transport.request.call_count == attempts
