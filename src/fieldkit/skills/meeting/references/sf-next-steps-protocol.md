# SF Next Steps Update Protocol

Shared reference for any skill that may recommend updating `Next_Steps__c` on a
Salesforce Opportunity. Include this in your reasoning whenever you have signal about
what an AE's next action should be.

---

## When to Recommend an Update

Recommend when **all three** hold:

1. **Clear successor action exists** — you can state the next step specifically.
   "Schedule POC kickoff with [name] by [date]" qualifies. "Follow up" does not.

2. **Authoritative source** — the proposed text comes from a meeting transcript,
   a sent/drafted email with a commitment, the AE's own words, or explicit AE input.
   Do not infer next steps from thin signals (e.g. a contact opened an email).

3. **Current value is stale or empty** — the existing field refers to a completed
   action, is empty, or the pursuit frontmatter `sf_next_steps` diverges from SF.

**Also recommend** when: active opp has no next step and you have a relevant signal.

**Never recommend** when:
- Opp stage is Closed Won or Closed Lost
- Current value appears current and no new action has been identified
- Proposed text is a restatement of the current value with minor rewording
- The only signal is field age — staleness alone without a known completed action
  is not sufficient

---

## Proposal Format

Always output a structured block. Never write prose that buries the command.

```
📋 Next Steps Update Proposal — <Opp Name> (<opp_id>)

Current (SF)   : <current value, or "(empty)">
Signal         : <source and date — e.g. "Post-meeting notes 2025-06-04: POC scoping
                  complete; 3-node pilot approved, legal reviewing SOW">
Proposed text  : <specific, actionable next step>

Source file    : <path to transcript/email/frontmatter that supports this>

To apply (run this yourself):
  fieldkit sf set-next-steps --confirm <opp_id> "<proposed text>"
```

Rules for the proposed text:
- One to two sentences max
- Starts with a verb (Confirm, Schedule, Send, Review, Follow up with...)
- Includes a person or team when known
- Includes a date or week when known

---

## What the Agent Must NOT Do

- Never run `fieldkit sf set-next-steps --confirm ...` autonomously — the hook will block it
- Never present multiple proposed texts and ask the AE to pick — draft the best one
- Never update without showing the current value first
- Never propose an update if the source evidence is thin or ambiguous

---

## Post-Update Reminder

After the AE confirms the write, remind them:

> If you edited `sf_next_steps` in the pursuit frontmatter locally, run
> `fieldkit sf opportunity <opp_id> <pursuit_file>` to pull SF back down and
> keep frontmatter in sync.
