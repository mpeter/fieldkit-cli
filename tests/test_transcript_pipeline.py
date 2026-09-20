"""Unit tests for lib/transcript_pipeline.py.

Strategy:
- stage1_clean / stage2_extract tested under NO_LLM=1 (stub path).
- render_vault_note exercised with inline GeminiDocContent + RouteResult fixtures.
- compute_vault_path tested for correct path construction and slugification.
- All tests are fully offline — no network, no real LLM, no real Drive API.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.pipeline import (
    _MAX_TRANSCRIPT_CHARS,
    Stage1Result,
    TranscriptMeta,
    _slugify,
    compute_vault_path,
    infer_meeting_date,
    render_vault_note,
    stage1_clean,
    stage2_extract,
)
from fieldkit.ingest.router import Confidence, RouteResult
from fieldkit.llm import _NO_LLM_STUB

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_doc_content(
    *,
    doc_id: str = "DOC001",
    doc_title: str = "Q2 Strategy Call June 5, 2026",
    notes_text: str = "Invited: alice@globalpay.example.com\nSuggested next steps\n[ ] Follow up\n",
    transcript_text: str | None = "Alice: Let's talk strategy.\nBob: Agreed.",
    tab_count: int = 2,
) -> GeminiDocContent:
    """Build a minimal GeminiDocContent for testing."""
    from fieldkit.ingest.docs import parse_invited_emails, parse_next_steps

    return GeminiDocContent(
        doc_id=doc_id,
        doc_title=doc_title,
        notes_text=notes_text,
        transcript_text=transcript_text,
        invited_emails=parse_invited_emails(notes_text),
        gemini_next_steps=parse_next_steps(notes_text),
        tab_count=tab_count,
    )


def _make_route(
    accounts: list[str] | None = None,
    confidence: Confidence = Confidence.HIGH,
    pursuits: list[str] | None = None,
) -> RouteResult:
    return RouteResult(
        accounts=accounts or ["globalpay"],
        confidence=confidence,
        is_internal=False,
        pursuits=pursuits or [],
    )


# ---------------------------------------------------------------------------
# stage1_clean
# ---------------------------------------------------------------------------


_LONG_TRANSCRIPT = "Alice: Let's talk strategy.\nBob: Agreed on the roadmap.\n" * 5  # 290+ chars


# ── TestStage1Clean (flattened) ─────────────────────────────────────────────


def test_stage1_clean_no_llm_returns_input_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """With NO_LLM=1, stage1_clean returns the raw transcript unchanged (bypassed=False)."""
    monkeypatch.setenv("NO_LLM", "1")
    result = stage1_clean(_LONG_TRANSCRIPT)
    assert isinstance(result, Stage1Result)
    assert result.text == _LONG_TRANSCRIPT
    assert result.bypassed is False  # stub path is NOT a bypass


def test_stage1_clean_no_llm_stub_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub string from synthesize() triggers passthrough, not the stub itself."""
    monkeypatch.setenv("NO_LLM", "1")
    result = stage1_clean(_LONG_TRANSCRIPT)
    # Must NOT return the stub string
    assert result.text != _NO_LLM_STUB
    # Must return the original input
    assert result.text == _LONG_TRANSCRIPT


def test_stage1_clean_empty_transcript_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: empty transcript returns as-is without calling LLM (bypassed=True)."""
    monkeypatch.setenv("NO_LLM", "1")
    result = stage1_clean("")
    assert isinstance(result, Stage1Result)
    assert result.text == ""  # passed through unchanged, no LLM call
    assert result.bypassed is True  # short-circuit bypass


def test_stage1_clean_short_transcript_99_chars_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: transcript of 99 chars returns as-is without calling LLM (bypassed=True)."""
    monkeypatch.setenv("NO_LLM", "1")
    short = "a" * 99
    result = stage1_clean(short)
    assert isinstance(result, Stage1Result)
    assert result.text == short  # passed through unchanged
    assert result.bypassed is True


