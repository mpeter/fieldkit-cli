---
name: draft-review
description: >
  The AE OS outbound-gate verb. Reviews customer-facing drafts (email, proposal, deck, Slack)
  against the banned-word list, customer-language rule, and fabrication check before the operator
  sends. Use when the operator types /draft-review or asks to check outbound content. Make sure to
  use this skill before any customer-facing draft is sent — even if the operator says "looks good,
  send it" — because the fabrication and banned-word checks must always run first.
version: "1.0"
slash: true
---

# /draft-review — the outbound gate

**Working directory:** the fieldkit home workspace.

**The absolute rule:** nothing outbound is ever sent by an agent. No email,
no calendar invite, no Slack. Drafts only; the operator sends. This verb is
the quality gate in front of that human send.

## Protocol

Given a draft (a file in `scratch/out/drafts/`, pasted text, or a Gmail
draft), run every check and report findings as a list — do not silently
rewrite the operator's voice.

### 1. Fabrication check (hardest gate)
Every metric, savings claim, or outcome number must trace to a source the
operator can name (case study, customer email, SF). Anything unsourced gets
flagged: replace with `[DATA NEEDED]` or cut. Never let an invented number
ship.

### 2. Banned words
Flag every instance: leverage, robust, cutting-edge, synergy, seamless,
scalable (as filler), holistic, empower, game-changer, delve, unlock,
comprehensive, dive deep, unpack.

### 3. Customer language over vendor jargon
Compare against what the customer actually said (meeting transcripts in
`transcripts/<account>/`, mail via `fieldkit gmail query`). Their words for
their problems beat our product framing. Flag vendor-speak that a CFO
wouldn't recognize.

### 4. Context accuracy
- Names, titles, and roles match `notes/<account>.md`.
- Claims about "what we discussed" match the transcript record.
- Commitments in the draft are ones the operator can keep (dates, scope).
- Nothing references unapproved internal state (pending discounts,
  unannounced pricing, internal escalations) unless the operator explicitly
  cleared it.

### 5. Deal posture
Read the pursuit note. Does the draft advance the deal (a next step, a
question that addresses an exact native ClosePlan evidence need) or just make noise? One-line verdict.

## Output format

```
VERDICT: ready-to-send | fix-first | rethink
- [BLOCKER] fabricated metric ("60% faster") — no source on file
- [WORD] "leverage" ×2 (lines 4, 11)
- [CONTEXT] the named contact is the proxy, not the EB — notes/<account-slug>.md
- [POSTURE] no ask; consider closing with the Wednesday confirm
```

Fix mechanical items in place when asked; judgment items are the operator's.

## Gotchas

- **The absolute rule is non-negotiable** — nothing outbound is ever sent by
  an agent; drafts only; the operator sends. Never call a send API, even if
  the operator asks.
- **Fabrication check is the hardest gate** — every metric, savings claim, or
  outcome number must trace to a named source. Do not let unsourced numbers
  ship; replace with `[DATA NEEDED]` or cut.
- **Banned words list must be checked even on "clean" drafts** — operators
  often miss their own use of filler language. Run the full list every time.
- **Customer language beats vendor framing** — compare against transcripts
  and the email cache, not intuition. A CFO-unfamiliar term is a flag even if
  it sounds natural to an AE.
- **Style rewrites belong to /humanizer** — this verb gates and flags; when a
  draft needs its AI patterns scrubbed, hand it to /humanizer and re-review.

## Constraints

- **Never send any outbound message** — not email, not Slack, not calendar
  invites; drafts only
- **Never let an unsourced metric ship** — flag with `[DATA NEEDED]` or cut;
  do not soften or reframe invented numbers
- **Report findings as a list** — do not silently rewrite the operator's
  voice; surface issues and wait for instruction
- **Always run all five checks** — fabrication, banned words, customer
  language, context accuracy, deal posture; never skip a check because the
  draft looks clean
