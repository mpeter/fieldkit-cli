---
name: ingest
description: >
  New meeting recordings, transcripts, or Gemini notes have arrived and need to be processed
  into the vault; or the Gmail cache is stale and email signals need refreshing before account
  analysis. Runs the ingestion pipeline to discover, route, and archive unprocessed sources, or
  the Gmail sync → tag → enrich pipeline for gmail-intel.md files.
  Trigger with "ingest this recording", "process the transcript", "add this meeting",
  "PLAUD recording ready", "meeting notes to ingest", "ingest and route",
  "run the ingest pipeline", "refresh Gmail", "sync email", "Gmail cache is stale",
  "run Gmail sync", "gmail-refresh".
metadata:
  opencode/slash: "true"
  category: ops
---

# Ingest Pipeline

Operate `fieldkit ingest` — the source provenance and idempotent ingestion pipeline for
meeting transcripts, audio recordings, and Gemini meeting notes from Gmail/Drive.

All commands run from `<repo-root>/`.

## Folded Ops

- `ops/gmail-refresh.md` — Gmail sync → tag → enrich pipeline for gmail-intel.md files

---

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Pipeline IDs

The primary pipeline is `transcript-ingest`. Use this ID in all `--pipeline` arguments
unless you have registered a custom pipeline.

---

## Commands

### status — what's registered and queued

```bash
fieldkit ingest status
```

Shows: registered pipelines, source counts, artifact counts, last run info.
Run this first to understand the current state before doing anything else.

---

### discover — find new sources

Scans Gmail for Gemini meeting transcript emails and registers them in `pipeline.db`.

```bash
fieldkit ingest discover --pipeline transcript-ingest
fieldkit ingest discover --pipeline transcript-ingest --dry-run     # preview only
fieldkit ingest discover --pipeline transcript-ingest --limit 50    # cap scan volume
```

Run `discover` before `run` to ensure new sources are registered.

---

### run — process registered sources

Executes the pipeline on all pending sources (or up to `--limit`).

```bash
fieldkit ingest run --pipeline transcript-ingest
fieldkit ingest run --pipeline transcript-ingest --dry-run          # preview
fieldkit ingest run --pipeline transcript-ingest --limit 10         # batch
fieldkit ingest run --pipeline transcript-ingest --interactive      # confirm each
```

`--interactive` prompts y/n/q for each source — use when reviewing new sources
before committing to full pipeline execution.

---

### backfill — find orphaned vault files

Scans vault meeting notes for files that lack a `source_id` provenance stamp.
**Read-only** — never modifies files, only reports.

```bash
fieldkit ingest backfill
```

Use after bulk imports or migrations to find notes that aren't tracked in `pipeline.db`.

---

### reprocess — re-run an updated pipeline on existing artifacts

Re-runs artifacts produced by an older pipeline version through the current version.

```bash
fieldkit ingest reprocess --pipeline transcript-ingest
fieldkit ingest reprocess --pipeline transcript-ingest --from-version 1.0
fieldkit ingest reprocess --pipeline transcript-ingest --dry-run
```

Use after pipeline logic changes to update existing meeting notes without re-ingesting
from source.

---

## Standard Flow

```bash
# 1. Check current state
fieldkit ingest status

# 2. Discover new sources from Gmail
fieldkit ingest discover --pipeline transcript-ingest

# 3. Preview what would run
fieldkit ingest run --pipeline transcript-ingest --dry-run

# 4. Run the pipeline
fieldkit ingest run --pipeline transcript-ingest

# 5. Verify
fieldkit ingest status
```

---

## Output Artifacts

Pipeline artifacts are written to the vault:
- `accounts/<account>/meetings/YYYY-MM-DD-<slug>.md` — processed meeting notes
- `pipeline.db` — source registry and run history (path from `fieldkit init`)

---

## Related

- `fieldkit ingest status` — always run first; shows pipeline.db health at a glance
- `ops/gmail-refresh.md` — run this first if gmail-intel signals feeding a meeting brief are stale
