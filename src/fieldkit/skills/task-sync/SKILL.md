---
name: task-sync
description: >
  Reconcile the TASKS.md managed region with the Google Tasks "fieldkit" list.
  Google Tasks is the source of truth (operator ruling 2026-07-17): tasks the
  operator adds/edits/completes on mobile flow back into TASKS.md, and any item
  promoted into the managed region without an anchor gets created in the list.
  Trigger with /task-sync, "sync tasks", "sync my tasks", "pull my tasks",
  "Google Tasks sync", "tasks out of sync", or after the operator edited tasks
  on their phone. Runs via the `gws` CLI, never MCP.
metadata:
  opencode/slash: "true"
  argument-hint: ""
  category: ops
---

# /task-sync — reconcile TASKS.md ⇄ Google Tasks (Google wins)

**Working directory:** the fieldkit workspace root.
**Tooling:** `gws tasks …` (keyring-authenticated as {{email}}).
`gws` uses the live SSO keyring; no MCP gateway is required.

## The contract

- **List:** the one named `fieldkit`. Resolve its id at the start of a session:
  ```bash
  gws tasks tasklists list
  ```
  and reuse the id for every call below (shown as `<LIST_ID>`).
- **Source of truth:** the Google Tasks list. On any conflict, **Google wins**.
- **Identity:** each managed TASKS.md line carries `<!-- gtask:<id> -->`. Never
  hand-edit or delete an anchor — it's the permanent link.
- **Managed region** of TASKS.md: from `## Today · synced` down to the
  `BACKLOG` marker comment. Everything below the marker is unmanaged and this
  skill never touches it.
- **Section tag:** each task's Notes first line is `section:today` or
  `section:active` → controls which sub-block it lands in. Optional
  `account:<acct>` on the next line. Free-text notes follow.

## One reconcile pass

1. **Pull the list** (include completed so completions reflect):
   ```bash
   gws tasks tasks list --params \
     '{"tasklist":"<LIST_ID>","showCompleted":true,"showHidden":true}'
   ```
   **Validate the pull before trusting it (guards mass deletion).** The pull is
   the sole authority for what gets deleted in step 3, so a failed or empty pull
   must never be read as "the operator cleared everything":
   - If the command errored, timed out, returned non-JSON, or hit a reauth
     prompt → **STOP.** Do not regenerate, do not delete. Report the failure and
     that no changes were made; the operator re-runs after fixing `gws` auth.
   - Count the tasks returned. If the pull returns **zero** tasks while the
     managed region currently holds **one or more** `gtask:` anchors → treat the
     pull as **suspect, not authoritative.** A whole list vanishing at once is
     far more likely a bad pull than the operator deleting every task on mobile.
     STOP and report; do not delete. Only proceed with an empty pull if the
     managed region is already empty (nothing to lose) or the operator has
     explicitly confirmed they cleared the list.
## Push and regenerate

2. **Push promoted items first.** Scan the managed region for lines with **no
   `gtask:` anchor** (the operator moved them up from the backlog). For each,
   create it in the list, then write the returned id back as an anchor:
   ```bash
   gws tasks tasks insert --params '{"tasklist":"<LIST_ID>"}' \
     --json '{"title":"<text>","notes":"section:active\naccount:<acct>"}'
   ```
   Also push any managed line the operator checked `[x]` since last sync:
   ```bash
   gws tasks tasks patch --params '{"tasklist":"<LIST_ID>","task":"<id>"}' \
     --json '{"status":"completed"}'
   ```
3. **Regenerate the managed region from the pulled list** (Google wins):
   - task in list, `needsAction` → a `- [ ] <title> [— due <date>] <!-- gtask:id -->`
     line, filed under its `section:` sub-block.
   - task in list, `completed` → render `- [x]` and move it to `## Done Today`
     (carry the anchor), then drop from the managed region next pass.
   - anchor in TASKS.md but **absent from the list** → operator deleted it on
     mobile → remove the line. **Only if step 1's pull validated** (see the
     guard above): a suspect pull never deletes. And if this rule would remove
     **more than 3 anchored lines in one pass**, do not delete silently —
     list exactly which anchored tasks would be removed and get the operator's
     confirmation first. One or two disappearing is a normal mobile edit; a
     large batch disappearing is the failure signature.
   - list task with no matching anchor → operator added it on mobile → new
     managed line with its anchor.
   Sort each sub-block: due-dated first (soonest up), then the rest.
4. **Report** (3–6 lines): what came in from mobile (added/completed/deleted),
   what got pushed up, and the current managed count by section. Never dump the
   whole list.

## Task format

- **Frictionless-action bar (operator ruling 2026-07-18):** every task must
  carry enough information that the operator can take the next best action
  from the task alone — on mobile, with nothing else open. Title = the
  concrete next action; notes (after the `section:`/`account:` lines) = the
  state needed to act: what's owed, to whom, blocking what, relevant
  date/link. When promoting a bare backlog line, enrich it to this bar before
  creating it in the list.
- Multi-step items become a parent task with **sub-tasks** (create with
  `"parent":"<parent-id>"` in the insert params), each sub-task independently
  actionable.
- Account-scoped tasks keep their `[Account]` or `[Account / Pursuit]` title
  prefix on both sides; anything else renders locally as `**[Uncategorized]**`.
- The `<!-- gtask:ID -->` anchor is always at the end of the line, after all
  visible text.
- Tasks with sub-bullets: only the parent line syncs; sub-bullets are
  local-only context.
- Due dates are date-only in Google (`YYYY-MM-DDT00:00:00.000Z`); render as
  "Fri, Jul 18" style in TASKS.md.

## Guardrails

- `gws` only; scratch + the TASKS.md managed region are the only writes. Never
  the backlog region, never `notes/`, never `accounts/`, never a commit.
- **Never delete a Google task** as part of regeneration — completion is
  `status:completed`, not delete. Only the operator deletes tasks (on mobile or
  by explicit ask).
- **A failed or empty pull deletes nothing.** Deletions in the managed region
  are only valid downstream of a pull that validated in step 1. When in doubt,
  keep the line — a stale-but-present task costs a glance; a wrongly-deleted one
  loses the only local copy of its sub-bullet context. Google is source of
  truth, but only a *trusted read* of Google is.
- Anchors are immutable identity. If a title changed on both sides, Google's
  wins; keep the anchor.
- One list only (`fieldkit`). The operator's other Google Tasks lists are
  theirs — never read or write them here.
- Preserve section headers, HTML format-hint comments, and empty sections in
  TASKS.md exactly as-is.

## Cadence

On demand; at the top of `/brief` (so the morning sheet reflects mobile edits);
as the final step of `/brief`'s update op (so Google Tasks reflects the fully reconciled state,
not a stale snapshot); and at the close of the operator's end-of-day
handoff routine (so completions push up). Cheap enough to run whenever the operator has been on
their phone.
