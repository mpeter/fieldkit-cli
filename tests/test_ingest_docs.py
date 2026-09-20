"""Tests for fieldkit.ingest.docs — fetch_gemini_doc() retry behaviour (T038).

Verifies that:
- Transient HttpError (503) is retried up to 3 times.
- Permanent HttpError (404) raises DocNotFoundError immediately (no retry).
- Permanent HttpError (403) raises DocAccessDeniedError immediately (no retry).
"""

from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

pytestmark = pytest.mark.unit


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
                "tabProperties": {"title": "Full Recording Text"},
                "documentTab": {"body": {"content": []}},
            },
        ],
    }
    execute_mock = MagicMock(return_value=response)
    service = _make_service(execute_mock)

    with caplog.at_level("WARNING"):
        result = fetch_gemini_doc(service, "doc123")

    assert result.transcript_text is not None
    assert result.tab_count == 2
    assert any("transcript tab not identified" in rec.message for rec in caplog.records)
