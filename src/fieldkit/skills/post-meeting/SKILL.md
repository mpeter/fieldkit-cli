---
name: post-meeting
description: >
  Single entry point for everything that needs to happen after a customer meeting:
  ingest transcript, extract key facts, draft a follow-up email, stage evidence against
  exact native ClosePlan questions without writing qualification state, create task items,
  and save meeting notes. Shares context across all steps so nothing gets re-entered.
  Trigger with "post-meeting", "after the meeting", "wrap up the call",
  "post-call workflow", "/post-meeting [account]", "follow up on the call with [account]",
  "debrief [account]", or "capture the meeting with [account]".
metadata:
  opencode/slash: "true"
  argument-hint: "[account] [account/pursuit] [--from-notes 'text'] [--skip-email] [--skip-meddpicc] [--quick]"
  category: product
---

# Post-Meeting Workflow

Complete post-meeting capture in one guided pass. The AE provides context once;
this skill handles the rest: ingest → extract → email draft → native qualification
evidence staging → tasks → meeting note.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Do not advance deal stages without running the gate check first**
- **Always surface generated output for review before any external send**

## Flags

- `[account]` — scope to a specific account (e.g. `/post-meeting <account-slug>`)
- `[account/pursuit]` — scope to a specific pursuit (e.g. `/post-meeting <account-slug>/<pursuit-slug>`)
- `--from-notes 'text'` — skip ingest, use pasted notes as context
- `--skip-email` — skip follow-up email draft (internal sync, no customer follow-up needed)
- `--skip-meddpicc` — skip native qualification evidence staging (no new deal evidence)
- `--quick` — extract + tasks only (no email draft, qualification review, GDoc, or meeting note save)

CLI routes needed: `gws gmail` (create Gmail draft), `gws calendar` (resolve meeting event),
and `gws docs` (optional — append to GDoc Pursuit Workbook). Pursuit files and
meeting notes are read and written directly on disk (native file reads/writes).

---

## Step 0: Resolve context

