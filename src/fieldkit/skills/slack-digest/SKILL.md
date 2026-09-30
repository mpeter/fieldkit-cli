---
name: slack-digest
description: >
  Review an authorized, bounded Slack source for customer questions, team
  concerns, and threads that may need a reply. Report scope and gaps without
  claiming exhaustive coverage or modifying Slack. Use when the operator types /slack-digest, or
  asks what's happening in Slack or the deal rooms — "what's in Slack", "Slack catch-up",
  "any unanswered Slack questions", "check Slack for [account]" — and whenever Slack
  activity needs surfacing before a brief run or account review.
metadata:
  opencode/slash: "true"
  argument-hint: "[account]"
  category: ops
---

# /slack-digest — surface what Slack is saying about the accounts

**Working directory:** the fieldkit workspace root.

Use this agent workflow to summarize an identified Slack source. It is not a
continuous monitor, a fieldkit CLI subcommand, or proof that a whole account's
conversations were searched. It is **read-only with respect to Slack**: it never
posts, drafts, reacts, or sends. Optional watcher runs have separate local write
effects and require approval; invoking the skill alone writes nothing.

## Slack access

The fieldkit thread watcher invokes a separately installed `slackcli`.
fieldkit does not bundle that client or establish its workspace permissions.
For direct research, confirm the installed client's supported interfaces and
authentication without printing tokens. Do not assume tokens are in `.env`,
extract browser cookies, or log in automatically as a recovery step.

Use the [Slack search protocol](../tool-routing/references/slack-search-protocol.md)
for identity, bounded reads, privacy, and completeness. If it is absent from a
selective installation, request an approved source instead of guessing client
commands. Missing or failed authentication keeps live research unavailable.
An operator-approved cache can be summarized only with known age, coverage,
and provenance, explicitly labeled cached rather than presented as a live read.

## Choose a bounded source before judging

Confirm the account, workspace, channels, time range, result/page bounds, and
finite timeout. Prefer a selected source or the installed client's verified
read-only search interfaces. Record what was searched and whether retrieval
completed; a missing page or failed response is not a complete empty result.

The optional `fieldkit watch run slack-threads` command is a thread-age heuristic,
not a complete deal-room digest. It searches the first configured account
keyword (or a fallback account name), limits returned messages, filters by
configured account-channel names or a fallback, and excludes internal-channel
patterns. Accounts marked `internal` or `slack_watch: false` are skipped even
when named explicitly. It does not prove all customer questions were answered.

If the operator requests this watcher, confirm the exact account and use its
supported `--account`, `--limit`, `--limit-per-account`, `--threshold-hours`,
`--dry-run`, and `--json` options. A preview still makes live Slack reads but
does not create or prune fieldkit watcher logs, alerts, state, or run status.
It is not offline; the configured external client's own effects are separate.
A normal run also writes local alerts, state, and run status. Approve those
local effects separately from the read. This watcher is not an exhaustive
deal-room scan.

Inspect `outcome`, `auth_error`, `failures`, and `records_checked`, not just exit
status. Authentication failures exit 2; provider or persistence failures exit 1;
invalid arguments or configuration exit 3. Those results remain non-passing.
A failure or empty check does not
prove no account activity. Keep incomplete collection visibly pending.

## Resolve unknown users (the resolution rule)

For an unknown identity, use only a separately authorized, bounded identity
lookup supported by the installed client. No universal `unresolved_users`
collection schema or identity-lookup command is supplied by this skill.

Never invent a person's name from a raw `U…` id. If resolution fails, mark it
`[Unresolved: U0XXXXXX]` and flag for the operator — don't guess.

## Synthesize, don't dump

Present per account, newest first:

1. **Possible ball in our court** — a question appears unanswered in the
   observed thread context; report incomplete context and confirm responsibility.
2. **Customer signals** — concerns, timeline pressure, new stakeholders.
3. **Team concerns** — internal worry not yet visible in SF/email.

Group by account; lead with the channel and who said it.

## Labeling & boundaries

- Slack is external intelligence: label surfaced items `[Slack intel]`. It
  informs the operator; it is **not** written to pursuit or account files
  unless the operator confirms it first-hand.
- Read-only: never send, draft, or post Slack messages from this verb.
- Offer findings for a separately requested brief; do not claim that invoking
  this skill runs a watcher, updates a cache, or feeds a scheduled report.
- Include internal accounts only through an explicitly authorized direct source;
  naming one does not override the watcher's internal-account exclusion.
- Qualification-relevant intel (champion behavior, competitor mentions) routes
  to `/grill` as staged context for exact native questions. This skill never
  scores or changes ClosePlan.

Finish with source, scope, observed dates, completeness, identity gaps, and
findings. Local retention requires an approved private destination, confined
atomic write, and read-back. Keep message bodies, personal identifiers, and
private channel names out of public issues and release evidence.
