# AGENTS.md — Ingest Pipeline (`ingest/`)

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
