"""fieldkit.meeting.docs_domain — Pursuit Workbook business logic.

Each pursuit links to a single "Pursuit Workbook" Google Doc.
The workbook uses document tabs:
  Tab 1 (Overview): narrative, stakeholders, MEDDPICC, risks, scope
  Additional tabs:  one per meeting, named "YYYY-MM-DD — Title"

Frontmatter field: gdoc_workbook (single doc ID replaces gdoc_narrative/
gdoc_meeting_log/gdoc_proposal).
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fieldkit.config import TIMEOUT_MCP_TOOL, get_mcp_gateway_url, get_user_email_from_env
from fieldkit.pursuit import parse_frontmatter, write_frontmatter_raw

logger = logging.getLogger(__name__)

_GDOC_FIELD = "gdoc_workbook"

# Google Doc IDs are base64url-encoded strings, typically 44 characters.
# This pattern accepts 10-60 alphanumeric/dash/underscore characters to
# reject obviously malformed IDs (empty strings, path traversal, etc.)
# before they are passed to webbrowser.open().
_GDOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,60}$")


@dataclass(frozen=True)
class LinkResult:
    """Result of linking (or discovering an existing link for) a pursuit workbook."""

    url: str
    already_linked: bool


@dataclass(frozen=True)
class NoteResult:
    """Result of adding a meeting note tab to a pursuit workbook."""

    tab_name: str
    url: str


@dataclass(frozen=True)
class MeetingEntry:
    """One pursuit's linked workbook, for `meeting list`."""

    relative_path: Path
    url: str


def _user_google_email() -> str:
    """Return the configured Google email for MCP tool calls.

    Resolution order:
    1. FIELDKIT_USER_EMAIL environment variable
    2. USER environment variable + ``email_domain`` from config.yaml
    3. USER environment variable + domain derived from the ``email`` config key

    Raises RuntimeError if no email can be determined. Configure ``email_domain``
    in config.yaml (e.g. ``email_domain: example.com``) or set FIELDKIT_USER_EMAIL.
    """
    email = get_user_email_from_env()
    if email:
        return email
    raise RuntimeError(
        "Cannot determine user Google email. "
        "Set FIELDKIT_USER_EMAIL or add 'email_domain: your-org.com' to config.yaml."
    )


def _mcpjungle(tool: str, input_data: dict[str, object]) -> str:
    """Invoke an mcpjungle tool and return stdout. Raises RuntimeError on failure."""
    result = subprocess.run(
        ["mcpjungle", "--registry", get_mcp_gateway_url(), "invoke", tool, "--input", json.dumps(input_data)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_MCP_TOOL,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mcpjungle {tool} failed: {result.stderr or result.stdout}")
    return result.stdout or result.stderr


def _read_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    """Return (frontmatter_dict, body_after_second_delimiter)."""
    content = path.read_text(encoding="utf-8")
    result = parse_frontmatter(content)
    if result is None:
        raise ValueError(f"No YAML frontmatter found in {path}")
    return result


def _doc_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def _validated_doc_url(doc_id: str) -> str:
    """Return the Google Docs edit URL after validating the doc ID format.

    Raises ValueError if doc_id does not match the expected pattern.
    This prevents malformed or injected IDs from being passed to
    webbrowser.open() (HIGH severity finding from review council).
    """
    if not _GDOC_ID_RE.fullmatch(doc_id):
        raise ValueError(f"Invalid Google Doc ID in frontmatter: {doc_id!r}")
    return _doc_url(doc_id)


def _set_pageless(doc_id: str) -> None:
    """Apply pageless document mode via the Docs API."""
    from fieldkit.ingest.docs import get_docs_service

    docs = get_docs_service()
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {
                    "updateDocumentStyle": {
                        "documentStyle": {"documentFormat": {"documentMode": "PAGELESS"}},
                        "fields": "documentFormat",
                    }
                }
            ]
        },
    ).execute()


def _media_markdown(content: str) -> object:
    """Return a MediaUpload object for a markdown string."""
    from googleapiclient.http import MediaInMemoryUpload

    return MediaInMemoryUpload(
        content.encode("utf-8"),
        mimetype="text/markdown",
        resumable=False,
    )


