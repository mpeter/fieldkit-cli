"""Tests for fieldkit.ingest.docs — fetch_gemini_doc() retry behaviour (T038).

Verifies that:
- Transient HttpError (503) is retried up to 3 times.
- Permanent HttpError (404) raises DocNotFoundError immediately (no retry).
- Permanent HttpError (403) raises DocAccessDeniedError immediately (no retry).
"""

import ast
import inspect
import socket
from pathlib import Path
from time import monotonic
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

pytestmark = pytest.mark.unit


def test_google_transport_stub_matches_installed_constructor() -> None:
    """Dependency upgrades must not silently invalidate the local constructor stub."""
    from google_auth_httplib2 import AuthorizedHttp

    stub = Path(__file__).parents[1] / "typings/google_auth_httplib2/__init__.pyi"
    tree = ast.parse(stub.read_text(encoding="utf-8"))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert len(classes) == 1
    assert classes[0].name == "AuthorizedHttp"
    constructors = [node for node in classes[0].body if isinstance(node, ast.FunctionDef)]
    assert len(constructors) == 1
    constructor = constructors[0]
    assert constructor.name == "__init__"
    assert not constructor.args.posonlyargs
    assert not constructor.args.kwonlyargs
    assert constructor.args.vararg is None
    assert constructor.args.kwarg is None
    declared = constructor.args.args[1:]
    required_count = len(declared) - len(constructor.args.defaults)
    signature = inspect.signature(AuthorizedHttp)
    assert list(signature.parameters) == [argument.arg for argument in declared]
    for index, parameter in enumerate(signature.parameters.values()):
        assert parameter.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert (parameter.default is inspect.Parameter.empty) == (index < required_count)


@pytest.mark.parametrize("api,version", [("docs", "v1"), ("drive", "v3")])
def test_service_uses_explicit_transport_timeout(api: str, version: str) -> None:
    from fieldkit.ingest import docs

    credentials = MagicMock()
    with (
        patch.object(docs, "_get_creds", return_value=credentials),
        patch("googleapiclient.discovery.build") as build,
    ):
        result = docs.get_docs_service() if api == "docs" else docs.get_drive_service()

    assert result is build.return_value
    assert build.call_args.args == (api, version)
    transport = build.call_args.kwargs["http"]
    assert transport.credentials is credentials
    assert transport.timeout == 30
    assert transport.http.connections == {}
    assert "credentials" not in build.call_args.kwargs


@pytest.mark.parametrize("api,version", [("gmail", "v1"), ("docs", "v1"), ("drive", "v3")])
def test_service_transport_times_out_on_stalled_response(
    monkeypatch: pytest.MonkeyPatch, api: str, version: str
) -> None:
    """A real socket read times out without a fetch thread or external network."""
    from google.auth.credentials import AnonymousCredentials

    from fieldkit import google_oauth

    monkeypatch.setattr(google_oauth, "GOOGLE_HTTP_TIMEOUT_SECONDS", 0.05)
    with patch("googleapiclient.discovery.build") as build:
        result = google_oauth.build_google_service(api, version, AnonymousCredentials())
    assert result is build.return_value
    transport = build.call_args.kwargs["http"]
    transport.http.proxy_info = None

    client, stalled_peer = socket.socketpair()
    with client, stalled_peer:

        def connect(connection: httplib2.HTTPConnectionWithTimeout) -> None:
            client.settimeout(connection.timeout)
            connection.sock = client

        monkeypatch.setattr(httplib2.HTTPConnectionWithTimeout, "connect", connect)
        started = monotonic()
        try:
            with pytest.raises(TimeoutError, match="timed out"):
                transport.request("http://example.invalid/stalled")
            assert monotonic() - started < 2
        finally:
            transport.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_http_error(status: int) -> HttpError:
    """Construct a googleapiclient HttpError with the given HTTP status code."""
    resp = httplib2.Response({"status": str(status)})
    return HttpError(resp=resp, content=b"error body")


def _make_doc_response(doc_id: str = "doc123") -> dict:
    """Build a minimal Docs API response dict."""
    return {
        "title": "Test Meeting",
        "tabs": [
            {
                "tabProperties": {"title": "Notes"},
                "documentTab": {"body": {"content": []}},
            }
        ],
    }


