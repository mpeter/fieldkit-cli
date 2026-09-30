---
name: task-sync
description: >
  Safely reconcile the marked TASKS.md region with the Google Tasks list named
  fieldkit. Use only when the operator requests task synchronization and can
  review separate remote and local write plans.
metadata:
  opencode/slash: "true"
  argument-hint: ""
  category: ops
---

# Task sync

This is an assisted reconciliation workflow. fieldkit does not ship a
one-command bidirectional sync. It ships preview-first commands for creating and
completing individual Google Tasks; this workflow combines those commands with
a bounded read through the separately installed `gws` CLI and a reviewed local
file update. Do not report a sync as automatic or complete unless every planned
write was confirmed and read back.

## Prerequisites and scope

Resolve `TASKS.md` at the configured fieldkit workspace root. Confirm that the
file is a regular file, not a symlink, and contains exactly one
`<!-- task-sync:start -->` marker followed by exactly one
`<!-- task-sync:end -->` marker. Never change content outside the two task-sync
markers. Within the managed region, require exactly one each of `## Today`,
`## Active`, and `## Done`; reject duplicate or malformed `gtask:` anchors.

`gws` is a separate prerequisite. Run `gws auth status` and verify that its
authenticated Google identity is the one the operator intends to use. fieldkit's
Google authentication is a different credential path and does not prove `gws`
is ready. If `gws` is absent, authentication is unclear, or the intended account
is ambiguous, stop without changing either side.

Resolve the task-list id from a complete bounded call to `gws tasks tasklists
list --page-all --page-limit 100 --params '{}'`. Require exactly one list whose
title is exactly `fieldkit`. Zero or multiple exact matches are an error; never
create or choose a list implicitly. Apply a caller-enforced 30-second process
timeout to each `gws` invocation; do not compose the command through a shell.

Google is authoritative only for the marked managed region and only after a
complete validated read. Waiting-on, queued items, and every other section outside
the markers are local and out of scope.

The `Backlog` section in TASKS.md is local and outside the sync markers.

## Build a trusted snapshot

Read the selected list with `gws tasks tasks list --page-all --page-limit 100
--params '{"tasklist":"<LIST_ID>","showCompleted":true,"showHidden":true}'`.
Parse every returned JSON page. Reject non-JSON output, malformed task objects,
duplicate task ids, repeated page tokens, nonzero exits, authentication errors,
and timeouts. If the last returned page still has a `nextPageToken`, the
100-page bound was reached: stop. A partial pull is not authoritative.

An empty result is suspect when the local managed region contains any
`gtask:` anchor. Stop unless the operator explicitly confirms that the remote
list was cleared. Never interpret a failed, partial, malformed, or suspect pull
as remote deletion.

Take a fresh snapshot of `TASKS.md` immediately before planning. If the file
changes before the local write, discard the plan and reconcile again.

## Plan without writing

Match records only by exact `<!-- gtask:<id> -->` identity. Titles are not
identity and are not safe deduplication keys.

- A remote open task with `section:today` or `section:active` in its notes maps
  to that managed section and retains its anchor. Google supplies its title,
  due date, section, and account metadata.
- A remote open task without supported section metadata needs an explicit local
  classification. Preserve the current local section for an existing anchor;
  for a new mobile task, ask whether it belongs in `Today` or `Active`. Record
  that the classification exists only in `TASKS.md`; do not claim Google stores
  it. If the operator does not classify it, leave the item unresolved and stop
  before regenerating local state.
- A remote completed task maps to managed `Done` only when its id already has a
  local anchor or was confirmed during this reconciliation. Retain its anchor
  and an explicit remote completion date when supplied. Do not invent a date or
  import unrelated completed history with no local anchor.
- A local anchorless item in `Today` or `Active` is a proposed remote create.
  Reject an unknown account slug or malformed due date rather than dropping it.
- A checked local anchored item whose remote record is still open is a proposed
  remote completion.
- A local anchor absent from a trusted remote snapshot is a proposed local
  removal. Show it explicitly; never delete it merely because it is absent.
- A local title, due-date, account, or section edit on an anchored item is not a
  supported outbound update. Google wins; show the remote value that would be
  restored and ask the operator to edit Google Tasks instead if that was not
  intended.

Render the complete remote plan and the complete local diff. Obtain separate
approval for remote and local writes. A general request to "sync" authorizes
the read and plan, not either mutation batch. Any removal, including one line,
must be visible in the local diff and explicitly approved.

## Apply approved remote writes

Use fieldkit's shipped preview-first mutation commands, constructed as argv
lists rather than shell-composed strings.

For each proposed create, run `fieldkit gtask create <title> --section <today-or-active>
--json`, adding `--account <slug>` and `--due YYYY-MM-DD` only when those values
are known and valid. Preview is the default. Verify that the JSON result has
`confirmed: false` and matches the plan. After approval, repeat the same argv
with `--confirm`; require `confirmed: true` and a nonempty returned task id.

For each proposed completion, run `fieldkit gtask complete <task-id> --json`.
Verify the preview, then repeat the same argv with `--confirm` only after
approval. Use only these fieldkit commands for remote writes; raw `gws` insert,
patch, and delete calls are outside this workflow.

Read back Google Tasks after every confirmed remote write using the same bounded
complete-list rules. Confirm every created id and completed status before using
it in the local diff. If a write result is unknown or read-back fails, stop,
preserve all local content, and report partial completion. Do not retry a create
whose outcome is unknown; first reconcile to avoid duplication.

## Apply the approved local diff

Re-read and revalidate the file and confirm it still matches the planning
snapshot. Incorporate returned ids as exact `gtask:` anchors, regenerate only
the content between the markers from the verified read-back, and preserve every
byte outside the markers. Do not remove `fieldkit-task:` provenance comments
from surviving items. Refuse ambiguous ownership rather than adopting or
overwriting it.

Write through a temporary file and atomic replacement in the same directory.
If safe atomic replacement or concurrent-change detection is unavailable, stop
and leave the local file unchanged. Read the resulting file back, revalidate
its markers, sections, and unique anchors, and compare it with the approved
diff.

## Report truthfully

Report remote creates and completions, local additions, updates and removals,
unresolved records, read-back status, and whether both sides match the approved
plan. Keep failures and partial outcomes explicit. A successful preview is not
a sync, a successful remote batch is not a successful local update, and stale
or mixed-revision evidence is non-passing. Do not retain authentication output,
raw task responses, titles, account names, or notes in shared diagnostics. When
evidence is requested, retain only bounded argv, timestamps, exit statuses,
counts, validation results, and approved artifact digests unless the operator
chooses a private destination for customer content.
