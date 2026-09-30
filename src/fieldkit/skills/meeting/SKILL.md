---
name: meeting
description: Prepare a source-attributed meeting brief or tomorrow's calendar drafts.
  Route QBRs, account snapshots, pulses, stakeholder maps, and 1:1 updates to
  their on-demand workflows.
metadata:
  opencode/slash: "true"
  category: product
---

# Meeting preparation

Use this skill for a customer meeting brief or "prep for tomorrow." Produce a
reviewable draft with context, an objective, agenda, questions, and a proposed
close. Read the [brief template](brief-template.md) for the output structure.
An absent integration is missing evidence, not a reason to invent a signal.

For a different task, read the corresponding on-demand workflow:
[QBR prep](ops/qbr-prep.md), [account snapshot](ops/account-snapshot.md),
[account pulse](ops/account-pulse.md), [stakeholder map](ops/stakeholder-map.md),
or [1:1 update](ops/one-on-one.md). Do not combine their outputs by default.

## Prepare one meeting

1. Identify the account, meeting time, attendees, purpose, and relevant pursuit.
   Ask for missing identity when guessing could attach another customer's data.
   Read the configured workspace, not the code checkout. Start with a dated
   dossier if present, then inspect the account record (`account.md`), recent
   meeting notes, and the pursuit record relevant to this meeting. A dossier is
   background context, not proof its cached signals are current.
2. Use the local lookup below for known attendee emails when a
   local contact index exists. Resolve ambiguous names with an exact email.
   Report only fields returned for that contact; external contacts do not have
   local Slack message counts. Missing profiles remain `no local signal`.

   ```console
   fieldkit contact find <email> --json
   ```
3. If a local Gmail cache exists, use its account and contact queries for dated
   thread context. A cache read does not sync Gmail or prove who owes a reply.
   If the operator authorizes and has configured `gws calendar`, use the event
   for time and attendees; otherwise use the details they provided. Optional
   Google Docs meeting logs require authorized `gws` access and an exact
   document ID from the pursuit record. Missing or failed reads are unavailable.
4. If a specific pursuit has an exact Salesforce Opportunity ID and live
   qualification is in scope, use the credentialed read below.

   ```console
   fieldkit sf meddpicc <opp_id> --json
   ```

   This read requires a configured and authorized Salesforce identity. Do not
   infer live availability from help output or a cached result. Record the
   returned qualification fields and observation time; a failed or incomplete
   read is unavailable, not verified qualification.
   Multiple linked ClosePlan deals are `pending` until the operator selects an
   exact deal ID; missing links, incomplete reads, and authentication failures
   are `unavailable` with the reason. Turn a few exact unanswered native
   questions into meeting questions. Do not treat historical local scores as
   current qualification or change any Salesforce value during prep.
5. Draft from the brief template. Distinguish customer statements, local
   records, provider suggestions, and your own inference. Include source dates
   for material claims and mark missing data unavailable. Adjust the agenda for
   discovery, executive, demo, recovery, or negotiation meetings; use the
   [QBR workflow](ops/qbr-prep.md) for a QBR. Present the draft for review and
   ask before writing it to the workspace. Never send it externally.

## Optional research

Use only routes configured for this operator and relevant to the meeting.
No internal knowledge service is bundled with fieldkit.

- An operator-provided account-intelligence or directory source may supply
  dated background and attendee details. These services are not bundled with
  fieldkit. Identify the source, verify its date and identity, and leave
  unavailable fields unknown; never promote a suggested risk to a customer
  commitment or copy it into pursuit frontmatter.
- If the operator has authorized an external research or internal discussion
  source, it may inform private preparation notes. Verify authors and dates,
  keep internal conversation out of the customer-facing draft, and treat
  public web results as leads rather than proof of a customer's priority.

## Prep for tomorrow

If an authorized calendar route is unavailable, ask the operator for event
details; do not claim that the calendar was scanned. Otherwise inspect the
calendar method's current parameters and request tomorrow's events in the
operator's local timezone. Read configured account domains and internal domains
from the workspace's `config/accounts.yaml`.

For each event, compare external attendee domains with configured account
domains. Internal-only events and events with no account match need no customer
brief. If domains match different accounts, mark the event ambiguous, list the
candidates, and ask the operator to select one before drafting. Never choose
the first match by list order. Draft for each unambiguous event using the
single-meeting steps above, and show an event-by-event summary including skips
and failures. One failed event must not conceal the others.

Present drafts and proposed destinations under the matching account's
`meetings/` directory. Obtain confirmation before writing any file; an
existing file needs explicit overwrite approval. Do not interpret a draft as
fresh Gmail, Salesforce, Backstory, or calendar synchronization.

After the meeting, offer the [follow-up draft](../followup-draft/SKILL.md).
When a Salesforce Opportunity next step might change, use the
[next-step protocol](../tool-routing/references/sf-next-steps-protocol.md);
if that reference is absent from a selective installation, stop that workflow.
