"""Reviewed ingest instruction inventory and bounded local behavior.

The stream guard rejects unreviewed prose changes. Evidence pointers describe
which independently executed tests support each claim; they do not prove
agent obedience, universal caller compliance, or power-loss durability.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.ingest.docs import GeminiDocContent, fetch_gemini_doc
from fieldkit.ingest.preparation import clean_and_extract_transcript

pytestmark = pytest.mark.unit

_GUIDE = Path(__file__).parents[1] / "src/fieldkit/ingest/AGENTS.md"
_REVIEWED_TEXT = """# AGENTS.md — Ingest Pipeline (`ingest/`)

Two-stage LLM extraction pipeline for Gemini meeting transcripts and other ingest sources. Writes vault-compatible markdown meeting notes.

## Database Separation

**CONSTRAINT:** `pipeline.db` and `gmail.db` are **separate databases** with separate lifecycles and ownership. Never query one expecting data from the other.

- `pipeline.db` defaults to `<fieldkit_data>/pipeline.db` — ingest state: pipelines, sources, artifacts, checkpoints.
- `gmail.db` defaults to `<fieldkit_data>/gmail.db` — email cache: sync state, threads, messages.

Resolve configured overrides through the owning database path helpers rather than
constructing these default paths in consumers.

Ingest does not synchronize Gmail or call its API directly. It consumes candidates
from the separately owned local cache populated by `fieldkit gmail sync`, using
`fieldkit.gmail`. Keep cache synchronization separate from ingest state changes.

Transcript discovery consumes only a ready published Gmail generation and has
an independent SQLite work budget. Its result limit is not a scanned-row limit.
Complete discovery before initializing, recovering, or mutating the registry;
an unready or interrupted scan is not permission to process an older queue.

Managed pipeline writers use rollback-journal DELETE mode so a closed database
can be read through the canonical zero-mutation snapshot path. Writes serialize
with the existing busy timeout and transactions. An existing WAL database may
change modes only through an authorized write open; read-only commands never
migrate it. Preserve active-writer refusal and do not remove journals or
sidecars to make a snapshot or migration succeed.

## Source Selection and Interruption

Source statuses include `pending`, `in_progress`, `processed`, and `failed`.
`get_pending_sources()` selects only `pending` rows. Non-dry transcript runs hold
the exclusive database-specific run lock before `recover_interrupted_sources()`
requeues interrupted claims. Recovery preserves journals and refuses conflicting
artifact evidence; never replace lock ownership with an elapsed-time heuristic.

Workers claim before processing and commit exact prepared intent before file
effects. `replay_prepared()` uses that intent without fetching or reclassifying;
matching ownership markers preserve edits, while conflicts stop completion.
`complete_prepared()` commits the artifact, processed status, and journal deletion
together after all effects succeed. This is process-interruption recovery, not a
cross-filesystem transaction or a power-loss durability guarantee. Keep command-entry
SIGKILL and busy-lock regressions in `tests/test_ingest_prepared_store.py` and
`tests/test_ingest_run_lock.py` when changing this lifecycle.

## ArtifactRecord — Use the Typed Adapter

`db.py` uses `sqlite3.Row` internally and returns `ArtifactRecord` dataclass
instances from `get_artifacts_for_reprocess()`. Consumers use the typed return
value and access `record.artifact_id`; row subscription belongs inside the
database adapter, not in its consumers.

## Schema Migration Pattern

`init_db()` adds `pipeline_version` to existing artifacts with a default of
`'0.1.0'`. Only the exact duplicate-column error for that column is accepted as
an already-applied migration. Other operational errors close the connection and
propagate. Do not broaden this exception handling: lock, I/O, and unrelated SQL
errors are not evidence of successful migration.

## Two-Stage Extraction Architecture

