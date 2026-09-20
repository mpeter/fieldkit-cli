---
name: meeting
description: Build a pre-meeting brief with context, agenda, questions, talking points, and
  objection handling; also batch-preps tomorrow's meetings. Covers QBR prep ("QBR for
  [account]"), account snapshots ("account snapshot"), pulse checks ("account pulse"),
  stakeholder mapping ("stakeholder map"), and 1:1 updates ("1:1 update"). Trigger with "meeting
  prep [account]", "prep for tomorrow", or any phrase above.
metadata:
  opencode/slash: "true"
  category: product
---

# Meeting Skill

Give the AE everything they need to walk into a customer meeting with confidence:
context, agenda, questions, talking points, and anticipated objections.

## Folded Ops

Read on demand: [`ops/qbr-prep.md`](ops/qbr-prep.md), [`ops/account-snapshot.md`](ops/account-snapshot.md), [`ops/account-pulse.md`](ops/account-pulse.md),
[`ops/stakeholder-map.md`](ops/stakeholder-map.md), [`ops/one-on-one.md`](ops/one-on-one.md).

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account files without explicit confirmation**
- **Always surface generated output for review before any external send**

Routes needed: **fieldkit-sales** (Backstory MCP exception), `gws gmail` (recent emails), `gws calendar` (calendar event), `tvly` (web search), **fieldkit-dataverse** (optional Rover MCP exception), and `chrome-use` (optional intranet research). Account and meeting files are read directly from disk.

## Step: Check for GDocs meeting log

Read pursuit frontmatter. If `gdoc_meeting_log` is set and non-empty:
- Fetch meeting content: `gws docs documents get --params '{"documentId":"<gdoc_meeting_log value>"}'`
- Use this as the meeting history context for the brief
- Note the GDoc URL: https://docs.google.com/document/d/<id>/edit

If `gdoc_meeting_log` is absent or empty, fall through to scanning markdown meeting files as before.

---

## Input Sources (use all available)

**Check dossier first:**
```
read accounts/<account>/dossier/dossier.md
```
If it exists and was generated within 14 days, use it to seed context — skip re-deriving
contract history, key contacts, and strategic priorities already captured there.

- **Calendar event** — `gws calendar events get/list` — attendees, topic, time
- **Account file** — read `accounts/<account>/account.md`
- **Recent meeting notes** — 3 most recent under `accounts/<account>/meetings/` (`ls -t accounts/<account>/meetings/*.md | head -3`)
- **Recent email threads** — `gws gmail users messages list` — last 5–10 emails with these contacts
- **Backstory** — fieldkit-sales group — engagement scores, last interactions, health
- **Slack search** — `slackcli search messages "<account name>"` and contact names
- **Web search** — `tvly search` — recent news, earnings, exec announcements
- **Pursuit frontmatter** — read the pursuit file for `sf_stage`, `sf_close_date`
- **Directory integration** — for each attendee on your organization's domain, optionally query a configured directory source for title, manager, and location. Skip and note "Directory unavailable" if the source is unreachable or returns an error.
- **Knowledge-source integration** — optionally search a configured internal knowledge source for content relevant to the meeting topic. It may require a valid session and network access.

Run Slack searches for the last 2 weeks — open items, champion signals, team concerns.
Resolve any unknown Slack users with `slackcli search people "<name or email>"`.

---

## Step 0: Gather Backstory Intelligence

**Always run this before generating any brief content.** This fulfills the CLAUDE.md
rule: "Always invoke Backstory before: meeting."

```
1. backstory__backstory__find_account(<account name>)
   → peopleai_account_id

2. backstory__backstory__get_account_status(peopleai_account_id)
   → health score, risks flagged, next steps, trending topics

3. backstory__backstory__get_recent_account_activity(peopleai_account_id)
   → what's been discussed in last 30 days across all contacts

4. backstory__backstory__account_company_news(peopleai_account_id)   [public companies only]
   → business pressures, leadership changes, strategic shifts
```

Fold these signals into the brief's **Backstory Signals** subsection (see brief template).
Label any Backstory-sourced insight as `[Backstory]` so the AE knows what to verify.
Do NOT write Backstory data into pursuit frontmatter or account.md — brief only.

---

## Dataverse Rover Enrichment (optional)

For each attendee on your organization's domain, if your directory integration is available:

1. Query Rover People for: title, manager, location, department
2. Add a **From Dataverse** subsection in the attendee block:
   ```
   **[Name]** (Rover): Senior Principal Architect → reports to [Manager], based in [City]
   ```
3. If `fieldkit-dataverse` is unavailable or returns an error: note "Dataverse unavailable — org context not enriched" and continue.

## The Source Research (optional)

Before generating brief content, if thesource session is valid (`~/.config/thesource-mcp/cookies.txt` < 10h old):

1. Use `thesource search_content` with the meeting topic and account name as query
2. Include up to 3 results in a **From The Source** section:
   ```
   From The Source:
   - "AI Strategy 2025" — configured knowledge-source URL
   - "Virtualization Roadmap" — configured knowledge-source URL
   ```
3. If session is missing or expired: skip and note "thesource session not active — run /thesource login for internal research"

---

## Attendee Research

For each attendee, pull from People.AI and web search:
- Title, role in deal, relationship history
- LinkedIn profile summary (web search)
- Communication style indicators from past emails
- Last interaction date and context
- Their known priorities and concerns

