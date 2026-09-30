---
name: task-management
description: >
  Review and maintain the local TASKS.md file in the configured fieldkit
  workspace. Use for today's commitments, active work, waiting-on items,
  queued task review, and explicitly approved task edits.
metadata:
  category: ops
---

# Task management

Use this workflow to review or propose changes to the operator's local task
list. It does not refresh source systems, send messages, update pursuits, or
write Google Tasks by itself.

## Locate and validate the task file

`TASKS.md` belongs at the configured fieldkit workspace root. It belongs in
that workspace, not the fieldkit source checkout or runtime-data directory.
Resolve the configured fieldkit workspace root before reading or proposing a
write. If the workspace cannot be resolved unambiguously, stop and ask which
workspace is intended.

Treat a missing file as a setup choice, not permission to create one. Propose a
new UTF-8 file with the following elements in order: `# Tasks`, the exact
`<!-- task-sync:start -->` marker, `## Today`, `## Active`, `## Done`, the exact
`<!-- task-sync:end -->` marker, `## Waiting On`, and the local queued section. Show the
complete proposed file and obtain confirmation before creating it.

In TASKS.md, the local queued section is named `## Backlog`.

The two task-sync markers must each occur exactly once and in that order. When
Google Tasks reconciliation is in use, `Today`, `Active`, and `Done` are the
managed region. Waiting-on and queued items remain local. Do not add, remove, or
rewrite `<!-- gtask:<id> -->` or `<!-- fieldkit-task:... -->` identity comments.
If markers or identity comments are missing, duplicated, malformed, or
ambiguous, stop and ask for reconciliation instead of guessing.

## Read and summarize

For a review request, read the file without changing it. Present `Today`, then
`Active`, `Waiting On`, and queued items. Use only explicit dates to identify
overdue work. Unknown owners and due dates remain unknown. A subject line or
summary is not proof of a commitment; inspect the authorized underlying source
before attributing one.

Keep source freshness visible. Cached Gmail results are not a live mailbox,
workspace pursuit fields are not a fresh Salesforce read, and optional
integration data may be absent. Missing data is unavailable, not evidence that
no work exists.

## Propose a task change

Convert the request into one concrete action. Preserve the operator's wording
unless the documented task format requires normalization. Do not invent an
account, owner, deadline, or customer commitment. Account-scoped items use the
known account slug in a visible
`**[account-slug]**` prefix. Use an ISO date (`YYYY-MM-DD`) when a due or
follow-up date is known.

Use these local forms:

- open work: `- [ ] **[account-slug]** Concrete next action — due YYYY-MM-DD`
- waiting: `- **[account-slug]** Waiting on Person re: topic — sent YYYY-MM-DD, follow up by YYYY-MM-DD`
- completed: retain the original line and identity comment, change the checkbox
  to `[x]`, and add the observed completion date only when it is known

For a new item, propose `Today` only when the operator explicitly commits to it
today; otherwise propose `Active` or the local queued section. If task sync is enabled, an
anchorless item in `Today` or `Active` is a pending proposal for remote creation
on the next separately approved task-sync run. A checked anchored item is only
a staged completion until that run confirms it remotely.

Show the exact proposed diff, including any preserved identity comments. Do not
write until the operator approves that diff. Approval to edit `TASKS.md` does
not authorize a Google Tasks write, a pursuit edit, a Salesforce write, or an
external message. After a local write, read the file back and report only the
change that is present.

## Source-supported follow-ups

Meeting notes, cached email, pursuit records, and Salesforce reports can suggest
candidate tasks only after the relevant dated evidence has been inspected. Do
not infer qualification gaps, customer intent, or unanswered commitments from
a score, a missing field, a thread subject, or a summary alone. For meeting
action items with stable provenance, `fieldkit ingest promote` is the shipped
interactive local-write path; it still requires the operator to classify each
item and does not sync Google Tasks.

Morning and end-of-day reviews are ordinary read/propose cycles. Do not reset
sections, prune old work, write lessons, or move items merely because the clock
or calendar changed. Present the proposed changes and use the same approval and
read-back requirements.

## Completion and sync boundaries

Marking a task done records only task state. It does not prove that a message
was sent, a customer agreed, qualification changed, or another system was
updated. If a task yielded evidence for another workflow, offer that separate
workflow and obtain its own approval.

Use the [task-sync workflow](../task-sync/SKILL.md) only when the operator asks
to reconcile with Google Tasks. That workflow has separate prerequisites,
previews, approvals, and remote read-back requirements.