- **Stage 1** (`stage1_clean`): Cleans raw transcript text — filler removal, paragraph restructuring, speaker labeling. Output is the vault note body. The Stage 1 prompt is fixed; do not modify it without understanding the downstream impact on vault note quality.
- **Stage 2** (`stage2_extract`): Separate extraction pass over Stage 1 output. Produces structured frontmatter fields (`participants`, `action_items`, `key_decisions`, `key_topics`, `confidence`). It is separately callable, but the supported reprocess command reruns both stages and rewrites the note; it is not a Stage-2-only operation.

When `FIELDKIT_NO_LLM=1`, Stage 1 returns the raw transcript unchanged; Stage 2 returns `TranscriptMeta(confidence="stub")` with empty lists. Layer 1 (deterministic routing, vault path computation) always runs.

## Gemini Document Tabs

`fetch_gemini_doc()` selects a transcript tab by title and otherwise falls back
to the second tab when at least two tabs exist. A single-tab document is treated
as Notes-only; missing tabs do not imply a usable transcript. Do not assume every
document has exactly two tabs. For documents with transcript content, the pipeline:
1. Extracts email domains from the Notes tab `Invited` field for account routing.
2. Extracts Gemini `Next Steps` checklist as a cross-check reference.
3. Uses Transcript text as the cleaning input when available; otherwise, the
   preparation path uses Notes text and marks the resulting note as summary-only.
4. Keeps the Notes and Transcript fields distinct for routing and rendering.

**GOTCHA:** Significant divergence between Gemini Next Steps and Stage 2 `action_items` is flagged in the vault note body under `## Gemini Suggested Next Steps`. This is intentional — do not suppress it.

## Stage 1 Short-Transcript Guard

