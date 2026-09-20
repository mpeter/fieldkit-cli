"""Two-stage LLM extraction pipeline and vault note renderer for Gemini transcripts.

Stage 1: Clean raw transcript text (filler removal, paragraph restructuring).
Stage 2: Extract structured metadata (participants, action items, key decisions).
Render:  Convert doc content + routing + metadata into a vault-compatible .md file.

Public API
----------
TranscriptMeta          — dataclass for structured extraction output
stage1_clean()          — remove filler; returns cleaned transcript text
stage2_extract()        — JSON extraction into TranscriptMeta
render_vault_note()     — render a complete vault meeting-note markdown string
compute_vault_path()    — compute the destination Path under data_root/accounts/
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple, cast

from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.router import RouteResult
from fieldkit.llm.core import _NO_LLM_STUB, synthesize
from fieldkit.llm.sanitize import UNTRUSTED_DATA_PREAMBLE, wrap_user_data

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

ConfidenceLevel = Literal["high", "medium", "low", "none", "stub"]


@dataclass
class TranscriptMeta:
    """Structured metadata extracted from a cleaned transcript or notes body."""

    participants: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)
    key_topics: list[str] = field(default_factory=list)
    confidence: ConfidenceLevel = "stub"
    accounts: list[str] = field(default_factory=list)
    pursuits: list[str] = field(default_factory=list)


class Stage1Result(NamedTuple):
    """Return value of stage1_clean() — carries both the cleaned text and a bypass flag.

    When bypassed=True, the transcript was either too short for LLM cleaning or was
    truncated before being sent (historic regression). In both cases stage2_extract caps confidence
    at 'low' — the input quality is degraded and extracted data may be incomplete.
    """

    text: str
    bypassed: bool = False


# ---------------------------------------------------------------------------
# Stage 1 — transcript cleaning
# ---------------------------------------------------------------------------

_MIN_TRANSCRIPT_CHARS = 100  # shorter than this is not a real transcript
# Paired upper bound. Transcripts longer than this are truncated (head + tail) before stage 1 processing.
_MAX_TRANSCRIPT_CHARS: int = 200_000

_STAGE1_PROMPT_TEMPLATE = """\
{preamble}

You are a meeting transcript editor. Clean the following raw transcript text:

1. Remove filler words and verbal tics (um, uh, like, you know, sort of, kind of).
2. Restructure run-on sentences into clear paragraphs.
3. Add speaker labels where identifiable (e.g. "Speaker A:", "Alice:").
4. Identify and list meeting participants at the top under "Participants:".
5. Preserve all substantive content — do not summarize or drop information.
6. Return the cleaned transcript as plain text only (no markdown fences).

Raw transcript:
---
{raw_transcript_safe}
---
"""


def stage1_clean(raw_transcript: str) -> Stage1Result:
    """Clean a raw meeting transcript using Stage 1 LLM prompt.

    historic regression: Returns raw_transcript unchanged (bypassed=True) when it is shorter
    than _MIN_TRANSCRIPT_CHARS — very short inputs (empty strings, \"[silence]\",
    etc.) waste LLM quota and produce garbage output. stage2_extract caps
    confidence at 'low' when Stage1Result.bypassed is True.

    historic regression: When the transcript exceeds _MAX_TRANSCRIPT_CHARS, it is truncated
    to head+tail before being sent to the LLM. The returned Stage1Result has
    bypassed=True so stage2_extract caps confidence at 'low' — the middle section
    is missing and extracted data (MEDDPICC, action items) may be incomplete.

    When NO_LLM=1, returns the raw transcript unchanged (stub passthrough,
    bypassed=False — the stub path is not a bypass; it is a deliberate no-op).

    Args:
        raw_transcript: Full raw text from the Transcript tab of a Gemini doc.

    Returns:
        Stage1Result with cleaned text and a bypassed flag. When bypassed=True,
        callers must cap confidence at 'low'.
    """
    stripped = raw_transcript.strip()
    if not stripped or len(stripped) < _MIN_TRANSCRIPT_CHARS:
        # Too short to clean — pass through as-is; signal bypass to stage2.
        return Stage1Result(text=raw_transcript, bypassed=True)
    raw_text = stripped
    truncated = False
    if len(raw_text) > _MAX_TRANSCRIPT_CHARS:
        half = _MAX_TRANSCRIPT_CHARS // 2
        logger.warning(
            "stage1_clean: transcript truncated from %d to %d chars (head+tail); "
            "stage2 confidence will be capped at 'low' (historic regression)",
            len(raw_text),
            _MAX_TRANSCRIPT_CHARS,
        )
        raw_text = raw_text[:half] + raw_text[-half:]
        truncated = True
    # historic regression: wrap raw transcript in <user_data> delimiters before interpolation.
    prompt = _STAGE1_PROMPT_TEMPLATE.format(
        preamble=UNTRUSTED_DATA_PREAMBLE,
        raw_transcript_safe=wrap_user_data(raw_text, "raw_transcript"),
    )
    result = synthesize(prompt)

    # Detect NO_LLM stub → return input unchanged (not a bypass — stub is intentional)
    if result == _NO_LLM_STUB:
        return Stage1Result(text=raw_transcript, bypassed=False)

    # historic regression: propagate truncation flag so stage2 caps confidence at 'low'.
    return Stage1Result(text=result, bypassed=truncated)


# ---------------------------------------------------------------------------
# Stage 2 — structured extraction
# ---------------------------------------------------------------------------

_STAGE2_PROMPT_TEMPLATE = """\
{preamble}

