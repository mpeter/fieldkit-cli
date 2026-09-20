"""Drive/Docs fetcher for Gemini meeting documents.

Provides:
  get_docs_service()      — authenticated Google Docs API service
  fetch_gemini_doc()      — fetch Notes+Transcript tabs from a Gemini doc
  extract_text_from_tab() — recursive text extraction from Docs API structure
  parse_invited_emails()  — extract email addresses from Notes 'Invited:' section
  parse_next_steps()      — extract checklist items from 'Next Steps' section

Custom exceptions:
  DocNotFoundError       — HTTP 404 from the Docs API
  DocAccessDeniedError   — HTTP 403 from the Docs API
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fieldkit.config.retry import RETRY_TRANSIENT_STATUSES, transient_retry
from fieldkit.errors import FieldkitError
from fieldkit.google_oauth import refresh_google_credentials

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class DocNotFoundError(FieldkitError):
    """Raised when the Docs API returns HTTP 404 for a document."""

    def __init__(self, doc_id: str) -> None:
        super().__init__(f"Document not found: {doc_id!r}")
        self.doc_id = doc_id


class DocAccessDeniedError(FieldkitError):
    """Raised when the Docs API returns HTTP 403 for a document."""

    def __init__(self, doc_id: str) -> None:
        super().__init__(f"Access denied for document: {doc_id!r}")
        self.doc_id = doc_id


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GeminiDocContent:
    """Structured content extracted from a Gemini meeting Google Doc."""

    doc_id: str
    doc_title: str
    notes_text: str
    transcript_text: str | None
    invited_emails: list[str] = field(default_factory=list)
    gemini_next_steps: list[str] = field(default_factory=list)
    tab_count: int = 1


# ---------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------


def get_docs_service(token_path: Path | None = None) -> Any:
    """Return an authenticated Google Docs v1 service.

    Args:
        token_path: Path to the OAuth2 token JSON file. Defaults to
            ``<umbrella-root>/data/<user-email>.json``.

    Returns:
        A ``googleapiclient.discovery.Resource`` for the Docs v1 API.

    Raises:
        FileNotFoundError: If token_path does not exist.
        google.auth.exceptions.RefreshError: If the token cannot be refreshed.
    """
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=_get_creds(token_path))


def _get_creds(token_path: Path | None = None) -> Any:
    """Return refreshed Google OAuth credentials from the fieldkit token file."""
    from google.oauth2.credentials import Credentials

    if token_path is None:
        from fieldkit.config import get_google_token_path

        token_path = get_google_token_path()

    if not token_path.exists():
        raise FileNotFoundError(
            f"OAuth token not found at {token_path}. Run 'fieldkit gmail sync' once to complete the Google OAuth flow."
        )

    from fieldkit.config import GOOGLE_OAUTH_SCOPES

    creds: Credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
        str(token_path),
        scopes=GOOGLE_OAUTH_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        refresh_google_credentials(creds, token_path)
    return creds


def get_drive_service(token_path: Path | None = None) -> Any:
    """Return an authenticated Google Drive v3 service.

    Uses the same unified token as :func:`get_docs_service`.  Requires the
    ``drive`` scope (read+write) — see ``GOOGLE_OAUTH_SCOPES`` in config.

    Args:
        token_path: Optional override for the token file path.

    Returns:
        A ``googleapiclient.discovery.Resource`` for the Drive v3 API.
    """
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_get_creds(token_path))


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------


def extract_text_from_tab(tab: dict[str, Any]) -> str:
    """Recursively extract plain text from a Google Docs API tab structure.

    Walks ``tab['documentTab']['body']['content']`` (or ``tab['body']['content']``
    for top-level docs) and collects all ``textRun.content`` strings.

    Args:
        tab: A single tab dict from the Docs API response.

    Returns:
        The concatenated plain text of all text runs in the tab.
    """
    # Support both tab-level and document-level structures
    body = tab.get("documentTab", {}).get("body") or tab.get("body") or {}
    content = body.get("content", [])
    return _extract_text_from_content(content)


def _extract_text_from_content(content: list[Any]) -> str:
    """Walk a Docs API 'content' list and collect all text."""
    parts: list[str] = []
    for element in content:
        if not isinstance(element, dict):
            continue

        # Paragraph element
        paragraph = element.get("paragraph")
        if paragraph:
            for pe in paragraph.get("elements", []):
                text_run = pe.get("textRun")
                if text_run:
                    parts.append(text_run.get("content", ""))
            continue

        # Table element — recurse into cells
        table = element.get("table")
        if table:
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    parts.append(_extract_text_from_content(cell.get("content", [])))
            continue

        # SectionBreak, pageBreak — ignored for text purposes

    return "".join(parts)


# ---------------------------------------------------------------------------
# Notes-tab parsers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Match lines that look like checklist items: "[ ] text" or "☐ text" or "- text"
_CHECKLIST_RE = re.compile(
    r"^\s*(?:\[[ xX]\]|☐|☑|✓|•|-|\u2013|—|\*)\s*(.+)$",
    re.MULTILINE,  # \u2013 = EN DASH
)

# Section headers that introduce next steps
_NEXT_STEPS_SECTION_RE = re.compile(
    r"(?:suggested\s+)?next\s+steps?",
    re.IGNORECASE,
)

_INVITED_SECTION_RE = re.compile(r"invited:", re.IGNORECASE)


def parse_invited_emails(notes_text: str) -> list[str]:
    """Extract email addresses from the 'Invited:' section of Notes text.

    Finds the line starting with 'Invited:' and collects all email-shaped
    tokens from that line onward, stopping at the next blank line or section
    header.

    Args:
        notes_text: Full text of the Notes tab.

    Returns:
        Deduplicated list of email addresses in discovery order.
    """
    lines = notes_text.splitlines()
    in_invited = False
    collected: list[str] = []
    seen: set[str] = set()

    for line in lines:
        if _INVITED_SECTION_RE.search(line):
            in_invited = True

        if in_invited:
            for email in _EMAIL_RE.findall(line):
                email_lower = email.lower()
                if email_lower not in seen:
                    seen.add(email_lower)
                    collected.append(email)

            # Stop at a blank line after we've seen at least one email,
            # or at a new section header (line ending with ':' that isn't the
            # Invited line itself) — to avoid consuming the whole document.
            stripped = line.strip()
            if collected and not stripped:
                break
            if collected and stripped.endswith(":") and not _INVITED_SECTION_RE.search(line):
                break

    return collected


def parse_next_steps(notes_text: str) -> list[str]:
    """Extract checklist items from the 'Next Steps' section of Notes text.

    Finds the first section header matching 'Next Steps' / 'Suggested next
    steps', then collects checklist-style lines until the next blank line or
    section header.

    Args:
        notes_text: Full text of the Notes tab.

    Returns:
        List of next-step action strings (checklist markers stripped).
    """
    lines = notes_text.splitlines()
    in_section = False
    items: list[str] = []

    for line in lines:
        if _NEXT_STEPS_SECTION_RE.search(line):
            in_section = True
            continue

        if not in_section:
            continue

        stripped = line.strip()

        # End of section: two consecutive blank lines or a new section header
        if not stripped:
            if items:
                # One blank line after content — stop collecting
                break
            continue

        m = _CHECKLIST_RE.match(line)
        if m:
            items.append(m.group(1).strip())

    return items


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------


def _google_http_status(exc: BaseException) -> int | None:
    """Return a Google API HTTP status without requiring the SDK at import time."""
    try:
        from googleapiclient.errors import HttpError
    except ModuleNotFoundError:
        return None
    if not isinstance(exc, HttpError):
        return None
    return int(exc.resp.status)


def _is_transient_http_error(exc: BaseException) -> bool:
    """Return True for HttpError statuses that are worth retrying.

    Sources ``RETRY_TRANSIENT_STATUSES`` rather than declaring its own set, so this
    path cannot drift from the Salesforce and Gmail predicates. Previously this
    checked ``{429, 500, 503}`` only, silently dropping 502 and 504.

    403 and 404 are permanent failures — they are converted to
    DocAccessDeniedError / DocNotFoundError before tenacity sees them, so
    they will never reach this predicate.  This guard is a belt-and-suspenders
    check for any raw HttpError that slips through.
    """
    status = _google_http_status(exc)
    return status in RETRY_TRANSIENT_STATUSES if status is not None else False


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------


@transient_retry(_is_transient_http_error, logger)
def fetch_gemini_doc(service: Any, doc_id: str) -> GeminiDocContent:
    """Fetch a Gemini meeting Google Doc and extract structured content.

    Calls the Docs API with ``includeTabsContent=True`` to get both the
    Notes tab (index 0 or title contains 'Notes') and the Transcript tab
    (index 1 or title contains 'Transcript').

    Args:
        service: Authenticated Google Docs API service (from ``get_docs_service()``).
        doc_id:  The Google Doc document ID.

    Returns:
        ``GeminiDocContent`` with populated fields.

    Raises:
        DocNotFoundError:       HTTP 404 from the Docs API.
        DocAccessDeniedError:   HTTP 403 from the Docs API.
    """
    try:
        doc = (
            service.documents()
            .get(
                documentId=doc_id,
                includeTabsContent=True,
            )
            .execute()
        )
    except Exception as exc:
        status = _google_http_status(exc)
        if status == 404:
            raise DocNotFoundError(doc_id) from exc
        if status == 403:
            raise DocAccessDeniedError(doc_id) from exc
        raise

    doc_title: str = doc.get("title", "")
    tabs: list[dict[str, Any]] = doc.get("tabs", [])
    tab_count = len(tabs)

    # Identify Notes and Transcript tabs by title, falling back to index order.
    notes_tab: dict[str, Any] | None = None
    transcript_tab: dict[str, Any] | None = None

    for tab in tabs:
        props: dict[str, Any] = tab.get("tabProperties", {})
        title: str = props.get("title", "")
        if "transcript" in title.lower():
            transcript_tab = tab
        elif notes_tab is None:
            # First tab without 'transcript' in name is treated as Notes
            notes_tab = tab

    # historic regression: the title heuristic above misses when Google renames the transcript
    # tab (or a doc grows a 3rd tab). Fall back to index order (0=Notes, 1=Transcript)
    # instead of silently dropping the transcript, and log so the drift is visible.
    if transcript_tab is None and tab_count >= 2:
        tab_titles = [tab.get("tabProperties", {}).get("title", "") for tab in tabs]
        logger.warning(
            "fetch_gemini_doc: transcript tab not identified by title heuristic "
            "(doc_id=%s, tab_titles=%r); falling back to index order (tabs[1])",
            doc_id,
            tab_titles,
        )
        transcript_tab = tabs[1]
        if notes_tab is None:
            notes_tab = tabs[0]

    # If no tabs at all — empty document, return empty content.
    # (Some very old docs may have a top-level body without tabs, but Gemini
    # meeting docs always use the tabs API, so we don't need a fallback here.)

    notes_text = extract_text_from_tab(notes_tab) if notes_tab else ""
    transcript_text = extract_text_from_tab(transcript_tab) if transcript_tab else None

    # If there is only one tab and it doesn't have 'transcript' in the title,
    # transcript_text is None (Notes-only document).
    if tab_count == 1:
        transcript_text = None

    invited_emails = parse_invited_emails(notes_text)
    gemini_next_steps = parse_next_steps(notes_text)

    return GeminiDocContent(
        doc_id=doc_id,
        doc_title=doc_title,
        notes_text=notes_text,
        transcript_text=transcript_text,
        invited_emails=invited_emails,
        gemini_next_steps=gemini_next_steps,
        tab_count=tab_count,
    )
