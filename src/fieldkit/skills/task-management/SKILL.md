---
name: task-management
description: >
  You're mid-session and need to add, update, or review pursuit action items and daily
  commitments tracked in TASKS.md. Manages pursuit actions, documented native
  qualification evidence needs, waiting-on items, and daily commitments from a
  single task file.
  Trigger with "task management", "update my tasks", "what are my open tasks",
  "add a task", "TASKS.md", "task list", "manage action items", "pursuit tasks",
  "what do I need to do today", "show my tasks".
metadata:
  category: ops
---

# Task Management

Tasks are tracked in `TASKS.md` in the project root.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Always surface generated output for review before any external send**

## File Location

Always use `TASKS.md` in the project root. If it doesn't exist, create it from the template below.

## Format & Template

```markdown
# Tasks

## Today

<!-- Reset each morning. Move done items to Done Today. Roll unfinished back to Active. -->

## Active

## Waiting On

<!-- Format: - **[Account / Pursuit]** Waiting on [person] re: [topic] — sent [date], follow up by [date] -->

## Someday

## Done Today

## Done
```

Task format:

- `- [ ] **Task title** — [Account] context, for [person], due [date]`
- Sub-bullets for additional details
- Completed: `- [x] ~~Task~~ (date)`

Waiting On format:

- `- **[acme-corp / Platform Modernization]** Waiting on <contact-name> re: SOW redlines — sent 2026-04-28, follow up by 2026-05-07`

## Task Sources

Tasks come from five places — never generic trackers like Jira or Linear:

1. **Pursuit files** (`accounts/*/pursuits/*.md`) — documented qualification
   evidence needs, champion follow-ups, and proposal deadlines; local legacy
   scores are not current
2. **Backstory (People.ai)** — risks flagged, next steps surfaced, stalled engagement
3. **Gmail cache** (`fieldkit gmail query`) — commitments made in email threads, unanswered asks
4. **Salesforce** — stage changes, close date shifts, new opportunities assigned
5. **Meetings and conversations** — action items from calls, QBRs, internal syncs

## Protocols

### Morning Reset (run when user says "start my day" / "morning reset")

1. Read TASKS.md
2. Move any unfinished Today items back to Active
3. Clear Done Today (move notable items to Done)
4. Surface any Waiting On items with overdue follow-up dates — flag by account
5. Ask: "What are you driving today?" — add 1-3 items to Today
6. If pursuit notes or a current `/grill` review names an urgent native evidence
   need, surface it as a candidate Today item; do not derive gaps from historical
   local scores

### EOD Close (run when user says "wrap up" / "end of day")

1. Read TASKS.md
2. Mark any completed Today items as done (`[x]`, move to Done Today)
3. Roll unfinished Today items back to Active
4. Ask about any new Waiting On items from today's meetings or emails
5. Append a dated entry to `memory/system/lessons-learned.md` if anything notable happened (win, loss, new stakeholder insight, deal signal)
6. Output a one-paragraph summary of what moved

## How to Interact

**When user asks "what's on my plate" / "my tasks":**

- Read TASKS.md
- Summarize Today first, then Active — grouped by account
- Highlight anything in Waiting On with a follow-up date that has passed
- If any pursuit has a close date within 14 days, flag it

**When user says "add a task" / "remind me to":**

- Add to Active (or Today if user says "for today")
- Format: `- [ ] **Task** — [Account] context, due [date]`
- Always include the account name (and its slug) when the task relates to a specific account or pursuit

**When user says "done with X" / "finished X":**

- Mark `[x]` with strikethrough and date
- Move to Done Today (or Done if end-of-day close)
- If the task gathered qualification evidence (for example, "confirm economic buyer"), stage it for `/grill`; do not write a local score

**When user asks "what am I waiting on":**

- Read the Waiting On section
- Flag anything past its follow-up date
- Group by account

**When user moves something to Waiting On:**

- Always capture: account/pursuit, who, topic, date sent, follow-up-by date
- Match the person against `accounts/*/account.md` stakeholder maps when possible

## Conventions

- **Bold** the task title for scannability
- Include "[Account]" prefix when the task relates to a specific account
- Include "for [person]" when it's a commitment to someone
- Include "due [date]" for deadlines
- Include "since [date]" for waiting items
- Keep Done for ~1 week, then clear old items
- Today resets daily — it's a commitment list, not a backlog

## Extracting Tasks from Meetings

When summarizing meetings or conversations, offer to add extracted tasks:

- Commitments the user made ("I'll send that over")
- Action items assigned to them
- MEDDPICC actions surfaced ("we need to confirm the decision process")
- Follow-ups with specific people at the account

Ask before adding — don't auto-add without confirmation.

## Extracting Tasks from Pursuit Files

When reading pursuit files, surface only documented qualification actions as
candidate tasks. Do not infer current gaps from `meddpicc` or `legacy_meddpicc`:

- Missing economic buyer → "Schedule intro to EB at [Account]"
- No champion identified → "Identify and develop champion for [Pursuit]"
- Stale next steps → "Update next steps on [Pursuit] — last updated [date]"
- Close date within 30 days with incomplete paper process → "Confirm paper process for [Pursuit]"