You are a meeting analyst. Extract structured information from the following \
meeting transcript or notes.

Return a JSON object with exactly these keys:
  "participants": list of strings (names or "Name (Company)" format)
  "action_items": list of strings (each a specific task with owner if known)
  "key_decisions": list of strings (decisions made during the meeting)
  "key_topics": list of strings (main subjects discussed)
  "confidence": one of "high", "medium", "low" \
(your confidence in extraction quality)

Return raw JSON only — no markdown code fences, no explanation.

Meeting text:
---
{cleaned_text_safe}
---
"""

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")
_EMAIL_DOMAIN_RE = re.compile(r"@([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)")


def _parse_stage2_json(raw: str) -> dict[str, object]:
    """Parse JSON from LLM response, stripping markdown code fences if present."""
    text = raw.strip()

    # Strip code fences
    m = _CODE_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()

    # json.loads() returns Any; narrow to the expected dict shape via cast.
    # The caller validates individual field types via _str_list() before use.
    parsed: dict[str, object] = json.loads(text)
    return parsed


def stage2_extract(stage1_result: Stage1Result | str) -> TranscriptMeta:
    """Extract structured metadata from cleaned transcript text.

    Accepts either a Stage1Result (preferred) or a plain str (backwards compat
    for callers that pass notes_text or other non-Stage1 text directly).

    When stage1_result.bypassed is True, confidence is capped at 'low' regardless
    of what the LLM returns — a bypassed Stage 1 means the input was too short
    for meaningful extraction. Extraction still runs so fields are preserved.

    When NO_LLM=1, returns TranscriptMeta with empty lists and confidence='stub'.
    On malformed JSON response, logs a warning and returns an empty TranscriptMeta.

    Args:
        stage1_result: Stage1Result from stage1_clean(), or a plain str for
                       notes_text fallback paths.

    Returns:
        TranscriptMeta populated from LLM response, or with stub/empty values.
    """
    # Normalise: accept both Stage1Result and plain str for backwards compat.
    if isinstance(stage1_result, Stage1Result):
        cleaned_text = stage1_result.text
        bypassed = stage1_result.bypassed
    else:
        cleaned_text = stage1_result
        bypassed = False

    # historic regression: wrap cleaned transcript in <user_data> delimiters before interpolation.
    prompt = _STAGE2_PROMPT_TEMPLATE.format(
        preamble=UNTRUSTED_DATA_PREAMBLE,
        cleaned_text_safe=wrap_user_data(cleaned_text, "cleaned_text"),
    )
    result = synthesize(prompt)

    # NO_LLM stub path
    if result == _NO_LLM_STUB:
        return TranscriptMeta(confidence="stub")

    # Parse JSON
    try:
        data = _parse_stage2_json(result)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("stage2_extract: malformed JSON from LLM (%s); returning empty meta", exc, exc_info=True)
        return TranscriptMeta(confidence="low")

    def _str_list(key: str) -> list[str]:
        val = data.get(key, [])
        if not isinstance(val, list):
            return []
        return [str(item) for item in val if item]

    confidence_raw = str(data.get("confidence", "low")).lower()
    # Narrow the type: only accept known Literal values; fall back to "low".
    # cast() is used instead of type: ignore to keep mypy coverage on this branch.
    confidence: ConfidenceLevel = cast(
        ConfidenceLevel, confidence_raw if confidence_raw in {"high", "medium", "low"} else "low"
    )

    # historic regression: Cap confidence at 'low' when Stage 1 was bypassed (transcript too short
    # for cleaning). Extraction runs in full — fields are preserved — but confidence
    # is capped so callers know the result quality is limited.
    if bypassed:
        confidence = "low"

    return TranscriptMeta(
        participants=_str_list("participants"),
        action_items=_str_list("action_items"),
        key_decisions=_str_list("key_decisions"),
        key_topics=_str_list("key_topics"),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def primary_account(route: RouteResult) -> str:
    """Return the primary account name from a RouteResult.

    Always safe — RouteResult.accounts is validated to be non-empty
    (empty list is coerced to ['unknown'] in RouteResult.__post_init__).

    Args:
        route: RouteResult from route_by_domains() or route_with_pursuits().

    Returns:
        The first account name in route.accounts.
    """
    return route.accounts[0]


# ---------------------------------------------------------------------------
# Vault note renderer
# ---------------------------------------------------------------------------


def _yaml_list(items: list[str], indent: str = "  ") -> str:
    """Render a Python list as a YAML sequence block."""
    if not items:
        return "[]"
    return "\n" + "\n".join(f"{indent}- {_yaml_escape(item)}" for item in items)


def _yaml_escape(value: str) -> str:
    """Minimally escape a string for inline YAML value use."""
    # Wrap in double quotes if value contains special YAML characters
    if any(c in value for c in (":", "#", "[", "]", "{", "}", ",", '"', "'", "\n")):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _diverges(gemini_steps: list[str], extracted: list[str], threshold: float = 0.5) -> bool:
    """Return True when gemini_next_steps and extracted action_items diverge significantly.

    Uses simple token overlap heuristic: if fewer than *threshold* fraction of
    gemini_steps tokens appear anywhere in extracted text, consider diverged.
    """
    if not gemini_steps:
        return False

    combined_extracted = " ".join(extracted).lower()
    matches = sum(
        1 for step in gemini_steps if any(word in combined_extracted for word in step.lower().split() if len(word) > 4)
    )
    overlap_ratio = matches / len(gemini_steps)
    return overlap_ratio < threshold


def _yaml_field(key: str, items: list[str]) -> str:
    """Render a YAML list field, using [] for empty lists."""
    return f"{key}: {_yaml_list(items)}" if items else f"{key}: []"


def _internal_identity() -> tuple[str, set[str]]:
    """Load the configured user name and organization domains for attendee classification."""
    try:
        from fieldkit.config import get_internal_domains, get_user_email, get_user_name

        user_name = get_user_name().lower().strip()
        user_email = (get_user_email() or "").lower().strip()
        domains = {domain.lower().lstrip("@").strip() for domain in get_internal_domains() if domain.strip()}
        if "@" in user_email:
            domains.add(user_email.split("@")[-1])
        return user_name, domains
    except Exception:  # noqa: BLE001
        logger.debug("transcript_pipeline: failed to load internal identity from config", exc_info=True)
        return "", set()


def _is_internal_participant(participant: str, user_name: str, internal_domains: set[str]) -> bool:
    """Classify one participant using an exact user name or email-domain match."""
    normalized = participant.lower()
    participant_domains = {match.group(1).rstrip(".") for match in _EMAIL_DOMAIN_RE.finditer(normalized)}
    return bool((user_name and normalized == user_name) or participant_domains.intersection(internal_domains))


def _partition_attendees(participants: list[str]) -> tuple[list[str], list[str]]:
    """Partition attendees into configured internal and external groups."""
    user_name, internal_domains = _internal_identity()
    internal = [
        participant
        for participant in participants
        if _is_internal_participant(participant, user_name, internal_domains)
    ]
    external = [participant for participant in participants if participant not in internal]
    return internal, external


def _build_frontmatter(
    *,
    doc_content: GeminiDocContent,
    route: RouteResult,
    meta: TranscriptMeta,
    pipeline_version: str,
    meeting_date: str,
    effective_doc_url: str,
) -> str:
    """Build the YAML frontmatter block for a vault note."""
    primary_acct = primary_account(route)
    all_accounts = route.accounts
    attendees_internal, attendees_external = _partition_attendees(meta.participants)
    source_quality = "full" if doc_content.transcript_text else "summary_only"

    lines = [
        "---",
        f"account: {_yaml_escape(primary_acct)}",
        "type: meeting",
        _yaml_field("attendees_internal", attendees_internal),
        _yaml_field("attendees_external", attendees_external),
        f"source_id: {doc_content.doc_id}",
        "pipeline: transcript-ingest",
        f"pipeline_version: {pipeline_version}",
        f"source_quality: {source_quality}",
        _yaml_field("accounts", all_accounts),
        _yaml_field("pursuits", meta.pursuits),
        _yaml_field("participants", meta.participants),
        _yaml_field("action_items", meta.action_items),
        _yaml_field("key_decisions", meta.key_decisions),
        _yaml_field("key_topics", meta.key_topics),
        f"confidence: {meta.confidence}",
        f"meeting_date: {meeting_date}",
        f"meeting_title: {_yaml_escape(doc_content.doc_title)}",
        f"gemini_doc_url: {effective_doc_url}",
        "---",
    ]
    return "\n".join(lines)


def _build_body(
    *,
    doc_content: GeminiDocContent,
    meta: TranscriptMeta,
    cleaned_body: str,
    meeting_date: str,
    effective_doc_url: str,
    account: str = "unknown",
) -> list[str]:
    """Build the markdown body parts (excluding frontmatter)."""
    parts: list[str] = [
        f"# {doc_content.doc_title}",
        "",
        f"**Date:** {meeting_date}",
        f"**Source:** [Google Doc]({effective_doc_url})",
        "",
    ]

    # Linked pursuits — relative markdown links from meetings/ to pursuits/
    if meta.pursuits:
        parts += ["**Pursuits:** " + ", ".join(f"[{slug}](../pursuits/{slug}.md)" for slug in meta.pursuits), ""]

    parts += ["---", ""]

    if cleaned_body.strip():
        parts += ["## Transcript", "", cleaned_body.strip(), ""]
    else:
        parts += ["## Notes", "", "_No transcript or notes content available._", ""]

    if meta.action_items:
        parts += ["## Action Items", ""]
        parts += [f"- [ ] {item}" for item in meta.action_items]
        parts.append("")

    if doc_content.gemini_next_steps and _diverges(doc_content.gemini_next_steps, meta.action_items):
        parts += [
            "## Gemini Suggested Next Steps",
            "",
            "_Note: Gemini's suggested next steps differ from extracted action items above._",
            "",
        ]
        parts += [f"- {step}" for step in doc_content.gemini_next_steps]
        parts.append("")

    return parts


def render_vault_note(
    *,
    doc_content: GeminiDocContent,
    route: RouteResult,
    meta: TranscriptMeta,
    cleaned_body: str,
    pipeline_version: str,
    doc_url: str | None = None,
    meeting_date: str | None = None,
) -> str:
    """Render a complete vault meeting-note markdown string.

    Produces a file with:
      - YAML frontmatter with full provenance (source_id, pipeline, accounts, etc.)
      - Markdown body with cleaned transcript and optional Gemini vs. extracted
        action-items comparison when they diverge.

    Args:
        doc_content:      GeminiDocContent fetched from the Docs API.
        route:            RouteResult from route_by_domains().
        meta:             TranscriptMeta from stage2_extract().
        cleaned_body:     Cleaned transcript text (from stage1_clean, or notes fallback).
        pipeline_version: Pipeline version string (e.g. "0.1.0").

    Returns:
        Full markdown string suitable for writing to a .md vault file.
    """
    if meeting_date is None:
        meeting_date = infer_meeting_date(doc_content.doc_title)
    if meeting_date is None:
        # historic regression: the fallback to today is a guess — say so. historic regression: it is now
        # reached only when the title genuinely carries no date, not merely because the
        # caller omitted the argument.
        logger.warning(
            "transcript_pipeline: could not infer meeting date from doc title %r, falling back to today's date",
            doc_content.doc_title,
        )
        meeting_date = datetime.now(UTC).strftime("%Y-%m-%d")
    effective_doc_url = doc_url or f"https://docs.google.com/document/d/{doc_content.doc_id}"

    frontmatter = _build_frontmatter(
        doc_content=doc_content,
        route=route,
        meta=meta,
        pipeline_version=pipeline_version,
        meeting_date=meeting_date,
        effective_doc_url=effective_doc_url,
    )
    body_parts = _build_body(
        doc_content=doc_content,
        meta=meta,
        cleaned_body=cleaned_body,
        meeting_date=meeting_date,
        effective_doc_url=effective_doc_url,
        account=primary_account(route),
    )

    return "\n".join([frontmatter, "", *body_parts])


# Gemini titles: "Acme Sync - 2026/05/26 14:31 EDT". Also tolerates YYYY-MM-DD.
_GEMINI_DATE_RE = re.compile(r"\b(\d{4})[/-](\d{2})[/-](\d{2})\b")

# Long form: "Acme Sync - May 26, 2026".
_LONG_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),\s+(\d{4})\b"
)


def infer_meeting_date(doc_title: str) -> str | None:
    """Extract ``YYYY-MM-DD`` from a meeting title, or ``None`` if none is present.

    Returns ``None`` rather than today's date so a caller can distinguish "this title
    carries no date" from "this title says today". Conflating those is historic regression: an
    omitted ``meeting_date`` argument and an unparseable title were indistinguishable at
    the call site, so ``ingest reprocess`` silently re-stamped historical notes with the
    reprocess date.

    Both title formats in use are handled. Previously this function knew only the long
    form while the Gemini format — the one virtually every real doc uses — was parsed by
    a separate helper in the CLI layer, so any path that did not go through that helper
    fell straight through to today.
    """
    m = _GEMINI_DATE_RE.search(doc_title)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            logger.debug("transcript_pipeline: %r matched YYYY/MM/DD but is not a real date", m.group(0))

    m = _LONG_DATE_RE.search(doc_title)
    if m:
        try:
            return datetime.strptime(m.group(0), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            logger.debug("transcript_pipeline: %r matched 'Month DD, YYYY' but is not a real date", m.group(0))

    return None


# ---------------------------------------------------------------------------
# Vault path computation
# ---------------------------------------------------------------------------

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9\s-]")
_SLUG_COLLAPSE_RE = re.compile(r"[\s-]+")


def _slugify(text: str) -> str:
    """Convert text to a URL/filename-safe slug.

    Lowercases, strips non-alphanumeric characters (except hyphens), and
    collapses runs of whitespace/hyphens into a single hyphen.

    Examples::

        >>> _slugify("Q4 Planning: Acme Bank Review!")
        'q4-planning-acme-bank-review'
        >>> _slugify("  Hello  World  ")
        'hello-world'
    """
    text = text.lower()
    text = _SLUG_STRIP_RE.sub("", text)
    text = _SLUG_COLLAPSE_RE.sub("-", text)
    return text.strip("-")


def compute_vault_path(
    data_root: Path,
    account: str,
    meeting_date: str,
    meeting_title: str,
) -> Path:
    """Compute the vault file path for a meeting note.

    Returns ``data_root/accounts/<account>/meetings/YYYY-MM-DD-<slug>.md``.

    Args:
        data_root:     Root of the fieldkit-data directory.
        account:       Account name (folder must exist or be creatable).
        meeting_date:  ISO date string (YYYY-MM-DD).
        meeting_title: Raw meeting title; will be slugified.

    Returns:
        Absolute Path for the vault note file.
    """
    slug = _slugify(meeting_title)
    filename = f"{meeting_date}-{slug}.md"
    return data_root / "accounts" / account / "meetings" / filename
