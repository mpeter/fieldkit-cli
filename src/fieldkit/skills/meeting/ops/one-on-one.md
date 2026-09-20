# One-on-One Update

Manager-ready 1:1 update in under 2 minutes.
Covers: what closed, wins, at-risk with asks, pipeline numbers, this week's focus, escalations.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Flags

- `--since YYYY-MM-DD` — custom lookback window for wins/closed (default: last 7 days)
- `--qbr-mode` — expand to quarterly summary (30-day window, add YTD numbers)
- `--slack` — shorter Slack-paste version (bullets only, no section headers)

Groups needed: **fieldkit-sales** (Backstory — positive signals and stage advances).
Pursuit files, TASKS.md, and watcher alerts are read directly from disk (native file reads).

---

## Step 1: Closed / won this week

Scan all pursuit files for `stage: closed-won` AND `last-transition` within lookback window.
For each: deal name, account, close value (sf_consulting_total or sf_training_total), brief context note.

---

## Step 2: Pipeline numbers

```bash
fieldkit pursuit forecast
```

Parse output for:
- Closed Won (QTD): sum of consulting + training from closed-won pursuits this quarter
- Commit: sum of negotiate + closed-won values
- Weighted Forecast: from forecast command output
- Best Case: from forecast command output

---

## Step 3: At-risk deals

```bash
fieldkit pursuit health
```

A deal is surfaced as at-risk if any of:
- Stuck in any stage > 21 days
- Close date is overdue, or within 30 days while still in an early stage
- Missing `sf_opportunity_id`
- Native qualification is unavailable in the local health report

Also read `<fieldkit_home>/watchers/pursuit-stall-alerts.md` for stall details.

For each at-risk deal, auto-suggest the manager ask:
- Stuck in negotiate > 30 days → "Need executive alignment call with [account]"
- Native qualification unavailable → "Run /grill for the exact read-only ClosePlan review"
- Zombie project → "Help with formal closeout / customer communication"

---

## Step 4: Wins and positive signals (7-day window)

Check pursuit file `transition-history` for stage advances in the lookback window.
Via **fieldkit-sales** group: `get_recent_account_activity` for positive signals (new contacts, meetings, engagement uptick).

---

## Step 5: This week's focus

Read TASKS.md Today + Active sections. Identify top 3 items by urgency and impact:
- Close dates within 30 days → highest priority
- Stuck deals with known next action → second tier
- Expiring projects needing renewal conversation → third tier

---

## Step 6: Escalations

Identify items that need manager action:
- Pursuit health HIGH risk at negotiate+ with no champion or EB
- TASKS.md Waiting On items older than 14 days with no response

---

## Output Format

```markdown
## 1:1 Update — [DATE]

### ✅ Closed / Won This Week
- [deal] at [account]: [value] — [brief context]
- (none)

### 🏆 Wins & Positive Signals
- [deal or account] — [what happened]

### 🚨 At Risk / Needs Attention
- [deal] at [account] ([value]): [reason] — Recommended ask: [specific ask]

### 📊 Pipeline Numbers
- Closed Won (QTD): $[N]
- Commit (negotiate+): $[N] — [deal list]
- Weighted Forecast: $[N]
- Best Case: $[N]

### 🎯 This Week's Focus
1. [priority]
2. [priority]
3. [priority]

### 🤝 Asks / Escalations
- [specific ask] — [context and urgency]
- (none)
```

Saved to: `<fieldkit_home>/archive/one-on-ones/YYYY-MM-DD-1on1-update.md`

---

## Slack Format (--slack)

Compact version without headers, all bullets:

```
*1:1 — [DATE]*
✅ Closed: [deal] $[N]
🚨 At risk: [deal] — stuck in [stage] [N]d — need [manager ask]
📊 Pipeline: Commit $[N] | Weighted $[N] | Best Case $[N]
🎯 Focus: [top 2 priorities]
🤝 Ask: [if any]
```