def _account_name(pursuit_file: Path) -> str:
    """Extract account name from path: accounts/<account>/pursuits/<file>.

    Returns the account slug (uppercased) when the path matches the standard
    structure.  When the path does not contain an 'accounts' segment, logs a
    warning and returns an empty string rather than silently returning a wrong
    directory name (historic regression).
    """
    parts = pursuit_file.parts
    try:
        idx = next(i for i, p in enumerate(parts) if p == "accounts")
        return parts[idx + 1].upper()
    except (StopIteration, IndexError):
        logger.warning(
            "meeting: cannot infer account name from path %r — "
            "expected accounts/<slug>/pursuits/<file>; using empty string",
            str(pursuit_file),
        )
        return ""


_OVERVIEW_TEMPLATE = """\
# {pursuit_title}

**Account:** {account}
**Pursuit:** {pursuit_stem}
**Last Updated:** {today}

---

## Summary

*[2-3 paragraph narrative: what the opportunity is, how it originated, current status and next milestone]*

---

## Key Stakeholders

| Contact | Signal |
|---------|--------|
| [Name](mailto:email@example.com), Title, Team | **Champion / Economic Buyer / Influencer** — context |

---

## MEDDPICC Narrative

*[Prose commentary on deal quality. Complements the scores in the markdown frontmatter.]*

**Economic Buyer:**

**Champion:**

**Biggest Risk:**

---

## Risks

| Risk | Severity | Status |
|------|----------|--------|
| | HIGH / MEDIUM / LOW | Open |

---

## Current Scope

**Period:**
**Total:** $

| Role | Hours | Rate | Total |
|------|-------|------|-------|
| **Grand Total** | | | **$** |

---

## Thread Log

*Chronological deal history — key meetings, decisions, pivots, stakeholder moves.*

---
"""


def link(pursuit_file: Path) -> LinkResult:
    """Create a Pursuit Workbook GDoc and write its ID to frontmatter.

    The workbook is a single pageless Google Doc with tabs:
      Tab 1 (Overview): narrative, stakeholders, MEDDPICC, risks, scope
      Additional tabs are added per meeting via `add_note()`.

    Returns the resulting URL. If a workbook is already linked, returns it
    with ``already_linked=True`` and does not create a new one.
    """
    expected_mtime = pursuit_file.stat().st_mtime
    fm, body = _read_frontmatter(pursuit_file)

    if fm.get(_GDOC_FIELD):
        return LinkResult(url=_doc_url(str(fm[_GDOC_FIELD])), already_linked=True)

    account = _account_name(pursuit_file)
    pursuit_title = pursuit_file.stem.replace("-", " ").title()
    for line in body.splitlines():
        if line.startswith("# "):
            pursuit_title = line[2:].strip()
            break

    title = f"{account} -- {pursuit_title} -- Pursuit Workbook"
    overview_md = _OVERVIEW_TEMPLATE.format(
        pursuit_title=pursuit_title,
        account=account,
        pursuit_stem=pursuit_file.stem,
        today=date.today().isoformat(),
    )

    # Create doc via Drive API with markdown→GDoc conversion
    from fieldkit.ingest.docs import get_drive_service

    drive = get_drive_service()
    media = (
        drive.files()
        .create(
            body={
                "name": title,
                "mimeType": "application/vnd.google-apps.document",
            },
            media_body=_media_markdown(overview_md),
            fields="id",
        )
        .execute()
    )
    doc_id: str = media["id"]

    # Set pageless and rename the default tab to "Overview"
    _set_pageless(doc_id)
    _mcpjungle(
        "google_workspace__manage_doc_tab",
        {
            "document_id": doc_id,
            "user_google_email": _user_google_email(),
            "action": "rename",
            "tab_id": "t.0",
            "title": "Overview",
        },
    )

    # Validate the doc ID before writing to frontmatter and returning it.
    # _validated_doc_url() raises ValueError on malformed IDs — defense in
    # depth even for API-sourced IDs.
    url = _validated_doc_url(doc_id)
    fm[_GDOC_FIELD] = doc_id
    write_frontmatter_raw(pursuit_file, fm, body, expected_mtime=expected_mtime)

    return LinkResult(url=url, already_linked=False)


