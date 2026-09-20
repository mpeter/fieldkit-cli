# Week End

Complete end-of-week close-out in 1–2 minutes.
Closes open loops, captures lessons, drafts your 1:1, and resets for next week.

Invoked on Friday afternoon. This is the weekly close-out counterpart to
`ops/week-start.md`: that one opens the week and sets priorities, this one
closes it and resets TASKS.md for the next.

Previously a standalone skill named `week-end`. Folded here under D1 Wave 5
per the D4 fold mechanics — it is a brief cadence, so it lives with the brief.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent ops** — this is the Friday close-out;
  `ops/week-start.md` is the Monday setup and the brief root is the daily run

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Options

- `--skip-1on1` — skip 1:1 draft generation
- `--no-reset` — skip TASKS.md reset (manage manually)

Routes needed: `gws tasks` (Google Tasks sync). TASKS.md, pursuit files,
and meeting notes are read directly from disk (native file reads).

---

## Step 1: SF hygiene check

For each active pursuit file with `sf_opportunity_id` set:
- Check `sf_last_pulled` frontmatter field: if > 7 days ago → flag
- Do not inspect local historical qualification values. Note that a current
  ClosePlan review requires `/grill`; this hygiene pass reports qualification as
  unavailable rather than inventing a cached result
- Check `sf_next_steps` field: if empty → flag for update before EOW

```bash
fieldkit pursuit audit --account <all>
```

Read the audit output. Report: "N pursuits need SF update before EOW."
Keep structural/timeline audit results separate from native qualification.

---

## Step 2: Follow-up audit

Read TASKS.md **Waiting On** section. For each item:
- Parse the follow-up date (if present)
- If follow-up date is past or item is > 7 days old with no date → flag

Output: "N waiting-on items past follow-up date: [list with age]"

---

## Step 3: Done-this-week summary

Read TASKS.md **Done Today** and **Done** sections.
Scan `accounts/*/meetings/` for files created or modified this week.
Scan pursuit file `transition-history` for entries with this week's date.

Summarise:
- N tasks completed
- N meetings with external accounts (meeting note files from this week)
- Stage advances this week: [list of deal → stage]

---

## Step 4: Lessons learned

Prompt: "Any lessons from this week to capture? (yes / skip)"

If yes: collect lesson text from user. Append to `memory/system/lessons-learned.md`:

```markdown
## [DATE] — [account or topic if applicable]
- [lesson 1]
- [lesson 2]
- [lesson 3]
```

---

## Step 5: Draft 1:1 update (skip if --skip-1on1)

Invoke the `meeting` skill's `src/fieldkit/skills/meeting/ops/one-on-one.md` logic.
Save to `<fieldkit_home>/archive/one-on-ones/YYYY-MM-DD-1on1-update.md`.
Report: "1:1 draft saved. Review before Monday."

---

## Step 6: Reset TASKS.md for next week (skip if --no-reset)

1. Move **Done Today** items → **Done** section
2. Move any **Today** items not completed → **Active** with carry-forward note: "(carried from [DATE])"
3. Clear **Today** section
4. Run `/task-sync` to push changes to Google Tasks

Report: "N items moved to Done, N items carried to Active, Today section cleared."

---

## Output Format

```markdown
## Week Wrap — [DATE]

### ✅ Accomplished This Week
- N tasks completed
- N meetings with external accounts
- Stage advances: [list or 'none']

### ⚠️ SF Hygiene Issues
- [pursuit]: sf_last_pulled N days ago — run sf-sync
- [pursuit]: no Next Steps — update before EOW
- (none)

### 📬 Follow-Ups Past Due
- Waiting on [name] re: [topic] — sent [date], N days past due
- (none)

### 📝 1:1 Draft
Saved to: <fieldkit_home>/archive/one-on-ones/[DATE]-1on1-update.md

### 🔄 TASKS.md Reset
N items moved to Done, N items carried to Active, Today section cleared.
```
