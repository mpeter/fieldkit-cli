---
name: post-meeting
description: >
  Turn one customer meeting's notes into a reviewed follow-up, action proposals,
  and a source-attributed meeting note. Offer optional native qualification review
  and separately approved writes; do not infer that a transcript belongs to the meeting.
metadata:
  opencode/slash: "true"
  argument-hint: "[account] [account/pursuit] [--from-notes 'text'] [--skip-email] [--skip-meddpicc] [--quick]"
  category: product
---

# After a meeting

Use this skill to capture what happened in one identified meeting and propose
follow-up work. The result is a reviewable summary and drafts; no customer email,
Salesforce field, Google Task, workbook, or workspace file changes merely because
the skill was invoked.

The arguments in the skill invocation are **not fieldkit CLI flags**.
`--from-notes` uses operator-provided notes instead of looking for a transcript.
`--skip-email` omits the email draft. `--skip-meddpicc` omits native qualification
review. `--quick` produces only the fact summary and task proposals, without
email, qualification review, or saved notes.

For email alone, use [follow-up draft](../followup-draft/SKILL.md). For meeting
preparation, use [meeting](../meeting/SKILL.md).

## Identify the meeting and source

Ask for the account, meeting date, and enough detail to distinguish this meeting
from others. If the operator did not provide them, an authorized calendar event
or an existing workspace meeting note can be a candidate, but confirm the exact
event and account before using its content. Do not select a file solely because
it was modified today. Resolve an ambiguous pursuit or Salesforce Opportunity
ID with the operator; never choose the first match.

Use the supplied notes, a confirmed existing transcript, or another source the
operator identifies. Preserve its title, date, and location in the private
working summary. The following can inspect account-scoped discovery when that
pipeline and authorization are available:

```console
fieldkit ingest discover --pipeline transcript-ingest --account ACCOUNT --dry-run
```

Replace `ACCOUNT` with the confirmed account. Discovery does not prove a result is
this meeting. `fieldkit ingest run` has no account filter and can process other
pending transcripts, so do not run it as an automatic post-meeting step. If
ingest is needed, show the selected source and its destination and obtain
separate approval before running it. The [ingest workflow](../ingest/SKILL.md#process-only-after-accepting-the-global-queue-boundary)
explains that its interactive source prompt does not preview every destination.
If the available workflow cannot provide the required selected-source and
destination preview, stop rather than run it. A failed or unavailable source
stays unavailable; do not claim it was ingested.

## Extract and review

From the confirmed source, list attendees, topics, each side's commitments,
owners and dates when stated, open questions, and agreed next steps. Distinguish
what the customer said from internal notes and your inference. Mark unknowns
unknown; do not invent a deadline or treat an unsent draft as a commitment.
Present the summary with source and date, and ask for corrections before making
any downstream proposal. If the source changed during the workflow, reread it
before relying on the old extraction.

## Prepare separate proposals

- Unless email is skipped, draft a subject and body grounded in the reviewed
  facts. Show recipient identity, commitments, and wording for review. Do not
  send. An authenticated external Gmail draft is optional and requires separate
  approval, verified account and recipient, and read-back of the created draft;
  otherwise leave it as an on-screen draft. `gws` is not bundled with fieldkit.
- Propose actionable tasks from our commitments and follow-up dates for customer
  commitments. Identify the intended workspace `TASKS.md` and show the exact
  additions before asking to write it. Do not create the file or edit its
  managed Google Tasks region by default. [Task sync](../task-sync/SKILL.md) is
  a separate Google reconciliation with its own account, read, and write checks;
  run it only when explicitly requested and approved.
- Unless qualification review is skipped, if the pursuit has an exact
  Salesforce Opportunity ID and live read is authorized, run `fieldkit sf
  meddpicc OPP_ID --json`. For multiple deals, require selection of an exact
  deal ID and a new read. Missing, incomplete, or failed reads are unavailable.
  Associate meeting evidence only with exact native ClosePlan question IDs
  returned by a complete selected deal; list unmatched signals separately.
  Offer `/grill` for read-only review. Do not write qualification state, local
  scores, or stage-gate outcomes.
- A proposed Salesforce Next Steps edit is separate from qualification evidence.
  Follow the [next-step protocol](../tool-routing/references/sf-next-steps-protocol.md)
  for a live current-value read, preview, explicit operator-controlled write,
  and independent read-back. If that reference is absent from a selective skill
  installation, leave the proposal pending.

## Save only approved destinations

Unless in quick mode, show the structured meeting note and its proposed path
under the confirmed account's `meetings/` directory. Include the date, source,
attendees, topics, commitments, open questions, and next steps. Include
qualification *signals* only as unscored evidence with exact native question
IDs when known. Ask before creating or changing a file; an existing note needs
explicit overwrite approval. Check the configured workspace root, not the code
checkout.

If a linked Pursuit Workbook and exact pursuit file are available, offer a
separate Google Docs write. `fieldkit meeting note PURSUIT_FILE --title TITLE
--content CONTENT` creates a workbook tab; it does not save the local meeting
note. Require approval of the content and target first, then verify the result.
Unavailable credentials or a missing workbook leave this step pending, not
successful.

Finish with a concise ledger: source used; reviewed draft; each local or
external destination marked proposed, written-and-verified, skipped, pending,
or unavailable. Do not collapse a partial workflow into success.
