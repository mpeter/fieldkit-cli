---
name: followup-draft
description: >
  A customer call just ended and you need to send a follow-up email — just the email,
  not the full post-meeting workflow. Drafts a concise, professional follow-up from
  call notes, transcript, or a summary, and sounds like you wrote it.
  Distinguish from post-meeting (full post-call workflow including native
  qualification evidence, tasks, and ingestion — this is email only).
  Trigger with "draft the follow-up", "write the follow-up email", "follow-up email for [account]",
  "send a follow-up after the call", "draft follow-up", "follow up on the call",
  "email summary of the meeting", "post-call email".
metadata:
  opencode/slash: "true"
  category: ops
---

# Follow-Up Draft Skill

Draft a professional, concise post-meeting follow-up email that:
- Confirms the AE's understanding of customer requirements matches what was discussed
- Captures commitments on both sides
- Keeps the deal moving forward
- Sounds like the AE wrote it — not like a template

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today; this skill reads from cached files
- **CLI authentication must be available** — verify the needed `gws` command can read before drafting from Google data
- **Trigger overlap with adjacent skills** — check that you need this specific skill and not a closely named one (for example, the `contract` skill's [contract check](../contract/ops/contract-check.md) versus [contract extract](../contract/ops/contract-extract.md))

## Constraints

- **Never write to account files without explicit confirmation**
- **Do not modify pursuit frontmatter mid-workflow** — only write at designated save steps
- **Always surface output for review before sending externally**

## Input Sources (use what's available)
- Meeting notes pasted by user
- Call transcript or recording summary (pasted or from meeting tool)
- Calendar event (via the `gws calendar` CLI) for attendees + time
- People.AI contact data for title confirmation
- Existing account context from `accounts/<account>/account.md`
- Previous email threads (via the `gws gmail` CLI) for tone/relationship context

---

## Process

### Step 1: Extract the Key Facts
From the meeting notes or transcript, identify:
- **Who attended** (names, titles, companies)
- **What was discussed** (main topics — 3–5 bullets max)
- **What the customer said they need** (their language, not ours)
- **Commitments we made** (what we promised, by when)
- **Commitments the customer made** (what they agreed to do, by when)
- **Open questions** (things that came up without resolution)
- **Agreed next steps** (the specific next meeting, demo, POC, etc.)
- **MEDDPICC signals** (new info on any element — champion behavior, EB access, pain, process)

If any of these are missing from the notes, flag it to the user before drafting.
Do not invent commitments or next steps.

### Step 2: Check the Relationship Register
Before writing, check:
- How formal/informal is this relationship? (Gmail thread history or user guidance)
- Are there any sensitivities from previous interactions?
- What's the current deal stage — early discovery or late-stage negotiation?

**Check internal Slack** before drafting — colleagues may have flagged concerns, blockers,
or context from the same meeting that should shape the follow-up tone or content:

```bash
# Check daily cache first
grep -i "<account name>" <data-repo>/watchers/slack-signals.md

# Find account project/delivery channels for internal context
slackcli search channels "<account name>"          # find #team-acme-corp-*, #proj-acme-corp-*
slackcli conversations read <channel-id> --limit 10

# Search for recent internal discussion about this meeting's topic
slackcli search messages "<account name> <topic>" after:<meeting-date> --limit 10
```

Look for: internal concerns a colleague raised that the email should acknowledge,
commitments made in Slack that need to be tracked, or delivery issues that would
affect the tone of what you commit to in writing. Slack is internal only —
do not reference internal Slack content in the customer email itself.

See [`references/slack-search-protocol.md`](references/slack-search-protocol.md).

Early-stage: warmer, more open-ended, focused on listening.
Late-stage: crisper, focused on path to close.

### Step 3: Draft the Email

Read `email-template.md` for the output structure and tone rules.

---

## Output

Produce two versions:

**Version A — Short** (exec-level recipients or brief meetings)
Under 150 words. Subject + 3 bullets + next step. That's it.

**Version B — Standard** (working-level recipients or complex meetings)
Full structure from template. Under 300 words.

Present both to the user and ask which to use, or let them mix elements.

---

## Internal Meeting Summary

In addition to the email, produce an internal summary for the pursuit file:

```
## Meeting Summary — [Date] — [Topic]

**Qualification Evidence:**
- [native question ID when known]: [what we learned — evidence only; current
  qualification changes only in Salesforce and mutation is not enabled]

**New Stakeholder Intel:**
- [any new info on stakeholders, roles, or influence]

**Risk Flags:**
- [anything concerning — timeline slip, competitive threat, champion cooling]

**Deal Velocity:**
- [is the deal accelerating, stalling, or unchanged?]
```

---

CLI route needed: `gws gmail` (create Gmail draft). Meeting notes and pursuit-file edits are written directly to disk.

## Step: Write to GDocs meeting log if configured

Check pursuit frontmatter for `gdoc_meeting_log`. If set and non-empty:
- Run: `fieldkit meeting note <pursuit-path>` to append the follow-up to the GDoc log
- Note the GDoc URL in the output

If `gdoc_meeting_log` is absent or empty, create/append to a markdown meeting note file as before.

---

## After Drafting

1. **Save meeting notes** by writing the file directly:
   ```
   write accounts/<account>/meetings/YYYY-MM-DD-<topic>.md
   ```

2. Do NOT send — present to user for review and approval

3. If the user approves, create the draft with `gws gmail users drafts create` (draft only — never send), then verify it with `gws gmail users drafts get`

4. **Edit the pursuit file** with new intel directly:
   ```
   # Step 1: Read the current "Next Steps" section of
   #   accounts/<account>/pursuits/<opp>.md
   # Step 2: Apply a targeted edit replacing the old section content
   #   with the updated content
   ```
   Update sections for: new commitments captured, updated next step/date,
   new stakeholder intel, and qualification evidence. Do not turn evidence into
   local scores or write native ClosePlan state; `/grill` performs the exact
   read-only question review.

---

## Related Skills

- `meeting` — Uses meeting brief output as input
- `/grill` — Align follow-up asks to exact native ClosePlan evidence needs
- `/humanizer` — Run humanizer on draft before sending

## SF Next Steps

When you have signal that an opp's next step should change, follow the protocol in:
[`references/sf-next-steps-protocol.md`](references/sf-next-steps-protocol.md)
