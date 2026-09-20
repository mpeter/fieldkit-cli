# AGENTS.md — Ingest Pipeline (`ingest/`)

Two-stage LLM extraction pipeline for Gemini meeting transcripts and other ingest sources. Writes vault-compatible markdown meeting notes.

## Database Separation (D052)

**CONSTRAINT:** `pipeline.db` and `gmail.db` are **separate databases** with separate lifecycles and ownership. Never query one expecting data from the other.

- `pipeline.db` at `<fieldkit_data>/pipeline.db` — ingest state: pipelines, sources, artifacts, checkpoints.
- `gmail.db` at `<fieldkit_data>/gmail.db` — email cache: sync state, threads, messages.

The ingest pipeline does NOT process Gmail directly. Gmail has its own separate sync pipeline via `gmail_cache/`. The two databases share WAL mode and foreign keys but are otherwise independent.

## Three-Phase Processing and Resume

Processing stages: **discover → process → finalize**.

Resume is done via the `status` column in the `sources` table:
- `pending` — not yet processed; picked up on next run.
- `processed` — completed; skipped on resume.

`get_pending_sources()` naturally skips `status='processed'` rows — no separate checkpoint write needed. SIGINT-safe: committed rows act as durable checkpoints (P11).

## ArtifactRecord — Use the Typed Adapter

**GOTCHA:** `db.py` uses `conn.row_factory = sqlite3.Row` and returns `ArtifactRecord` dataclass instances from `get_artifacts_for_reprocess()`. Do NOT access `sqlite3.Row` attributes directly — use the typed `ArtifactRecord` dataclass to avoid mypy `attr-defined` errors.

```python
# WRONG — sqlite3.Row attribute access bypasses type checking
row["artifact_id"]

# CORRECT — use the typed dataclass returned by get_artifacts_for_reprocess()
record: ArtifactRecord = ...
record.artifact_id
```

## Schema Migration Pattern

`pipeline_version` column was added to `artifacts` via `ALTER TABLE` with a duplicate-column guard (`try/except sqlite3.OperationalError`) inside `init_db()`. Default value `'0.1.0'` for existing rows. This is the established pattern for schema evolution in this codebase — do not use `CREATE TABLE IF NOT EXISTS` for new columns.

## Two-Stage Extraction Architecture (D045)

- **Stage 1** (`stage1_clean`): Cleans raw transcript text — filler removal, paragraph restructuring, speaker labeling. Output is the vault note body. The Stage 1 prompt is fixed; do not modify it without understanding the downstream impact on vault note quality.
- **Stage 2** (`stage2_extract`): Separate extraction pass over Stage 1 output. Produces structured frontmatter fields (`participants`, `action_items`, `key_decisions`, `key_topics`, `confidence`). Stage 2 can be reprocessed independently without touching the transcript body.

When `NO_LLM=1`, Stage 1 returns the raw transcript unchanged; Stage 2 returns `TranscriptMeta(confidence="stub")` with empty lists. Layer 1 (deterministic routing, vault path computation) always runs.

## Gemini Doc Tab Structure (D046)

Every Gemini meeting doc has exactly **two tabs**: `Notes` (Gemini summary, decisions, next steps, invited list) and `Transcript` (raw timestamped content). The pipeline:
1. Extracts email domains from the Notes tab `Invited` field for account routing.
2. Extracts Gemini `Next Steps` checklist as a cross-check reference.
3. Discards the rest of the Notes tab prose (user distrusts Gemini summaries).
4. Feeds only the Transcript tab through Stage 1 cleaning.

**GOTCHA:** Significant divergence between Gemini Next Steps and Stage 2 `action_items` is flagged in the vault note body under `## Gemini Suggested Next Steps`. This is intentional — do not suppress it.

## Stage 1 Short-Transcript Guard (historic regression)

`stage1_clean()` returns the raw transcript unchanged when `len(stripped) < _MIN_TRANSCRIPT_CHARS` (currently 100). Short inputs (`"[silence]"`, empty meeting artifacts, test stubs) produce garbage LLM output and waste quota — the guard short-circuits before the prompt. Stage 2 then assigns `confidence='low'` for these. Do not lower the threshold without a concrete motivation.

## Deferred: SQLite Derived Index (D024)

A SQLite FTS/derived index for pursuit queries was deferred because the data model is actively evolving. Do not add one until the schema has stabilized post-M020. Queries during any future rebuild may return incomplete results.