Determine account and pursuit. If not provided, check:
1. Most recent calendar event today with external attendees from a configured account domain
2. Most recently modified meeting file under `accounts/*/meetings/` (today's date)
3. If neither: prompt "Which account / meeting are you capturing?"

Confirm: "Using context from [source]. Meeting: [title], [date]. Is this correct? (yes/no)"

Load shared fact object (carried through all steps):
```
{
  account: str,
  pursuit_file: Path | None,
  meeting_title: str,
  meeting_date: str,
  attendees: list,
  source: "calendar" | "file" | "pasted" | "unknown"
}
```

---

## Step 1: Ingest transcript (if available)

```bash
fieldkit ingest discover --pipeline transcript-ingest
fieldkit ingest run --pipeline transcript-ingest --limit 5
```

Check `<fieldkit_home>/accounts/<account>/meetings/` for a file created today.
If found: read it as primary context. Update fact object with `ingest_artifact_path`.
Report: "Transcript ingested: [title]." or "No new transcript found — using [source]."

Skip if `--from-notes` provided.

---

## Step 2: Extract meeting facts

From transcript, notes, or pasted text, extract and populate the fact object:

```
attendees:      [name, title, company for each]
topics:         [3–5 bullet topics discussed]
their_commits:  [what the customer agreed to do, by when]
our_commits:    [what we promised, by when]
open_questions: [unresolved items]
next_steps:     [specific agreed next meeting / action / date]
meddpicc_signals: {
  metrics: "evidence text or null",
  economic-buyer: "evidence text or null",
  decision-criteria: "...",
  decision-process: "...",
  identify-pain: "...",
  champion: "...",
  competition: "...",
  paper-process: "..."
}
```

Present extraction summary:
```
📋 Meeting Summary — [title]
Attendees: [names]
Topics: [bullets]
Their commitments: [bullets]
Our commitments: [bullets]
Next steps: [bullets]
MEDDPICC signals: [elements with new info]

Confirm or correct before I proceed? (yes / edit [field] / skip)
```

---

## Step 3: Draft follow-up email (skip if --skip-email or --quick)

Using the extracted facts, draft a follow-up email:
- Subject: "Follow-up: [meeting title] — [key outcome]"
- Body: confirms topics, lists both sides' commitments, states next step with date
- Tone: professional but warm; sounds like the AE wrote it, not a template

Save as a Gmail draft through the CLI (never send):
```bash
gws schema gmail.users.drafts.create --resolve-refs
gws gmail users drafts create --params '{"userId":"me"}' --json '<draft-resource-json>'
```

Verify the created draft with `gws gmail users drafts get`.

Report: "Draft created in Gmail. Subject: [subject]. Review before sending."

Do NOT send. Create as draft only.

---

## Step 4: Stage native ClosePlan evidence for /grill (skip if --skip-meddpicc or --quick)

This skill never writes qualification state. Do not read the former `meddpicc`
frontmatter block or `legacy_meddpicc` as current. If the pursuit has an exact
`sf_opportunity_id`, fetch the read-only native contract:

```bash
fieldkit sf meddpicc <opp_id> --json
```

If several deals are returned, report `pending`, list every exact deal ID, and ask
the operator to select one before rerunning with `--deal-id`. If the read is missing,
incomplete, or fails, report `unavailable` with the reason and keep any extracted
signals unassigned rather than guessing a question.

For a complete selected deal, associate each signal from Step 2 with an exact native
question ID. A category label alone is not a target:

```
Question ID          Native question                 Meeting evidence
a1E...               [exact Salesforce wording]      Customer cited 40% manual process cost
a1E...               [exact Salesforce wording]      [Name] introduced us to VP
```

Present every matched and unmatched signal, then hand off: "Run `/grill <account>`
to review this evidence against the current native questions." The current phase is
read-only: do not change Salesforce, pursuit frontmatter, question values, answers,
rollups, or qualification timestamps.

Then compose SF Next Steps string from gaps + commitments and preview:
"Write this to SF Next Steps for opp [ID]? (yes / edit / skip)"

If yes and sf_opportunity_id is set:
```bash
fieldkit sf set-next-steps <opp_id> '<composed_text>' --confirm
```

---

## Step 5: Create tasks

From `our_commits` and `next_steps` in the fact object, generate TASKS.md entries:
- Our commitments → Active section, tagged [account/pursuit]
- Waiting on (their commits) → Waiting On section with follow-up date

Present task list: "Add these to TASKS.md? (yes / edit / skip)"

If yes: write to TASKS.md, then run `/task-sync` to push to Google Tasks.

---

## Step 6: Offer qualification review

If the meeting produced qualification evidence, offer `/grill` for the exact
read-only native review. Do not claim the meeting changed Salesforce or cleared a
stage gate. If the operator then asks to advance, invoke `/pursuit-advance`; formerly
score-dependent transitions remain `pending` and require an explicit override reason.

---

## Step 7: Save meeting note

Write structured note to `accounts/<account>/meetings/YYYY-MM-DD-[topic-slug].md`:

```markdown
# [Meeting Title] — [DATE]

**Attendees:** [list]
**Account:** [account] | **Pursuit:** [pursuit]

## Topics Discussed
[bullets]

## Commitments
**Our team:** [bullets]
**[Customer]:** [bullets]

## Open Questions
[bullets]

## Next Steps
[bullets]

## MEDDPICC Signals
[elements with new information]
```

If account has a linked GDoc Pursuit Workbook (`gdoc_workbook` in frontmatter):
"Add this note as a new tab in the Pursuit Workbook? (yes / skip)"

If yes: use `fieldkit meeting note` command.

---

## Session Integrity

Carry the shared fact object through all steps. Never re-read the same file twice.
Each step reports what it did before moving to the next. If a step is skipped, say why.
