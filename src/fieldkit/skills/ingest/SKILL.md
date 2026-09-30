---
name: ingest
description: >
  Register and process Google Docs meeting notes discovered in the ready managed
  Gmail cache, or refresh Gmail-derived reports for one confirmed account. Use for
  transcript ingest, provenance backfill, Gmail refresh, and interrupted-ingest
  recovery. Transcript execution is approval-gated because its pending queue is
  not account-scoped.
metadata:
  opencode/slash: "true"
  category: ops
---

# Ingest meeting notes

Use the installed `fieldkit` command with the intended workspace and runtime-data
roots configured. A source checkout is not required.

This skill covers two separate jobs:

- register and process Google Docs meeting notes discovered in the local Gmail
  cache; and
- refresh Gmail-derived account reports through
  [the Gmail refresh workflow](ops/gmail-refresh.md).

Do not treat a recording, the newest message, or a similar meeting title as
proof of account ownership. Confirm the account and source with the operator.

## Inspect one account without writing

Start with the global registry counts and an account-scoped discovery preview:

```console
fieldkit ingest status --account <account> --json
fieldkit ingest discover --pipeline transcript-ingest --dry-run --account <account> --json
```

The status command validates the account slug, but its counts remain global.
The reported account filter is not evidence that those counts belong only to
the selected account. The discovery dry-run, unlike status, scopes its candidate
preview to the account.

Discovery reads the ready committed Gmail cache generation; it does not fetch
live Gmail or adopt a legacy database. The scoped dry-run reports candidates
without registering them. If the cache is missing, not ready, stale, or
unmanaged, pause and use the Gmail refresh workflow. That workflow includes the
explicit bounded sync and legacy-import paths; do not repair cache state from
this transcript flow.

Show the returned candidate source identifiers and subjects. The preview does
not return source dates; do not invent them. Resolve an empty or
ambiguous result with the operator instead of selecting a likely match.

## Register globally only after approval

Account-only registration is unavailable. Live discovery registers candidates
globally, even when `--account` is supplied. Preview the global bounded scan:

```console
fieldkit ingest discover --pipeline transcript-ingest --dry-run --limit <N> --json
```

Registration changes the runtime `pipeline.db` and can add other accounts'
sources. State that global scope, the previewed candidates, and the positive
matching-message result limit, then obtain explicit approval before running:

```console
fieldkit ingest discover --pipeline transcript-ingest --limit <N> --json
```

Use the same approved positive limit for the preview and registration. The
limit caps matching messages returned; it does not cap all rows examined or
select an exact source. A changing cache can change the candidates, so review
the returned sources before processing anything.

Discovery also enforces an independent SQLite work bound. If that bound is
exhausted, it exits non-zero before registering sources; the failed preview is
not a complete inventory or an empty-result finding. Stop rather than raising
limits automatically or processing an older queue.

## Process only after accepting the global queue boundary

`fieldkit ingest run` does not accept `--account`. Before prompting, it scans the
local Gmail cache without an account filter and may register additional sources.
Its pending queue can therefore contain other accounts.

Never continue from a generic “ingest this transcript” request into an automatic
batch. Explain the global registry boundary and obtain explicit approval to
review the queue interactively. Then check Google authorization and run:

Before execution, explain that transcript cleaning and extraction normally
send transcript content to the configured LLM provider. Verify that provider's
configuration, authorization and approved data scope separately; Google doctor
does not establish LLM readiness. The supported `FIELDKIT_NO_LLM=1` path avoids
those LLM calls: cleaning passes through the input and extraction returns empty
lists with stub confidence. It is not equivalent to a completed semantic
extraction. Obtain approval for the chosen mode and its effects before running.

```console
fieldkit doctor google --json
fieldkit ingest run --pipeline transcript-ingest --interactive
```

The Google check may refresh an expired token. Continue only when it exits 0.
At each ingest prompt, accept only the confirmed source and reject unrelated or
ambiguous entries. Do not use the non-interactive run command as a substitute
for account scoping.

The prompt identifies the source, not the eventual account, pursuit or task
destinations. Accepting it authorizes preparation and subsequent routing; it
does not mechanically confine effects to the account named in the request.
Explain that distinction before approval. If the operator requires exact
destination approval before any effect, stop: this interactive command does
not provide that destination preview.

A transcript run can write a meeting note, pursuit activity, and `TASKS.md`
effects in the routed account. Prepared output and ownership comments support
recovery across partial writes. Preserve those records, the database, its
sidecars, and backups.

For `transcript-ingest`, `ingest run --dry-run` previews pending and newly
discovered sources without registering them or initializing the registry.
It reads the managed Gmail cache and any existing registry through their
read-only paths; it does not process transcripts or establish permission to
execute the previewed work. Its preview remains global, not account-scoped.

## Treat every non-zero result as incomplete

Preserve the exact command, exit status, and sanitized JSON output. A busy lock,
interruption, setup error, or source failure is not success. Do not skip to a
later source or claim the batch completed.

Retry the same non-dry-run pipeline to replay retained prepared output. Replayed
effects are checked before writing again, but previously completed effects may
remain after a later effect fails. Reconcile operator edits or ownership
conflicts; do not delete recovery records to force progress.

## Audit missing provenance safely

Backfill is read-only and can be scoped to one confirmed account:

```console
fieldkit ingest backfill --account <account> --json
```

It reports meeting notes without a `source_id`; it does not repair them.

## Reprocess only as an explicit maintenance operation

Reprocessing replaces existing notes and can refetch the original Google Doc.
Require a backup, an account scope, an intentional `--from-version` or `--force`
selector, a preview, and separate approval for the live run. Retained
replacement journals must be reconciled rather than discarded. Initial-ingest
recovery belongs to `ingest run`; replacement recovery belongs to `ingest
reprocess`.

## Report the result

Name the confirmed account, every source reviewed, each accepted or rejected
source, written paths reported by the command, exact exit status, and any
remaining pending or recovery state. Do not summarize a partial result as a
successful ingest.
