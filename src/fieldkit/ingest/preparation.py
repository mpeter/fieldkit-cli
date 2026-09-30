"""Prepare validated transcript intent without publishing file or database effects."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fieldkit.errors import AuthError, LLMError
from fieldkit.ingest.constants import GEMINI_TRANSCRIPT_PIPELINE
from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.paths import compute_vault_path
from fieldkit.ingest.pipeline import TranscriptMeta, primary_account
from fieldkit.ingest.prepared import PreparedMeeting, PreparedTask
from fieldkit.ingest.router import RouteResult
from fieldkit.ingest.sources import SourceRecord
from fieldkit.ingest.writeback import classify_meeting_tasks, parse_meeting_frontmatter


@dataclass(frozen=True)
class _CleanResult:
    cleaned: str
    meta: TranscriptMeta
    used_fallback: bool


def _route_source(src: SourceRecord, doc_content: GeminiDocContent, *, data_root: Path) -> RouteResult:
    """Return a RouteResult for *src* using domain-then-title routing with pursuit matching.

    Routing priority:
    1. Email domains present -> route_with_pursuits (domain + pursuit keyword matching).
    2. No domains / unknown  -> route_by_title (title keyword matching).
    3. Either path that resolves an account -> match_pursuits_for_account with the
       meeting title as an additional keyword hint, so pursuits are linked even when
       no invitee email addresses appear in the Gemini doc.
    """
    from fieldkit.ingest.router import (
        match_pursuits_for_account,
        route_by_title,
        route_with_pursuits,
    )

    title = src.meeting_title or doc_content.doc_title or ""

    domains = [email.split("@")[-1].lower() for email in doc_content.invited_emails if "@" in email]

    route = route_with_pursuits(domains, keywords=[title] if title else None, data_root=data_root)

    if route.accounts == ["unknown"] and route.confidence.value == "none":
        route = route_by_title(title, data_root=data_root)

    # If we resolved an account, run pursuit matching with the title as a keyword hint.
    # route_with_pursuits only fires when confidence is HIGH (requires domain match);
    # match_pursuits_for_account works regardless of how the account was resolved.
    if route.accounts != ["unknown"]:
        account_name = primary_account(route)
        pursuits = match_pursuits_for_account(account_name, keywords=[title] if title else [], data_root=data_root)
        route = RouteResult(
            accounts=route.accounts,
            confidence=route.confidence,
            is_internal=route.is_internal,
            pursuits=pursuits,
        )

    return route


def clean_and_extract_transcript(doc_content: GeminiDocContent, report_warning: Callable[[str], None]) -> _CleanResult:
    """Clean and extract, propagating authentication and model-provider failures.

    Return cleaned text, metadata and explicit degradation state for run and reprocess.
    """
    from fieldkit.ingest.pipeline import Stage1Result, stage1_clean, stage2_extract

    raw_text = doc_content.transcript_text or doc_content.notes_text
    stage1_result: Stage1Result
    used_fallback = False
    try:
        stage1_result = stage1_clean(raw_text)
    except (AuthError, LLMError):
        raise
    except Exception:  # noqa: BLE001 — explicit degraded output, without provider payloads
        report_warning("Transcript cleaning failed; using uncleaned text. Review the degraded meeting output.")
        stage1_result = Stage1Result(text=raw_text, bypassed=True)
        used_fallback = True

    cleaned = stage1_result.text

    try:
        meta = stage2_extract(stage1_result)
    except (AuthError, LLMError):
        raise
    except Exception:  # noqa: BLE001 — explicit degraded output, without provider payloads
        report_warning(
            "Transcript extraction failed; using low-confidence metadata. Review the degraded meeting output."
        )
        meta = TranscriptMeta(confidence="low")
        used_fallback = True

    return _CleanResult(cleaned=cleaned, meta=meta, used_fallback=used_fallback)


def prepare_meeting(
    *,
    src: SourceRecord,
    doc_content: GeminiDocContent,
    data_root: Path,
    pipeline_version: str,
    report_warning: Callable[[str], None],
) -> PreparedMeeting:
    """Route, extract, classify, and bind one immutable output intent.

    Authentication and model-provider errors propagate. Other extraction
    failures report degradation through the caller's renderer. No output is published.
    """
    from fieldkit.ingest.pipeline import infer_meeting_date, render_vault_note

    source_id = src.source_id
    route = _route_source(src, doc_content, data_root=data_root)
    account = primary_account(route)

    transcript = clean_and_extract_transcript(doc_content, report_warning)
    cleaned = transcript.cleaned
    meta = transcript.meta
    meta.accounts = route.accounts

    # Re-run pursuit matching now that we have LLM-extracted topics and decisions,
    # which are richer signals than the meeting title alone.
    if account != "unknown":
        from fieldkit.ingest.router import match_pursuits_for_account

        topic_keywords = meta.key_topics + meta.key_decisions
        enriched_pursuits = match_pursuits_for_account(account, keywords=topic_keywords, data_root=data_root)
        # Merge: keep any title-matched pursuits, add topic-matched ones
        merged = list(dict.fromkeys(route.pursuits + enriched_pursuits))
        meta.pursuits = merged
    else:
        meta.pursuits = route.pursuits

    doc_url = f"https://docs.google.com/document/d/{source_id}"
    if src.meeting_date:
        meeting_date_str = src.meeting_date.strftime("%Y-%m-%d")
    else:
        # Try to parse meeting date from the title (Gemini format: "Topic - YYYY/MM/DD HH:MM TZ")
        _title_for_date = src.meeting_title or doc_content.doc_title or ""
        # None means the title carries no date; render_vault_note warns and uses today.
        meeting_date_str = infer_meeting_date(_title_for_date) or datetime.now(UTC).strftime("%Y-%m-%d")

    note_content = render_vault_note(
        doc_content=doc_content,
        route=route,
        meta=meta,
        cleaned_body=cleaned,
        pipeline_version=pipeline_version,
        doc_url=doc_url,
        meeting_date=meeting_date_str,
    )
    parse_meeting_frontmatter(note_content)
    vault_path = compute_vault_path(
        data_root=data_root,
        account=account,
        meeting_date=meeting_date_str,
        meeting_title=src.meeting_title or doc_content.doc_title or source_id,
        source_id=source_id,
    )

    meeting_title_str: str = src.meeting_title or doc_content.doc_title or source_id
    classified = classify_meeting_tasks(
        data_root=data_root,
        action_items=meta.action_items,
        pursuits=meta.pursuits,
        account=account,
        note_content=note_content,
    )
    return PreparedMeeting(
        schema_version=1,
        pipeline_id=GEMINI_TRANSCRIPT_PIPELINE,
        source_id=source_id,
        pipeline_version=pipeline_version,
        vault_relative_path=vault_path.relative_to(data_root).as_posix(),
        note_content=note_content,
        note_sha256=hashlib.sha256(note_content.encode("utf-8")).hexdigest(),
        account=account,
        meeting_date=meeting_date_str,
        meeting_title=meeting_title_str,
        pursuits=tuple(meta.pursuits),
        action_items=tuple(meta.action_items),
        tasks=tuple(
            PreparedTask(
                text=item.text,
                cls=item.cls.value,
                owner=item.owner,
                rationale=item.rationale,
                pursuit_label=item.pursuit_label,
            )
            for item in classified
        ),
        degraded=transcript.used_fallback or meta.confidence == "low",
    )
