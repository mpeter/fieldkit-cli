"""Unit tests for lib/drive_docs.py — Drive/Docs fetcher for Gemini meetings.

Strategy:
- FakeDriveService class returns canned Docs API responses (2-tab, 1-tab, empty).
- All Google API calls are mocked — no network requests.
- DocNotFoundError / DocAccessDeniedError tested via FakeHttpError mocks.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from fieldkit.ingest.docs import (
    DocAccessDeniedError,
    DocNotFoundError,
    GeminiDocContent,
    extract_text_from_tab,
    fetch_gemini_doc,
    parse_invited_emails,
    parse_next_steps,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake Docs API helpers
# ---------------------------------------------------------------------------


def _make_paragraph(text: str) -> dict[str, Any]:
    """Build a minimal Docs API paragraph element."""
    return {
        "paragraph": {
            "elements": [
                {"textRun": {"content": text}},
            ]
        }
    }


def _make_tab_body(paragraphs: list[str]) -> dict[str, Any]:
    """Build a documentTab dict with the given paragraph texts."""
    return {"documentTab": {"body": {"content": [_make_paragraph(p) for p in paragraphs]}}}


def _make_tab(title: str, paragraphs: list[str]) -> dict[str, Any]:
    """Build a full tab dict with tabProperties and documentTab body."""
    return {
        "tabProperties": {"title": title, "index": 0},
        **_make_tab_body(paragraphs),
    }


def _make_two_tab_doc(
    doc_id: str = "DOC001",
    doc_title: str = "Meeting Notes",
    notes_paragraphs: list[str] | None = None,
    transcript_paragraphs: list[str] | None = None,
) -> dict[str, Any]:
    """Build a canned Docs API response with Notes + Transcript tabs."""
    notes = notes_paragraphs or [
        "Meeting with the team\n",
        "Invited: alice@example.com, bob@internal.example.com\n",  # pii-guard: ignore
        "\n",
        "Suggested next steps\n",
        "[ ] Follow up with alice on pricing\n",
        "[ ] Send demo link to bob\n",
    ]
    transcript = transcript_paragraphs or [
        "Alice: Welcome everyone.\n",
        "Bob: Thanks for joining.\n",
    ]
    return {
        "title": doc_title,
        "documentId": doc_id,
        "tabs": [
            _make_tab("Notes", notes),
            _make_tab("Transcript", transcript),
        ],
    }


def _make_one_tab_doc(
    doc_id: str = "DOC002",
    doc_title: str = "Notes Only",
    paragraphs: list[str] | None = None,
) -> dict[str, Any]:
    """Build a canned Docs API response with only a Notes tab."""
    paras = paragraphs or [
        "Single tab meeting notes.\n",
        "Invited: charlie@example.com\n",  # pii-guard: ignore
    ]
    return {
        "title": doc_title,
        "documentId": doc_id,
        "tabs": [
            _make_tab("Notes", paras),
        ],
    }


def _make_empty_doc(doc_id: str = "DOC003") -> dict[str, Any]:
    """Build a canned Docs API response with empty tabs."""
    return {
        "title": "Empty Doc",
        "documentId": doc_id,
        "tabs": [],
    }


class FakeDriveService:
    """Minimal fake for the Google Docs API service.

    Supports configuring responses and error injection per doc_id.
    """

    def __init__(self) -> None:
        self._responses: dict[str, Any] = {}
        self._errors: dict[str, Exception] = {}

    def set_response(self, doc_id: str, response: dict[str, Any]) -> None:
        self._responses[doc_id] = response

    def set_error(self, doc_id: str, exc: Exception) -> None:
        self._errors[doc_id] = exc

    def documents(self) -> "_FakeDocuments":
        return _FakeDocuments(self)


class _FakeDocuments:
    def __init__(self, service: FakeDriveService) -> None:
        self._service = service

    def get(self, documentId: str, **_kwargs: Any) -> "_FakeRequest":
        return _FakeRequest(self._service, documentId)


class _FakeRequest:
    def __init__(self, service: FakeDriveService, doc_id: str) -> None:
        self._service = service
        self._doc_id = doc_id

    def execute(self) -> dict[str, Any]:
        if self._doc_id in self._service._errors:
            raise self._service._errors[self._doc_id]
        if self._doc_id in self._service._responses:
            return self._service._responses[self._doc_id]
        raise RuntimeError(f"No response configured for doc_id={self._doc_id!r}")


# ---------------------------------------------------------------------------
# FakeHttpError for 404/403 testing
# ---------------------------------------------------------------------------


class FakeHttpError(Exception):
    """Minimal stand-in for googleapiclient.errors.HttpError."""

    def __init__(self, status: int, doc_id: str) -> None:
        super().__init__(f"HttpError {status}")
        self.resp = MagicMock()
        self.resp.status = str(status)


# ---------------------------------------------------------------------------
# extract_text_from_tab tests
# ---------------------------------------------------------------------------


# ── TestExtractTextFromTab (flattened) ──────────────────────────────────────


def test_extract_text_from_tab_basic_paragraphs() -> None:
    tab = _make_tab("Notes", ["Hello ", "World\n"])
    result = extract_text_from_tab(tab)
    assert "Hello " in result
    assert "World\n" in result


def test_extract_text_from_tab_empty_tab_no_content() -> None:
    tab: dict[str, Any] = {"tabProperties": {"title": "Empty"}, "documentTab": {"body": {"content": []}}}
    result = extract_text_from_tab(tab)
    assert result == ""


def test_extract_text_from_tab_missing_body_returns_empty() -> None:
    tab: dict[str, Any] = {"tabProperties": {"title": "NoBody"}, "documentTab": {}}
    result = extract_text_from_tab(tab)
    assert result == ""


def test_extract_text_from_tab_table_cells_extracted() -> None:
    """Text inside table cells should be included."""
    tab: dict[str, Any] = {
        "documentTab": {
            "body": {
                "content": [{"table": {"tableRows": [{"tableCells": [{"content": [_make_paragraph("cell text\n")]}]}]}}]
            }
        }
    }
    result = extract_text_from_tab(tab)
    assert "cell text" in result


def test_extract_text_from_tab_multiple_text_runs_in_paragraph() -> None:
    """Multiple textRun elements in one paragraph all concatenated."""
    tab: dict[str, Any] = {
        "documentTab": {
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [
                                {"textRun": {"content": "Part A "}},
                                {"textRun": {"content": "Part B\n"}},
                            ]
                        }
                    }
                ]
            }
        }
    }
    result = extract_text_from_tab(tab)
    assert result == "Part A Part B\n"


def test_extract_text_from_tab_falls_back_to_body_key() -> None:
    """Top-level 'body' key used when 'documentTab' is absent."""
    tab: dict[str, Any] = {"body": {"content": [_make_paragraph("top-level body text\n")]}}
    result = extract_text_from_tab(tab)
    assert "top-level body text" in result


# ---------------------------------------------------------------------------
# parse_invited_emails tests
# ---------------------------------------------------------------------------


# ── TestParseInvitedEmails (flattened) ──────────────────────────────────────


def test_parse_invited_emails_simple_email_list() -> None:
    notes = "Meeting prep\nInvited: alice@example.com, bob@internal.example.com\n\nOther content"  # pii-guard: ignore
    emails = parse_invited_emails(notes)
    assert "alice@example.com" in emails  # pii-guard: ignore
    assert "bob@internal.example.com" in emails  # pii-guard: ignore


def test_parse_invited_emails_ignores_non_email_text() -> None:
    notes = "Invited: alice@example.com some random words\n\n"  # pii-guard: ignore
    emails = parse_invited_emails(notes)
    assert emails == ["alice@example.com"]  # pii-guard: ignore


def test_parse_invited_emails_no_invited_section_returns_empty() -> None:
    notes = "No invited section here. Just some text."
    emails = parse_invited_emails(notes)
    assert emails == []


def test_parse_invited_emails_deduplicates_case_insensitive() -> None:
    notes = "Invited: Alice@Example.com, alice@example.com\n\n"  # pii-guard: ignore
    emails = parse_invited_emails(notes)
    assert len(emails) == 1


def test_parse_invited_emails_emails_on_multiple_lines() -> None:
    notes = "Invited: alice@example.com\nbob@internal.example.com\n\n"  # pii-guard: ignore
    emails = parse_invited_emails(notes)
    assert "alice@example.com" in emails  # pii-guard: ignore
    assert "bob@internal.example.com" in emails  # pii-guard: ignore


def test_parse_invited_emails_stops_at_next_section_header() -> None:
    notes = "Invited: alice@example.com\nSuggested next steps:\nbob@internal.example.com\n"  # pii-guard: ignore
    emails = parse_invited_emails(notes)
    assert "alice@example.com" in emails  # pii-guard: ignore
    # bob should not be collected (it's after the new section header)
    assert "bob@internal.example.com" not in emails  # pii-guard: ignore


def test_parse_invited_emails_real_email_shapes() -> None:
    notes = "Invited: first.last+tag@sub-company-co-uk.example.com\n\n"  # pii-guard: ignore
    emails = parse_invited_emails(notes)
    assert "first.last+tag@sub-company-co-uk.example.com" in emails  # pii-guard: ignore


# ---------------------------------------------------------------------------
# parse_next_steps tests
# ---------------------------------------------------------------------------


# ── TestParseNextSteps (flattened) ──────────────────────────────────────────


def test_parse_next_steps_checkbox_items_extracted() -> None:
    notes = "Suggested next steps\n[ ] Send proposal\n[ ] Schedule demo\n\n"
    steps = parse_next_steps(notes)
    assert "Send proposal" in steps
    assert "Schedule demo" in steps


def test_parse_next_steps_next_steps_header_variant() -> None:
    notes = "Next Steps\n- Call Alice\n- Review contract\n\n"
    steps = parse_next_steps(notes)
    assert "Call Alice" in steps
    assert "Review contract" in steps


def test_parse_next_steps_no_next_steps_section_returns_empty() -> None:
    notes = "Meeting notes with no checklist."
    steps = parse_next_steps(notes)
    assert steps == []


def test_parse_next_steps_checked_boxes_included() -> None:
    notes = "Next steps\n[x] Already done\n[ ] Still pending\n\n"
    steps = parse_next_steps(notes)
    assert "Already done" in steps
    assert "Still pending" in steps


def test_parse_next_steps_bullet_dash_items() -> None:
    notes = "Next Steps\n• Item one\n• Item two\n\n"
    steps = parse_next_steps(notes)
    assert "Item one" in steps
    assert "Item two" in steps


def test_parse_next_steps_stops_at_blank_line() -> None:
    notes = "Next Steps\n[ ] Action A\n\n[ ] Not in section\n"
    steps = parse_next_steps(notes)
    assert "Action A" in steps
    assert "Not in section" not in steps


# ---------------------------------------------------------------------------
# fetch_gemini_doc tests
# ---------------------------------------------------------------------------


# ── TestFetchGeminiDoc (flattened) ──────────────────────────────────────────


def test_fetch_gemini_doc_two_tab_doc_notes_and_transcript() -> None:
    service = FakeDriveService()
    doc = _make_two_tab_doc(doc_id="DOC001", doc_title="Test Meeting")
    service.set_response("DOC001", doc)

    result = fetch_gemini_doc(service, "DOC001")

    assert isinstance(result, GeminiDocContent)
    assert result.doc_id == "DOC001"
    assert result.doc_title == "Test Meeting"
    assert result.tab_count == 2
    # Notes content present
    assert "Invited" in result.notes_text
    # Transcript content present
    assert result.transcript_text is not None
    assert "Alice:" in result.transcript_text or "Welcome" in result.transcript_text


def test_fetch_gemini_doc_two_tab_doc_invited_emails_parsed() -> None:
    service = FakeDriveService()
    doc = _make_two_tab_doc(
        doc_id="DOC001",
        notes_paragraphs=[
            "Meeting\n",
            "Invited: alice@example.com, bob@internal.example.com\n",  # pii-guard: ignore
            "\n",
        ],
    )
    service.set_response("DOC001", doc)

    result = fetch_gemini_doc(service, "DOC001")
    assert "alice@example.com" in result.invited_emails  # pii-guard: ignore
    assert "bob@internal.example.com" in result.invited_emails  # pii-guard: ignore
    # Contract: doc_title and notes_text must also be populated
    assert isinstance(result.doc_title, str)
    assert isinstance(result.notes_text, str)


def test_fetch_gemini_doc_two_tab_doc_next_steps_parsed() -> None:
    service = FakeDriveService()
    doc = _make_two_tab_doc(
        doc_id="DOC001",
        notes_paragraphs=[
            "Team meeting\n",
            "Suggested next steps\n",
            "[ ] Follow up with alice on pricing\n",
            "[ ] Send demo link to bob\n",
            "\n",
        ],
    )
    service.set_response("DOC001", doc)

    result = fetch_gemini_doc(service, "DOC001")
    assert len(result.gemini_next_steps) >= 2
    assert any("Follow up" in s for s in result.gemini_next_steps)
    assert any("demo link" in s for s in result.gemini_next_steps)


def test_fetch_gemini_doc_one_tab_doc_transcript_is_none() -> None:
    service = FakeDriveService()
    doc = _make_one_tab_doc(doc_id="DOC002")
    service.set_response("DOC002", doc)

    result = fetch_gemini_doc(service, "DOC002")

    assert result.tab_count == 1
    assert result.transcript_text is None
    assert result.notes_text != ""


def test_fetch_gemini_doc_one_tab_doc_notes_populated() -> None:
    service = FakeDriveService()
    doc = _make_one_tab_doc(
        doc_id="DOC002",
        paragraphs=["Single tab content.\n", "Invited: charlie@example.com\n"],  # pii-guard: ignore
    )
    service.set_response("DOC002", doc)

    result = fetch_gemini_doc(service, "DOC002")
    assert "Single tab content" in result.notes_text
    assert "charlie@example.com" in result.invited_emails  # pii-guard: ignore


def test_fetch_gemini_doc_empty_tabs_list_returns_empty_content() -> None:
    service = FakeDriveService()
    doc = _make_empty_doc(doc_id="DOC003")
    service.set_response("DOC003", doc)

    result = fetch_gemini_doc(service, "DOC003")
    assert result.tab_count == 0
    assert result.notes_text == ""
    assert result.transcript_text is None


def test_fetch_gemini_doc_doc_not_found_raises_doc_not_found_error() -> None:
    try:
        from googleapiclient.errors import HttpError as RealHttpError
    except ImportError:
        pytest.skip("googleapiclient not available")

    fake_resp = MagicMock()
    fake_resp.status = "404"
    fake_resp.reason = "Not Found"
    real_exc = RealHttpError(fake_resp, b"Not Found")

    service = FakeDriveService()
    service.set_error("MISSING404", real_exc)

    with pytest.raises(DocNotFoundError, match="MISSING404") as exc_info:
        fetch_gemini_doc(service, "MISSING404")
    assert exc_info.value.doc_id == "MISSING404"


def test_fetch_gemini_doc_doc_access_denied_raises_doc_access_denied_error() -> None:
    try:
        from googleapiclient.errors import HttpError as RealHttpError
    except ImportError:
        pytest.skip("googleapiclient not available")

    fake_resp = MagicMock()
    fake_resp.status = "403"
    fake_resp.reason = "Forbidden"
    real_exc = RealHttpError(fake_resp, b"Forbidden")

    service = FakeDriveService()
    service.set_error("DENIED", real_exc)

    with pytest.raises(DocAccessDeniedError, match="DENIED") as exc_info:
        fetch_gemini_doc(service, "DENIED")
    assert exc_info.value.doc_id == "DENIED"


def test_fetch_gemini_doc_tab_title_matching_prefers_transcript_name() -> None:
    """Tab titled 'Transcript' gets placed in transcript_text slot."""
    service = FakeDriveService()
    doc: dict[str, Any] = {
        "title": "Gemini Meeting",
        "documentId": "DOC004",
        "tabs": [
            _make_tab("Transcript", ["Speaker A: Hello.\n"]),
            _make_tab("Notes", ["Meeting agenda.\n", "Invited: x@y-com.example.com\n"]),
        ],
    }
    service.set_response("DOC004", doc)

    result = fetch_gemini_doc(service, "DOC004")
    assert result.transcript_text is not None
    assert "Speaker A" in result.transcript_text
    assert "Meeting agenda" in result.notes_text


def test_fetch_gemini_doc_returns_gemini_doc_content_dataclass() -> None:
    service = FakeDriveService()
    doc = _make_two_tab_doc()
    service.set_response("DOC001", doc)

    result = fetch_gemini_doc(service, "DOC001")
    assert isinstance(result, GeminiDocContent)
    assert isinstance(result.doc_id, str)
    assert isinstance(result.doc_title, str)
    assert isinstance(result.notes_text, str)
    assert isinstance(result.invited_emails, list)
    assert isinstance(result.gemini_next_steps, list)
    assert isinstance(result.tab_count, int)


# ---------------------------------------------------------------------------
# DocNotFoundError / DocAccessDeniedError unit tests
# ---------------------------------------------------------------------------


# ── TestCustomExceptions (flattened) ────────────────────────────────────────


def test_custom_exceptions_doc_not_found_error_message() -> None:
    exc = DocNotFoundError("TESTID")
    assert "TESTID" in str(exc)
    assert exc.doc_id == "TESTID"


def test_custom_exceptions_doc_access_denied_error_message() -> None:
    exc = DocAccessDeniedError("TESTID2")
    assert "TESTID2" in str(exc)
    assert exc.doc_id == "TESTID2"


def test_custom_exceptions_doc_not_found_is_exception() -> None:
    assert issubclass(DocNotFoundError, Exception)


def test_custom_exceptions_doc_access_denied_is_exception() -> None:
    assert issubclass(DocAccessDeniedError, Exception)