def _make_service(execute_mock: MagicMock) -> MagicMock:
    """Build a mock Docs API service whose .execute() is controlled by execute_mock."""
    service = MagicMock()
    service.documents.return_value.get.return_value.execute = execute_mock
    return service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# ── TestFetchGeminiDocRetry (flattened) ─────────────────────────────────────


def test_fetch_gemini_doc_retry_503_retried_twice_then_succeeds() -> None:
    """HttpError 503 on first two calls → success on third; execute called 3 times."""
    from fieldkit.ingest.docs import fetch_gemini_doc

    err_503 = _make_http_error(503)
    good_response = _make_doc_response()

    execute_mock = MagicMock(side_effect=[err_503, err_503, good_response])
    service = _make_service(execute_mock)

    with patch("time.sleep"):
        result = fetch_gemini_doc(service, "doc123")

    assert result.doc_id == "doc123"
    assert result.doc_title == "Test Meeting"
    assert execute_mock.call_count == 3


def test_fetch_gemini_doc_retry_404_raises_doc_not_found_immediately() -> None:
    """HttpError 404 → DocNotFoundError after exactly 1 call (no retry)."""
    from fieldkit.ingest.docs import DocNotFoundError, fetch_gemini_doc

    err_404 = _make_http_error(404)
    execute_mock = MagicMock(side_effect=err_404)
    service = _make_service(execute_mock)

    with patch("time.sleep"), pytest.raises(DocNotFoundError) as exc_info:
        fetch_gemini_doc(service, "doc123")

    assert exc_info.value.doc_id == "doc123"
    assert execute_mock.call_count == 1


def test_fetch_gemini_doc_terminal_401_requires_authentication(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    from fieldkit.cli_exit import handle_cli_exception
    from fieldkit.errors import AuthError
    from fieldkit.ingest.docs import fetch_gemini_doc

    failure = HttpError(
        resp=httplib2.Response({"status": "401"}),
        content=b"provider-payload-sentinel",
        uri="https://example.com/provider-uri-sentinel",
    )
    execute = MagicMock(side_effect=failure)
    with pytest.raises(AuthError, match="fieldkit auth google") as caught:
        fetch_gemini_doc(_make_service(execute), "doc-id-sentinel")
    result = handle_cli_exception(caught.value)
    assert result == 2
    execute.assert_called_once()
    assert caught.value.__suppress_context__
    output = capsys.readouterr()
    assert "sentinel" not in output.out + output.err + caplog.text


def test_fetch_gemini_doc_retry_403_raises_doc_access_denied_immediately() -> None:
    """HttpError 403 → DocAccessDeniedError after exactly 1 call (no retry)."""
    from fieldkit.ingest.docs import DocAccessDeniedError, fetch_gemini_doc

    err_403 = _make_http_error(403)
    execute_mock = MagicMock(side_effect=err_403)
    service = _make_service(execute_mock)

    with patch("time.sleep"), pytest.raises(DocAccessDeniedError) as exc_info:
        fetch_gemini_doc(service, "doc123")

    assert exc_info.value.doc_id == "doc123"
    assert execute_mock.call_count == 1


def test_fetch_gemini_doc_transcript_tab_title_renamed_falls_back_to_index(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """historic regression: title heuristic misses a renamed transcript tab; index fallback recovers it and warns."""
    from fieldkit.ingest.docs import fetch_gemini_doc

    response = {
        "title": "Renamed Tab Meeting",
        "tabs": [
            {
                "tabProperties": {"title": "Notes"},
                "documentTab": {"body": {"content": []}},
            },
            {
                "tabProperties": {"title": "private-tab-sentinel"},
                "documentTab": {"body": {"content": []}},
            },
        ],
    }
    execute_mock = MagicMock(return_value=response)
    service = _make_service(execute_mock)

    with caplog.at_level("WARNING"):
        result = fetch_gemini_doc(service, "private-document-sentinel")

    assert result.transcript_text is not None
    assert result.tab_count == 2
    assert any("transcript tab not identified" in rec.message for rec in caplog.records)
    assert "private-tab-sentinel" not in caplog.text
    assert "private-document-sentinel" not in caplog.text
