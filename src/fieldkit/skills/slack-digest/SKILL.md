---
name: slack-digest
description: >
  The AE OS Slack deal-room monitor. Sweeps each account's Slack channels for recent
  customer questions, team concerns, and unanswered threads; resolves unknown users; and
  surfaces what needs a reply. Use when the operator types /slack-digest, or
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

Slack is where deal-room reality shows up before it reaches SF or email —
customer questions, "ball in our court" asks, team worries. This verb sweeps
it deterministically, then synthesizes. It is **read-only**: it never posts,
drafts, or sends to Slack.

## Slack access

Slack is accessed via **`slackcli`** (browser session tokens from the
workspace `.env`). There is no Slack MCP server.

Prereq: `slackcli auth list` must show the operator's workspace. If auth is
dead, stop and tell the operator to re-run `slackcli auth login-browser` — do
not fall back to a partial source or cached data.

## Two collection pathways (collect before judging — never skip)

| Pathway | Purpose | Command |
|---|---|---|
| Watcher | Flags aged unanswered threads automatically | `fieldkit watch run slack-threads [--account <slug>] [--threshold-hours N] [--dry-run]` → `watchers/slack-thread-alerts.md` |
| Deal-room sweep | Exhaustive per-account keyword sweep | `tools/dealroom/scan.py --account all --days 7` (or a single slug, `--days 14 --limit 20`) |

Both read each in-scope account's `keywords` from `config/accounts.yaml`.
`scan.py` runs an exhaustive `slackcli search messages` pass over the window,
dedups by permalink, and writes `scratch/out/dealroom-<account>.json`
(channels, messages, `unresolved_users`). Collection is separated from
judgment on purpose — the full match set lands on disk before you interpret
anything. Use direct `slackcli` (`search messages`, `search channels`,
`search people`, `conversations`) only for targeted follow-up lookups.

## Resolve unknown users (the resolution rule)

For every id in `unresolved_users`:

```bash
slackcli search people <U0XXXXXX>
```

Never attribute a message to a raw `U…` id. If resolution fails, mark it
`[Unresolved: U0XXXXXX]` and flag for the operator — don't guess.

## Synthesize, don't dump

Present per account, newest first:

1. **Ball in our court** — customer asked something, no reply from our side
   after it.
2. **Customer signals** — concerns, timeline pressure, new stakeholders.
3. **Team concerns** — internal worry not yet visible in SF/email.

Group by account; lead with the channel and who said it.

## Labeling & boundaries

- Slack is external intelligence: label surfaced items `[Slack intel]`. It
  informs the operator; it is **not** written to pursuit or account files
  unless the operator confirms it first-hand.
- Read-only: never send, draft, or post Slack messages from this verb.
- Feeds `/brief`. When run inside brief, fold the top 1–2 ball-in-court items
  into the attention synthesis rather than a separate dump.
- Internal accounts (`internal: true` in `config/accounts.yaml`) are skipped
  unless the operator names one explicitly.
- Qualification-relevant intel (champion behavior, competitor mentions) routes
  to `/grill` as staged context for exact native questions. This skill never
  scores or changes ClosePlan.