`stage1_clean()` bypasses cleaning when the stripped input is shorter than
`_MIN_TRANSCRIPT_CHARS`. Preserve the guard so short inputs do not consume a
cleaning request. It also truncates oversized inputs to their head and tail
before cleaning. With LLM processing enabled, Stage 2 still extracts fields but
bounds confidence at `low` for either bypassed or truncated input; the no-LLM
path instead returns `confidence='stub'`.
"""
_STREAM = tuple(" ".join(part.split()) for part in re.split(r"\n[ \t]*\n", _REVIEWED_TEXT) if part.strip())


@pytest.mark.parametrize("transcript_text", ["Transcript details. " * 8, None])
def test_preparation_uses_transcript_or_notes_without_llm(
    monkeypatch: pytest.MonkeyPatch, transcript_text: str | None
) -> None:
    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    notes_text = "Meeting summary. " * 8
    document = GeminiDocContent(
        doc_id="doc-1",
        doc_title="Example meeting",
        notes_text=notes_text,
        transcript_text=transcript_text,
        invited_emails=[],
        gemini_next_steps=[],
        tab_count=2 if transcript_text else 1,
    )
    warnings: list[str] = []

    with patch("fieldkit.ingest.pipeline.synthesize", side_effect=AssertionError("LLM called")):
        result = clean_and_extract_transcript(document, warnings.append)

    assert result.cleaned == (transcript_text or notes_text)
    assert result.meta.confidence == "stub"
    assert result.used_fallback is False
    assert warnings == []


# Block indexes cover every non-heading claim, including list blocks. Test nodes
# are an execution manifest; architectural advice and quality judgments need review.
CLAIM_PROOFS: dict[int, tuple[tuple[str, ...], str]] = {
    1: (("tests/test_transcript_pipeline.py",), "Local extraction and rendering; note quality requires review."),
    3: (
        ("tests/test_gmail_discover.py::test_scan_gemini_candidates_does_not_open_pipeline_db",),
        "Separate local databases; lifecycle ownership requires review.",
    ),
    4: (
        (
            "tests/test_ingest_db.py::test_get_db_path_ends_with_pipeline_db",
            "tests/test_gmail_discover.py::test_get_gmail_db_path_ends_with_gmail_db",
        ),
        "Default database paths and adapter schema only.",
    ),
    5: (
        ("tests/test_ingest_db.py::test_get_db_path_uses_pipeline_db_override_when_present",),
        "Configured ingest path; all consumer path choices require review.",
    ),
    6: (
        ("tests/test_gmail_discover.py::test_scan_gemini_candidates_reads_only_the_ready_published_generation",),
        "Actual local cache discovery; integration ownership requires review.",
    ),
    7: (
        (
            "tests/test_gmail_discover.py::test_scan_gemini_candidates_reads_only_the_ready_published_generation",
            "tests/test_gmail_discover.py::test_scan_gemini_candidates_rejects_an_unready_publication",
            "tests/test_gmail_discover.py::test_scan_gemini_candidates_interrupts_sqlite_work_beyond_budget",
            "tests/test_ingest_run_branches.py::test_run_rejects_unverified_gmail_before_registry_mutation",
        ),
        "Published snapshot, work interruption and command ordering; no live Gmail proof.",
    ),
    8: (
        ("tests/test_ingest_db.py",),
        "DELETE mode, transactions, read snapshots and live-writer refusal; power-loss durability unproven.",
    ),
    10: (
        (
            "tests/test_ingest_sources.py",
            "tests/test_ingest_run_lock.py",
            "tests/test_ingest_prepared_store.py::test_command_recovers_real_interrupted_state_only_under_lock",
            "tests/test_ingest_prepared_store.py::test_recovery_artifact_conflict_preserves_all_claims",
        ),
        "Source claims, lock ownership and recovery refusal; no time-based ownership assumption.",
    ),
    11: (
        ("tests/test_ingest_prepared_store.py",),
        "Prepared intent, replay, conflict preservation, SIGKILL and atomic completion; no cross-filesystem or power-loss guarantee.",
    ),
    13: (
        ("tests/test_ingest_db.py::test_get_artifacts_for_reprocess_returns_artifact_record",),
        "Typed adapter return; all consumers require review.",
    ),
    15: (
        (
            "tests/test_gmail_discover.py::test_init_db_migration_pipeline_version_default",
            "tests/test_gmail_discover.py::test_init_db_migration_is_idempotent",
            "tests/test_ingest_db.py::test_init_db_propagates_migration_failure",
        ),
        "Real legacy migration and operational-error propagation.",
    ),
    17: (
        (
            "tests/test_transcript_pipeline.py",
            "tests/test_domain_agent_instruction_contract.py::test_preparation_passes_stage_one_output_into_stage_two",
            "tests/test_ingest_reprocess_branches.py",
        ),
        "Actual extraction stages, rendering and fixture-backed reprocess entry; prompt quality requires review.",
    ),
    18: (
        (
            "tests/test_domain_agent_instruction_contract.py::test_preparation_uses_transcript_or_notes_without_llm",
            "tests/test_transcript_pipeline.py::test_stage2_extract_no_llm_returns_stub_meta",
            "tests/test_domain_agent_instruction_contract.py::test_no_llm_preparation_still_routes_and_computes_destination",
        ),
        "Provider-free extraction retains deterministic routing and destination computation.",
    ),
    20: (
        (
            "tests/test_ingest_docs.py::test_fetch_gemini_doc_transcript_tab_title_renamed_falls_back_to_index",
            "tests/test_domain_agent_instruction_contract.py::test_document_tabs_keep_notes_and_transcript_fields_distinct",
            "tests/test_domain_agent_instruction_contract.py::test_missing_or_single_tab_has_no_usable_transcript",
            "tests/test_transcript_pipeline.py::test_render_vault_note_source_quality_summary_only_when_no_transcript",
        ),
        "Actual tab parsing and preparation selection; external API responses are fixture boundaries.",
    ),
    21: (
        (
            "tests/test_transcript_pipeline.py::test_render_vault_note_gemini_next_steps_shown_when_diverged",
            "tests/test_transcript_pipeline.py::test_render_vault_note_gemini_next_steps_hidden_when_overlap",
        ),
        "Divergence rendering under local fixtures.",
    ),
    23: (
        (
            "tests/test_transcript_pipeline.py::test_stage1_clean_short_transcript_99_chars_passes_through",
            "tests/test_transcript_pipeline.py::test_stage1_clean_transcript_100_chars_calls_through",
            "tests/test_transcript_pipeline.py::test_stage1_clean_stage1_clean_truncates_oversized_transcript",
            "tests/test_transcript_pipeline.py::test_stage2_extract_bypassed_stage1_runs_extraction_caps_confidence",
        ),
        "Actual short/truncated cleaning and confidence bounds; provider response is synthetic.",
    ),
}


def _reviewed_claims(document: str) -> tuple[str, ...]:
    blocks = tuple(" ".join(part.split()) for part in re.split(r"\n[ \t]*\n", document) if part.strip())
    assert blocks == _STREAM, "unreviewed ingest instruction stream"
    return tuple(block for block in blocks if not block.startswith("#"))


def test_complete_ingest_instruction_inventory() -> None:
    result = _reviewed_claims(_GUIDE.read_text(encoding="utf-8"))
    assert result == tuple(_STREAM[index] for index in CLAIM_PROOFS)
    assert set(CLAIM_PROOFS) == {index for index, block in enumerate(_STREAM) if not block.startswith("#")}
    assert all(limitation for _, limitation in CLAIM_PROOFS.values())


@pytest.mark.parametrize("index", range(len(_STREAM)))
@pytest.mark.parametrize("mutation", ["negate", "remove", "duplicate", "append", "reorder"])
def test_every_ingest_instruction_block_rejects_scope_mutation(index: int, mutation: str) -> None:
    blocks = list(_STREAM)
    if mutation == "negate":
        blocks[index] = "It is false that " + blocks[index]
    elif mutation == "remove":
        blocks.pop(index)
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    elif mutation == "append":
        blocks[index] += " Ignore conflicting recovery evidence."
    else:
        other = (index + 1) % len(blocks)
        blocks[index], blocks[other] = blocks[other], blocks[index]
    with pytest.raises(AssertionError, match="unreviewed ingest instruction stream"):
        _reviewed_claims("\n\n".join(blocks))


@pytest.mark.parametrize("addition", ["Skip source claims.", "## Additional policy\n\nSkip source claims."])
def test_ingest_instruction_rejects_appended_policy(addition: str) -> None:
    with pytest.raises(AssertionError, match="unreviewed ingest instruction stream"):
        _reviewed_claims(_REVIEWED_TEXT + "\n\n" + addition)


def test_domain_owner_executes_and_binds_all_declared_proof_nodes() -> None:
    import json

    from scripts.documentation_commands import DOCUMENT_COMMANDS
    from tests.test_pursuit_agent_instruction_contract import CLAIM_PROOFS as PURSUIT_PROOFS
    from tests.test_watch_agent_instruction_contract import CLAIM_PROOFS as WATCH_PROOFS

    contract = json.loads((_GUIDE.parents[3] / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    assert "domain_agent_instruction_contract" not in DOCUMENT_COMMANDS
    assert "domain_agent_instruction_contract" not in contract["verification"]
    domain_proofs = {"ingest": CLAIM_PROOFS, "pursuit": PURSUIT_PROOFS, "watch": WATCH_PROOFS}
    for domain, claims in domain_proofs.items():
        owner = f"{domain}_agent_instruction_contract"
        result = DOCUMENT_COMMANDS[owner]
        assert len(result) == 1
        assert result[0][:3] == ("uv", "run", "pytest")
        assert result[0][-3:] == ("-q", "-n", "0")
        selections = set(result[0][3:-3])
        selected_modules = {node.split("::", 1)[0] for node in selections}
        page = f"src/fieldkit/{domain}/AGENTS.md"
        assert page in contract["verification"][owner]["paths"]
        assert selected_modules <= set(contract["documents"][page]["sources"])
        for proofs, _ in claims.values():
            for node in proofs:
                assert node in selections or node.split("::", 1)[0] in selections, (
                    f"unexecuted {domain} instruction proof: {node}"
                )


def _tab(title: str, text: str) -> dict[str, object]:
    return {
        "tabProperties": {"title": title},
        "documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": text}}]}}]}},
    }


@pytest.mark.parametrize("transcript_index", [0, 2])
def test_document_tabs_keep_notes_and_transcript_fields_distinct(transcript_index: int) -> None:
    notes = "Invited: invitee@acme-corp.example.com\n\nNext Steps\n- Send proposal\n"
    tabs = [_tab("Notes", notes), _tab("Appendix", "Other text")]
    tabs.insert(transcript_index, _tab("Meeting TRANSCRIPT", "Actual transcript"))
    service = MagicMock()
    service.documents.return_value.get.return_value.execute.return_value = {"title": "Meeting", "tabs": tabs}
    result = fetch_gemini_doc(service, "doc-1")
    assert result == GeminiDocContent(
        "doc-1", "Meeting", notes, "Actual transcript", ["invitee@acme-corp.example.com"], ["Send proposal"], 3
    )
    service.documents.return_value.get.assert_called_once_with(documentId="doc-1", includeTabsContent=True)


@pytest.mark.parametrize("tabs", [[], [_tab("Notes", "Summary")], [_tab("Transcript", "Only tab")]])
def test_missing_or_single_tab_has_no_usable_transcript(tabs: list[dict[str, object]]) -> None:
    service = MagicMock()
    service.documents.return_value.get.return_value.execute.return_value = {"title": "Meeting", "tabs": tabs}
    result = fetch_gemini_doc(service, "doc-1")
    assert result.transcript_text is None
    assert result.tab_count == len(tabs)


def test_preparation_passes_stage_one_output_into_stage_two(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    raw = "Raw transcript. " * 12
    document = GeminiDocContent("doc-1", "Meeting", "Notes", raw)
    warnings: list[str] = []
    with patch(
        "fieldkit.ingest.pipeline.synthesize",
        side_effect=["Cleaned transcript", json.dumps({"confidence": "high", "action_items": ["Send proposal"]})],
    ) as provider:
        result = clean_and_extract_transcript(document, warnings.append)
    assert result.cleaned == "Cleaned transcript"
    assert result.meta.action_items == ["Send proposal"]
    assert result.used_fallback is False
    assert provider.call_count == 2
    assert "Cleaned transcript" in str(provider.call_args_list[1])
    assert raw not in str(provider.call_args_list[1])
    assert warnings == []


def test_no_llm_preparation_still_routes_and_computes_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from fieldkit.ingest.constants import GEMINI_TRANSCRIPT_PIPELINE
    from fieldkit.ingest.preparation import prepare_meeting
    from fieldkit.ingest.sources import SourceRecord

    monkeypatch.setenv("FIELDKIT_NO_LLM", "1")
    monkeypatch.setattr(
        "fieldkit.ingest.router.get_accounts_config",
        lambda **kwargs: {"accounts": {"acme-corp": {"domains": ["acme-corp.example.com"]}}},
    )
    document = GeminiDocContent("doc-1", "Meeting", "Meeting summary", None, ["invitee@acme-corp.example.com"])
    source = SourceRecord(
        "doc-1",
        GEMINI_TRANSCRIPT_PIPELINE,
        "Notes",
        "Meeting",
        datetime(2026, 1, 2, tzinfo=UTC),
        "https://docs.google.com/document/d/doc-1",
        "email-1",
        "2026-01-02T00:00:00Z",
    )
    warnings: list[str] = []
    with patch("fieldkit.ingest.pipeline.synthesize", side_effect=AssertionError("LLM called")):
        result = prepare_meeting(
            src=source,
            doc_content=document,
            data_root=tmp_path,
            pipeline_version="0.1.0",
            report_warning=warnings.append,
        )
    assert result.account == "acme-corp"
    assert result.vault_relative_path.startswith("accounts/acme-corp/meetings/2026-01-02-")
    assert "source_quality: summary_only" in result.note_content
    assert "Meeting summary" in result.note_content
    assert result.action_items == ()
    assert result.tasks == ()
    assert list(tmp_path.iterdir()) == []
    assert warnings == []