If an attendee is new (no prior history), flag for extra attention and
recommend an opening approach.

### Composite Contact Context

For each attendee whose email is known, query the contact lookup CLI:
```
fieldkit contact find <email> --json
```

Parse the JSON output and present as a table:

| Attendee | Champion Signal | Decay Signal | Emails | Meetings | Slack Messages |
|----------|----------------|--------------|--------|----------|----------------|
| [Name] ([email]) | [champion_signal] | [decay_signal] | [message_count] | [meeting_count] | [slack_message_count] |

- **Champion Signal** — INITIATOR (contact reaches out first), MIXED (bidirectional), or REACTIVE (you initiate; contact only responds)
- **Decay Signal** — GONE (no recent contact), DECAY (engagement dropping), or ACTIVE (healthy cadence)
- Missing data (contact not in index) — note "no local signal" and rely on Backstory/web

Include this table in the brief immediately after the attendee list.

---

## Native Qualification Evidence (pursuit-specific prep)

When the meeting is tied to a specific pursuit (deal name or opportunity mentioned in the prompt):

1. Read the pursuit frontmatter from `accounts/<account>/pursuits/<opp>.md`
2. Resolve its exact `sf_opportunity_id`; treat local `meddpicc` or
   `legacy_meddpicc` only as historical provenance
3. Run `fieldkit sf meddpicc <opp_id> --json`
4. If several deals are returned, list exact deal IDs and mark qualification
   `pending` until the operator selects one; if the read fails or is incomplete,
   mark it `unavailable`
5. For a complete selected deal, choose 2–3 unanswered or weakly evidenced exact
   native questions and turn them into meeting questions without changing their
   values
6. Add a **Native Qualification Evidence to Probe** section with each exact
   question ID and Salesforce wording

If no pursuit file or Opportunity link exists, mark the section `unavailable`
rather than inventing a gap.

---

## Meeting Brief Structure

Read `brief-template.md` for the full output format.

---

## Meeting Type Variants

**Discovery Call** — heavy on questions, light on talking points. Goal: listen.
**Executive Briefing** — brief agenda, strong opening frame, single clear ask.
**Technical Demo** — add a "demo flow" section; map features to stated pain.
**QBR** — read [`ops/qbr-prep.md`](ops/qbr-prep.md) instead.
**Objection / Recovery Meeting** — lead with acknowledgment of issue before agenda.
**Negotiation** — add BATNA section and concession ladder.

---

## Competitive Preparation

If competitors are active in this account (from pursuit file or Backstory):
- Include a "Competitive Landscape" section with positioning vs. each competitor
- Add landmine questions that naturally expose competitor weaknesses
- Prepare responses to competitor claims the customer might raise

---

## Batch Mode (auto-prep tomorrow's meetings)

Triggered by: "prep for tomorrow", "auto-prep tomorrow's meetings", "meeting-auto-prep", "what meetings do I have tomorrow", "generate prep briefs".

### Step B1: Load account config

Read `config/accounts.yaml`. Build a domain-to-account lookup map from `accounts.<name>.domains`.
Internal domains to always skip: your configured company domains.

### Step B2: Query tomorrow's calendar

Compute tomorrow's date range in the user's local timezone:

```bash
gws calendar events list --params '<calendar-id-and-time-range-json>'
```

Request the attendees field along with the time range; inspect the method schema
before composing unfamiliar parameters.

### Step B3: Classify each event

For each calendar event:
1. Extract all attendee email addresses.
2. Skip your configured internal domains.
3. Look up each external domain in the domain-to-account map.
4. If any domain matches → classify as external account meeting, record matched account.
5. If multiple domains match different accounts: prep for the first, note others in the summary table.
6. If no attendees, all internal, or no domain match → skip; log reason in summary table.

### Step B4: Generate a brief per matched meeting

For each matched external meeting, run the standard single-meeting flow above (Steps 0 through Competitive Preparation), using the calendar event as the meeting context. Apply the same meeting type variants and brief template.

### Step B5: Save briefs and output summary

Save each brief by writing the file directly:
```
accounts/<account>/meetings/YYYY-MM-DD-<topic-slug>-prep.md
```

Where `<topic-slug>` is the event title lowercased, spaces → hyphens, truncated to 40 chars, non-alphanumeric removed. Check for existing file first — if it exists, notify user and do not overwrite without confirmation.

Output a summary table:

```
## Tomorrow's Meeting Auto-Prep Summary

| Time | Event Title | Account Matched | Brief Generated | File Path |
|------|-------------|-----------------|-----------------|-----------|
| 9:00 AM | Q2 Strategy Review | acme-corp | Yes | accounts/acme-corp/meetings/... |
| 11:00 AM | Internal Standup | (internal only) | No | — |
```

Every event must appear in the table. Continue processing on per-event errors — never abort the full run for a single event failure.

**Graceful fallbacks:** No events → "No meetings scheduled for tomorrow." Brief already exists → alert user, skip. Backstory/Slack/Gmail errors → note in brief, continue.

---

## After Generating the Brief

1. Write the brief to `accounts/<account>/meetings/YYYY-MM-DD-<topic>-prep.md`
2. Present to user — ask if they want to adjust agenda or questions
3. After the meeting, use `followup-draft` skill to capture outcomes

## SF Next Steps

When you have signal that an opp's next step should change, follow the protocol in:
[`references/sf-next-steps-protocol.md`](references/sf-next-steps-protocol.md)