def test_stage1_clean_transcript_100_chars_calls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: transcript of exactly 100 chars passes the guard and calls synthesize."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    fake_output = "Cleaned transcript."
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=fake_output) as mock_synth:
        result = stage1_clean("a" * 100)
    mock_synth.assert_called_once()
    assert isinstance(result, Stage1Result)
    assert result.text == fake_output
    assert result.bypassed is False


def test_stage1_clean_real_synthesize_result_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """When synthesize returns real (non-stub) text, that text is returned."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    fake_output = "Alice: Let's discuss Q2 strategy.\nBob: Agreed on the roadmap."
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=fake_output):
        result = stage1_clean(_LONG_TRANSCRIPT)
    assert isinstance(result, Stage1Result)
    assert result.text == fake_output
    assert result.bypassed is False


def test_stage1_clean_stage1_clean_truncates_oversized_transcript(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Transcript exceeding _MAX_TRANSCRIPT_CHARS is truncated and a warning is logged."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    fake_output = "Cleaned transcript after truncation."
    oversized = "x" * (_MAX_TRANSCRIPT_CHARS + 100)
    with (
        caplog.at_level(logging.WARNING),
        patch("fieldkit.ingest.pipeline.synthesize", return_value=fake_output) as mock_synth,
    ):
        result = stage1_clean(oversized)
    # Warning must mention truncation
    assert any("truncated" in record.message for record in caplog.records), (
        "Expected a warning containing 'truncated' but none was logged"
    )
    # synthesize was called once (truncation happened)
    assert mock_synth.call_count == 1
    assert isinstance(result, Stage1Result)
    # historic regression: truncated transcript must set bypassed=True so stage2 caps confidence
    assert result.bypassed is True


@pytest.mark.parametrize("length", [_MAX_TRANSCRIPT_CHARS - 1])
def test_stage1_clean_stage1_clean_passthrough_under_transcript_ceiling(
    length: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Transcript just under _MAX_TRANSCRIPT_CHARS is processed without truncation warning."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    fake_output = "Cleaned transcript without truncation."
    under_ceiling = "x" * length
    with (
        caplog.at_level(logging.WARNING),
        patch("fieldkit.ingest.pipeline.synthesize", return_value=fake_output) as mock_synth,
    ):
        result = stage1_clean(under_ceiling)
    # No truncation warning should be logged
    truncation_warnings = [r for r in caplog.records if "truncated" in r.message]
    assert truncation_warnings == [], f"Unexpected truncation warning(s) for length={length}: {truncation_warnings}"
    # synthesize was called once (transcript processed normally)
    assert mock_synth.call_count == 1
    assert isinstance(result, Stage1Result)
    assert result.bypassed is False


# ---------------------------------------------------------------------------
# stage2_extract
# ---------------------------------------------------------------------------


# ── TestStage2Extract (flattened) ───────────────────────────────────────────


def test_stage2_extract_no_llm_returns_stub_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """With NO_LLM=1, returns TranscriptMeta with confidence='stub' and empty lists."""
    monkeypatch.setenv("NO_LLM", "1")
    meta = stage2_extract("Some cleaned transcript text.")
    assert isinstance(meta, TranscriptMeta)
    assert meta.confidence == "stub"
    assert meta.participants == []
    assert meta.action_items == []
    assert meta.key_decisions == []
    assert meta.key_topics == []


def test_stage2_extract_valid_json_parsed_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid JSON response is parsed into TranscriptMeta fields."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)  # pii-guard: ignore
    payload = {
        "participants": ["Alice (Globalpay)", "Bob (Example Vendor)"],
        "action_items": ["Send the deck by Friday", "Schedule follow-up"],
        "key_decisions": ["Proceed with Phase 2"],
        "key_topics": ["OpenShift", "Roadmap"],
        "confidence": "high",
    }
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=json.dumps(payload)):
        meta = stage2_extract("cleaned text")  # pii-guard: ignore

    assert meta.participants == ["Alice (Globalpay)", "Bob (Example Vendor)"]
    assert meta.action_items == ["Send the deck by Friday", "Schedule follow-up"]
    assert meta.key_decisions == ["Proceed with Phase 2"]
    assert meta.key_topics == ["OpenShift", "Roadmap"]
    assert meta.confidence == "high"


def test_stage2_extract_json_in_code_fence_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON wrapped in ```json ... ``` fences is parsed correctly."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    payload = {
        "participants": ["Carol"],
        "action_items": [],
        "key_decisions": [],
        "key_topics": ["AAP"],
        "confidence": "medium",
    }
    wrapped = f"```json\n{json.dumps(payload)}\n```"
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=wrapped):
        meta = stage2_extract("some text")

    assert meta.participants == ["Carol"]
    assert meta.confidence == "medium"


def test_stage2_extract_malformed_json_returns_empty_meta_no_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed JSON from synthesize logs a warning and returns default TranscriptMeta."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    with patch("fieldkit.ingest.pipeline.synthesize", return_value="not json at all {{{"):
        meta = stage2_extract("some text")

    # No exception raised
    assert isinstance(meta, TranscriptMeta)
    assert meta.participants == []
    assert meta.action_items == []
    # confidence set to 'low' on parse failure
    assert meta.confidence == "low"


def test_stage2_extract_unknown_confidence_defaults_to_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unrecognised confidence value coerced to 'low'."""
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    payload = {
        "participants": [],
        "action_items": [],
        "key_decisions": [],
        "key_topics": [],
        "confidence": "very-high-definitely",
    }
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=json.dumps(payload)):
        meta = stage2_extract("some text")

    assert meta.confidence == "low"