def open_doc(pursuit_file: Path) -> str:
    """Return the URL of the linked Pursuit Workbook.

    Raises RuntimeError if no workbook is linked.
    """
    fm, _ = _read_frontmatter(pursuit_file)
    doc_id = str(fm.get(_GDOC_FIELD, ""))
    if not doc_id:
        raise RuntimeError(f"No Pursuit Workbook linked. Run: fieldkit meeting link {pursuit_file}")
    return _validated_doc_url(doc_id)


def add_note(pursuit_file: Path, meeting_title: str, content: str) -> NoteResult:
    """Add a meeting note as a new tab in the Pursuit Workbook.

    Raises RuntimeError if no workbook is linked or the MCP call fails.
    """
    fm, _ = _read_frontmatter(pursuit_file)
    doc_id = str(fm.get(_GDOC_FIELD, ""))
    if not doc_id:
        raise RuntimeError("No Pursuit Workbook linked. Run: fieldkit meeting link <pursuit>")

    today = date.today().isoformat()
    tab_name = f"{today} — {meeting_title}" if meeting_title else f"{today} — Meeting Note"

    # historic regression: omit 'index' entirely — the API appends the tab at the natural end
    # position by default.  Passing index=999 always exceeds the valid range
    # [0, current_tab_count] and the API rejects the call.
    note_md = f"# {tab_name}\n\n{content}\n" if content.strip() else f"# {tab_name}\n"
    result_raw = _mcpjungle(
        "google_workspace__manage_doc_tab",
        {
            "document_id": doc_id,
            "user_google_email": _user_google_email(),
            "action": "create",
            "title": tab_name,
        },
    )
    if not result_raw or not result_raw.strip():
        raise RuntimeError(
            "Could not add meeting note: Drive API returned an empty response. "
            "Check that the fieldkit-docs MCP group is connected."
        )
    try:
        result = json.loads(result_raw)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Could not add meeting note: Drive API returned unexpected response "
            f"(not JSON). Check MCP connection.\nRaw: {result_raw[:200]}"
        ) from None
    tab_id = result.get("result", {}).get("tab_id", "")

    # Populate the tab with meeting note content
    if tab_id:
        _mcpjungle(
            "google_workspace__manage_doc_tab",
            {
                "document_id": doc_id,
                "user_google_email": _user_google_email(),
                "action": "populate_from_markdown",
                "tab_id": tab_id,
                "content": note_md,
            },
        )

    url = _validated_doc_url(doc_id)
    return NoteResult(tab_name=tab_name, url=url)


def list_meetings(data_root: Path, account: str | None = None) -> list[MeetingEntry]:
    """List all pursuits that have a linked Pursuit Workbook.

    Scans accounts/*/pursuits/*.md under the data root and returns one entry
    per pursuit whose frontmatter contains a non-empty 'gdoc_workbook' field.

    Args:
        data_root: fieldkit_home.
        account: Restrict to ``accounts/<account>/pursuits/`` when given. The
            slug is validated at the CLI boundary, so an unknown one cannot
            reach here; a configured account with no workbooks yields [].
    """
    entries: list[MeetingEntry] = []
    glob = "accounts/*/pursuits/*.md" if account is None else f"accounts/{account}/pursuits/*.md"
    pursuit_files = sorted(data_root.glob(glob))
    for pursuit_file in pursuit_files:
        try:
            fm, _ = _read_frontmatter(pursuit_file)
        except ValueError:
            # Skip files with no frontmatter — do not crash
            continue
        workbook_id = str(fm.get(_GDOC_FIELD, "")).strip()
        if workbook_id:
            entries.append(
                MeetingEntry(
                    relative_path=pursuit_file.relative_to(data_root),
                    url=_doc_url(workbook_id),
                )
            )
    return entries