def test_stage2_extract_bypassed_stage1_runs_extraction_caps_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: bypassed Stage1Result must run extraction and cap confidence at 'low'.

    Previously stage2_extract() returned empty TranscriptMeta(confidence='low')
    immediately when bypassed=True, silently discarding all content.
    """
    monkeypatch.delenv("NO_LLM", raising=False)
    payload = {
        "participants": ["Alice", "Bob"],
        "action_items": ["Send follow-up"],
        "key_decisions": ["Approved budget"],
        "key_topics": ["Q3 plan"],
        "confidence": "high",  # LLM returns high, but bypassed must cap to low
    }
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=json.dumps(payload)):
        meta = stage2_extract(Stage1Result(text="Short notes.", bypassed=True))

    # Confidence must be capped at 'low' — bypassed input was too short to clean
    assert meta.confidence == "low"
    # But extraction MUST have run — fields must NOT be empty
    assert meta.participants == ["Alice", "Bob"], "historic regression: participants silently discarded"
    assert meta.action_items == ["Send follow-up"], "historic regression: action_items silently discarded"
    assert meta.key_decisions == ["Approved budget"], "historic regression: key_decisions silently discarded"
    assert meta.key_topics == ["Q3 plan"], "historic regression: key_topics silently discarded"


# ---------------------------------------------------------------------------
# render_vault_note
# ---------------------------------------------------------------------------


# ── TestRenderVaultNote (flattened) ─────────────────────────────────────────


def _render_vault_note_render(
    *,
    doc_id: str = "DOC001",
    doc_title: str = "Q2 Strategy Call June 5, 2026",
    notes_text: str = "Invited: alice@globalpay.example.com\n",
    transcript_text: str | None = "Alice: Hello.",
    accounts: list[str] | None = None,
    pursuits: list[str] | None = None,
    participants: list[str] | None = None,
    action_items: list[str] | None = None,
    key_decisions: list[str] | None = None,
    key_topics: list[str] | None = None,
    confidence: str = "high",
    pipeline_version: str = "0.1.0",
    doc_url: str | None = None,
    gemini_next_steps: list[str] | None = None,
) -> str:
    from fieldkit.ingest.docs import parse_invited_emails, parse_next_steps

    doc = GeminiDocContent(
        doc_id=doc_id,
        doc_title=doc_title,
        notes_text=notes_text,
        transcript_text=transcript_text,
        invited_emails=parse_invited_emails(notes_text),
        gemini_next_steps=gemini_next_steps or parse_next_steps(notes_text),
        tab_count=2 if transcript_text else 1,
    )
    route = _make_route(
        accounts=accounts or ["globalpay"],
        pursuits=pursuits or [],
    )  # pii-guard: ignore
    meta = TranscriptMeta(
        participants=participants or ["Alice (Globalpay)", "Bob (Example Vendor)"],
        action_items=action_items or ["Send follow-up deck"],
        key_decisions=key_decisions or ["Proceed with Phase 2"],
        key_topics=key_topics or ["OpenShift"],
        confidence=confidence,
        accounts=accounts or ["globalpay"],
        pursuits=pursuits or [],
    )
    return render_vault_note(
        doc_content=doc,
        route=route,
        meta=meta,
        cleaned_body=transcript_text or "",
        pipeline_version=pipeline_version,
        doc_url=doc_url,
    )


def test_render_vault_note_frontmatter_starts_and_ends_with_dashes() -> None:
    output = _render_vault_note_render()
    lines = output.splitlines()
    assert lines[0] == "---"
    # Find closing ---
    closing_idx = next(i for i, ln in enumerate(lines[1:], 1) if ln == "---")
    assert closing_idx > 0


def test_render_vault_note_required_frontmatter_fields_present() -> None:
    output = _render_vault_note_render(
        doc_id="DOCXYZ",
        pipeline_version="0.2.0",
    )
    assert "source_id: DOCXYZ" in output
    assert "pipeline: transcript-ingest" in output  # pii-guard: ignore
    assert "pipeline_version: 0.2.0" in output  # pii-guard: ignore
    assert "account: globalpay" in output
    assert "source_quality: full" in output
    assert "confidence: high" in output
    assert "meeting_date:" in output
    assert "meeting_title:" in output
    assert "gemini_doc_url:" in output


def test_render_vault_note_source_quality_full_when_transcript_present() -> None:
    output = _render_vault_note_render(transcript_text="Some transcript.")
    assert "source_quality: full" in output


def test_render_vault_note_source_quality_summary_only_when_no_transcript() -> None:
    output = _render_vault_note_render(transcript_text=None)
    assert "source_quality: summary_only" in output


def test_render_vault_note_accounts_list_in_frontmatter() -> None:
    output = _render_vault_note_render(accounts=["globalpay", "acme-bank"])
    assert "accounts:" in output  # pii-guard: ignore
    assert "- globalpay" in output
    assert "- acme-bank" in output


# pii-guard: ignore
def test_render_vault_note_pursuits_list_in_frontmatter() -> None:  # pii-guard: ignore
    output = _render_vault_note_render(pursuits=["globalpay-modernization"])
    assert "- globalpay-modernization" in output


def test_render_vault_note_action_items_in_frontmatter() -> None:
    output = _render_vault_note_render(action_items=["Task A", "Task B"])
    assert "action_items:" in output
    assert "- Task A" in output


def test_render_vault_note_key_decisions_in_frontmatter() -> None:
    output = _render_vault_note_render(key_decisions=["Go with OpenShift"])
    assert "key_decisions:" in output
    assert "- Go with OpenShift" in output


def test_render_vault_note_body_contains_title_heading() -> None:
    output = _render_vault_note_render(doc_title="My Meeting June 5, 2026")
    assert "# My Meeting June 5, 2026" in output


def test_render_vault_note_body_contains_source_link() -> None:
    output = _render_vault_note_render(
        doc_id="DOC001",
        doc_url="https://docs.google.com/document/d/DOC001",
    )
    assert "[Google Doc](https://docs.google.com/document/d/DOC001)" in output


def test_render_vault_note_body_uses_doc_id_when_no_doc_url() -> None:
    output = _render_vault_note_render(doc_id="MYID", doc_url=None)
    assert "https://docs.google.com/document/d/MYID" in output


def test_render_vault_note_body_contains_transcript_section() -> None:
    output = _render_vault_note_render(transcript_text="Cleaned text here.")
    assert "## Transcript" in output
    assert "Cleaned text here." in output


def test_render_vault_note_body_fallback_when_no_content() -> None:
    output = _render_vault_note_render(transcript_text=None)
    assert "_No transcript or notes content available._" in output


def test_render_vault_note_action_items_in_body() -> None:
    output = _render_vault_note_render(action_items=["Do the thing", "Follow up"])
    assert "## Action Items" in output
    assert "- [ ] Do the thing" in output
    assert "- [ ] Follow up" in output


def test_render_vault_note_gemini_next_steps_shown_when_diverged() -> None:
    """Gemini steps appear when they differ significantly from extracted actions."""
    output = _render_vault_note_render(
        action_items=["Completely unrelated task A", "Different task B"],
        gemini_next_steps=["Schedule quarterly review with executives"],
    )
    # Divergence check fires → section appears
    assert "Gemini Suggested Next Steps" in output
    assert "Schedule quarterly review with executives" in output


def test_render_vault_note_gemini_next_steps_hidden_when_overlap() -> None:
    """Gemini steps NOT shown when they match extracted action items."""
    # Force significant overlap so _diverges returns False
    common = ["schedule quarterly review", "send the summary report to team"]
    output = _render_vault_note_render(
        action_items=common,
        gemini_next_steps=common,
    )
    assert "Gemini Suggested Next Steps" not in output


def test_render_vault_note_valid_yaml_frontmatter() -> None:
    """Frontmatter is parseable as YAML."""
    import yaml  # type: ignore[import-untyped]

    output = _render_vault_note_render()
    # Extract frontmatter block
    lines = output.splitlines()
    end_idx = next(i for i, ln in enumerate(lines[1:], 1) if ln == "---")
    fm_text = "\n".join(lines[1:end_idx])
    parsed = yaml.safe_load(fm_text)
    assert isinstance(parsed, dict)
    assert "source_id" in parsed
    assert "pipeline" in parsed


# ---------------------------------------------------------------------------
# compute_vault_path
# ---------------------------------------------------------------------------


# ── TestComputeVaultPath (flattened) ────────────────────────────────────────


def test_compute_vault_path_basic_path(tmp_path: Path) -> None:
    result = compute_vault_path(
        data_root=tmp_path,
        account="globalpay",
        meeting_date="2026-06-05",
        meeting_title="Q2 Strategy Call",
    )
    assert result == tmp_path / "accounts" / "globalpay" / "meetings" / "2026-06-05-q2-strategy-call.md"


def test_compute_vault_path_slug_strips_special_chars(tmp_path: Path) -> None:  # pii-guard: ignore
    result = compute_vault_path(
        data_root=tmp_path,
        account="acme-bank",
        meeting_date="2026-01-15",
        meeting_title="Kick-Off: Phase 2 — Planning!",
    )
    filename = result.name
    # Should be slugified: lowercase, hyphens, no colons/dashes/exclamation
    assert filename.startswith("2026-01-15-")
    assert ":" not in filename
    assert "!" not in filename
    assert filename == filename.lower()


def test_compute_vault_path_slug_spaces_become_hyphens(tmp_path: Path) -> None:
    result = compute_vault_path(
        data_root=tmp_path,
        account="globalpay",
        meeting_date="2026-03-01",
        meeting_title="weekly team sync",
    )
    assert result.name == "2026-03-01-weekly-team-sync.md"


def test_compute_vault_path_path_is_under_accounts_meetings(tmp_path: Path) -> None:  # pii-guard: ignore
    result = compute_vault_path(
        data_root=tmp_path,
        account="midwest-ins",
        meeting_date="2026-04-22",
        meeting_title="AAP Workshop",
    )  # pii-guard: ignore
    # Path structure: <data_root>/accounts/<account>/meetings/<file>.md
    assert result.parts[-4] == "accounts"
    assert result.parts[-3] == "midwest-ins"
    assert result.parts[-2] == "meetings"
    assert result.suffix == ".md"


def test_compute_vault_path_account_preserved_in_path(tmp_path: Path) -> None:
    result = compute_vault_path(
        data_root=tmp_path,
        account="my-account",
        meeting_date="2026-05-10",
        meeting_title="Demo",
    )
    assert "my-account" in str(result)


def test_compute_vault_path_unknown_account_allowed(tmp_path: Path) -> None:
    """compute_vault_path does not validate account existence."""
    result = compute_vault_path(
        data_root=tmp_path,
        account="unknown",
        meeting_date="2026-07-01",
        meeting_title="Mystery Meeting",
    )
    assert "unknown" in str(result)


# ---------------------------------------------------------------------------
# _slugify helper
# ---------------------------------------------------------------------------


# ── TestSlugify (flattened) ─────────────────────────────────────────────────


def test_slugify_lowercases() -> None:
    assert _slugify("Hello World") == "hello-world"


def test_slugify_strips_special_chars() -> None:
    assert _slugify("Q2 Review!") == "q2-review"


def test_slugify_collapses_hyphens() -> None:
    assert _slugify("hello---world") == "hello-world"


def test_slugify_leading_trailing_stripped() -> None:
    assert _slugify("  hello  ") == "hello"


def test_slugify_colon_removed() -> None:
    result = _slugify("Kick-Off: Phase 2")
    assert ":" not in result


def test_slugify_empty_string() -> None:
    assert _slugify("") == ""


# ---------------------------------------------------------------------------
# historic regression: _is_internal() uses email domain when name is empty in config.yaml
# ---------------------------------------------------------------------------


# ── Internal email-domain fallback regression (flattened) ──────────────────


def _internal_email_domain_fallback_render_with_config(
    tmp_path: Path,
    config_yaml: str,
    participants: list[str],
) -> str:
    """Write a config.yaml, then call render_vault_note with given participants."""
    from unittest.mock import patch

    from fieldkit.ingest.docs import GeminiDocContent
    from fieldkit.ingest.pipeline import TranscriptMeta, render_vault_note
    from fieldkit.ingest.router import Confidence, RouteResult

    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    doc = GeminiDocContent(
        doc_id="DOC-TEST-038",
        doc_title="Internal domain fallback test meeting",
        notes_text="",
        transcript_text="",
        invited_emails=[],
        gemini_next_steps=[],
        tab_count=1,
    )
    route = RouteResult(
        accounts=["acme-corp"],
        confidence=Confidence.HIGH,
        is_internal=False,
        pursuits=[],
    )
    meta = TranscriptMeta(
        participants=participants,
        action_items=[],
        key_decisions=[],
        key_topics=[],
        confidence="high",
        accounts=["acme-corp"],
        pursuits=[],
    )

    # Patch CONFIG_PATH in lib.config (where it is defined and read at call time
    # via the local import inside _build_frontmatter).
    with patch("fieldkit.config._loader.CONFIG_PATH", config_path):
        return render_vault_note(
            doc_content=doc,
            route=route,
            meta=meta,
            cleaned_body="",
            pipeline_version="0.1.0",
        )


def test_internal_email_domain_fallback_classifies_internal_when_name_empty(
    tmp_path: Path,
) -> None:
    """When name is empty but email domain matches config email, participant is classified as internal.

    Uses your-org.example.com (approved fixture domain) to test the _user_email_domain
    code path independently of the hardcoded domain / company-name checks.
    """
    # your-org.example.com is an approved test fixture domain (AGENTS.md R23)
    config_yaml = 'email: user@your-org.example.com\nname: ""\nfieldkit_home: /tmp/data\n'
    # Participant whose email address contains the configured domain
    participants = ["user@your-org.example.com", "alice@acme-corp.example.com"]

    output = _internal_email_domain_fallback_render_with_config(tmp_path, config_yaml, participants)

    assert "user@your-org.example.com" in output
    # The internal participant must appear in attendees_internal, not attendees_external
    assert "attendees_internal:" in output
    lines = output.splitlines()
    rh_section_start = next(i for i, ln in enumerate(lines) if "attendees_internal:" in ln)
    customer_section_start = next(i for i, ln in enumerate(lines) if "attendees_external:" in ln)
    # Collect items in attendees_internal block (lines between the two markers)
    rh_items = [ln.strip() for ln in lines[rh_section_start + 1 : customer_section_start] if ln.strip().startswith("-")]
    assert any("your-org.example.com" in item for item in rh_items), (
        "user@your-org.example.com must be in attendees_internal when email domain matches config (historic regression)"
    )


def test_internal_email_domain_requires_an_exact_domain_match(tmp_path: Path) -> None:
    config_yaml = 'email: user@your-org.example.com\nname: ""\nfieldkit_home: /tmp/data\n'
    participant = "person@your-org.example.com.external.org"

    output = _internal_email_domain_fallback_render_with_config(tmp_path, config_yaml, [participant])

    lines = output.splitlines()
    internal_start = next(i for i, line in enumerate(lines) if "attendees_internal:" in line)
    external_start = next(i for i, line in enumerate(lines) if "attendees_external:" in line)
    internal_items = lines[internal_start + 1 : external_start]
    external_items = lines[external_start + 1 :]
    assert not any(participant in item for item in internal_items)
    assert any(participant in item for item in external_items)


def test_internal_email_domain_fallback_is_not_applied_when_name_present(tmp_path: Path) -> None:
    """When name is set, name-based matching still works (no regression)."""
    # your-org.example.com is an approved test fixture domain (AGENTS.md R23)
    config_yaml = "email: user@your-org.example.com\nname: Alice Johnson\nfieldkit_home: /tmp/data\n"
    participants = ["Alice Johnson", "bob@acme-corp.example.com"]

    output = _internal_email_domain_fallback_render_with_config(tmp_path, config_yaml, participants)

    # Alice Johnson must be in attendees_internal (matched by name parts)
    lines = output.splitlines()
    rh_section_start = next(i for i, ln in enumerate(lines) if "attendees_internal:" in ln)
    customer_section_start = next(i for i, ln in enumerate(lines) if "attendees_external:" in ln)
    rh_items = [ln.strip() for ln in lines[rh_section_start + 1 : customer_section_start] if ln.strip().startswith("-")]
    assert any("Alice Johnson" in item for item in rh_items), (
        "Alice Johnson must be in attendees_internal when name is configured"
    )


def test_bug577_is_internal_no_substring_false_positive_on_shared_surname(tmp_path: Path) -> None:
    """A customer sharing a surname with the configured user must not be classified RH.

    Regression for historic regression: previously _is_internal() matched on any word-token
    substring, so configured user "Al Smith" caused participant "Walter Smith"
    (a customer, unrelated to the user) to be misclassified as attendees_internal.
    """
    config_yaml = "email: user@your-org.example.com\nname: Al Smith\nfieldkit_home: /tmp/data\n"
    participants = ["Walter Smith", "bob@acme-corp.example.com"]

    output = _internal_email_domain_fallback_render_with_config(tmp_path, config_yaml, participants)

    lines = output.splitlines()
    rh_section_start = next(i for i, ln in enumerate(lines) if "attendees_internal:" in ln)
    customer_section_start = next(i for i, ln in enumerate(lines) if "attendees_external:" in ln)
    rh_items = [ln.strip() for ln in lines[rh_section_start + 1 : customer_section_start] if ln.strip().startswith("-")]
    assert not any("Walter Smith" in item for item in rh_items), (
        "Walter Smith must NOT be in attendees_internal — shared surname 'Smith' with configured user 'Al Smith' "
        "must not cause a false-positive RH classification (historic regression)"
    )


def test_internal_email_domain_fallback_without_config_does_not_raise(tmp_path: Path) -> None:
    """When config.yaml does not exist, render_vault_note completes without error."""
    from unittest.mock import patch

    from fieldkit.ingest.docs import GeminiDocContent
    from fieldkit.ingest.pipeline import TranscriptMeta, render_vault_note
    from fieldkit.ingest.router import Confidence, RouteResult

    nonexistent = tmp_path / "nonexistent_config.yaml"
    doc = GeminiDocContent(
        doc_id="DOC-NOCONFIG",
        doc_title="No Config Test",
        notes_text="",
        transcript_text="",
        invited_emails=[],
        gemini_next_steps=[],
        tab_count=1,
    )
    route = RouteResult(
        accounts=["acme-corp"],
        confidence=Confidence.HIGH,
        is_internal=False,
        pursuits=[],
    )
    meta = TranscriptMeta(
        participants=["alice@acme-corp.example.com"],
        action_items=[],
        key_decisions=[],
        key_topics=[],
        confidence="high",
    )

    with patch("fieldkit.config._loader.CONFIG_PATH", nonexistent):
        output = render_vault_note(
            doc_content=doc,
            route=route,
            meta=meta,
            cleaned_body="",
            pipeline_version="0.1.0",
        )

    # Must produce valid output even without config
    assert "---" in output
    assert "pipeline: transcript-ingest" in output


def test_bug578_iso_date_in_title_is_parsed_not_ignored() -> None:
    """This title was historic regression's example of an *unparseable* one — and it carries a date.

    The original test asserted "Weekly Sync 2026-06-23" fell back to today, because the
    pipeline-side parser only knew "Month DD, YYYY". That the fixture author reached for
    a title containing an obvious date, and encoded ignoring it as correct, is the
    clearest statement of historic regression available.
    """
    assert infer_meeting_date("Weekly Sync 2026-06-23") == "2026-06-23"


def test_bug578_long_form_title_still_parses() -> None:
    assert infer_meeting_date("Kickoff Meeting - June 23, 2026") == "2026-06-23"


# ---------------------------------------------------------------------------
# historic regression: a reprocessed note must keep its own meeting date
# ---------------------------------------------------------------------------


def test_render_vault_note_infers_gemini_title_date_not_today() -> None:
    """The reprocess path passes no meeting_date, so inference must get it right.

    `ingest reprocess` calls render_vault_note without meeting_date. Before this fix
    the inference knew only "Month DD, YYYY" while real Gemini titles carry
    "YYYY/MM/DD", so every reprocessed note was re-stamped with the reprocess date —
    silently overwriting the true meeting date on disk, unrecoverably after the first
    run.
    """
    output = _render_vault_note_render(doc_title="Acme Delivery Sync - 2026/03/04 10:00 PST")

    assert "meeting_date: 2026-03-04" in output
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert f"meeting_date: {today}" not in output or today == "2026-03-04", (
        "a title carrying a real date must never yield today's date"
    )


def test_render_vault_note_explicit_meeting_date_wins_over_title() -> None:
    """A caller that knows the date (ingest run, from the Gmail source) is authoritative."""
    from fieldkit.ingest.docs import parse_invited_emails

    doc = GeminiDocContent(
        doc_id="DOC900",
        doc_title="Acme Sync - 2026/03/04 10:00 PST",
        notes_text="Invited: alice@globalpay.example.com\n",
        transcript_text="Alice: Hello.",
        invited_emails=parse_invited_emails("Invited: alice@globalpay.example.com\n"),
    )
    output = render_vault_note(
        doc_content=doc,
        route=_make_route(accounts=["globalpay"], pursuits=[]),  # pii-guard: ignore
        meta=TranscriptMeta(confidence="high", accounts=["globalpay"], pursuits=[]),
        cleaned_body="Alice: Hello.",
        pipeline_version="0.1.0",
        meeting_date="2025-12-25",
    )

    assert "meeting_date: 2025-12-25" in output


def test_render_vault_note_warns_only_when_title_truly_has_no_date(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """historic regression's warning must not fire for a title the parser can now read.

    A warning that fires on every reprocessed note would be noise; it should mark the
    genuinely undatable case only.
    """
    with caplog.at_level(logging.WARNING, logger="fieldkit.ingest.pipeline"):
        dated = _render_vault_note_render(doc_title="Acme Sync - 2026/03/04 10:00 PST")

    assert "meeting_date: 2026-03-04" in dated
    assert not [r for r in caplog.records if "could not infer meeting date" in r.getMessage()]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="fieldkit.ingest.pipeline"):
        _render_vault_note_render(doc_title="Weekly Standup")

    assert [r for r in caplog.records if "could not infer meeting date" in r.getMessage()], (
        "an undatable title must still warn — that is historic regression"
    )
