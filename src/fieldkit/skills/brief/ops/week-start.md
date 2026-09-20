# Week Start

Complete start-of-week setup in 3–5 minutes.
Refreshes everything, surfaces risks, and sets confirmed priorities.

Runs on Monday morning, or whenever the week begins. This is the weekly
counterpart to the daily brief: the brief orients you for today, this orients
you for the week and sets confirmed priorities.

Previously a standalone skill named `week-start`. Folded here under D1 Wave 5
per the D4 fold mechanics — it is a brief cadence, so it lives with the brief.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent ops** — this is the Monday setup; `ops/week-end.md`
  is the Friday close-out and the brief root is the daily start-of-day run

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Options

- `--skip-snapshots` — skip per-account snapshot generation (faster; skip if already done)
- `--account NAME` — run account snapshot for one account only instead of all

Routes needed: **fieldkit-sales** (Backstory MCP exception) and `gws tasks` (Tasks sync).
Vault markdown files are read directly from disk (native file reads).

---

## Step 1: Full data refresh (including Salesforce)

```bash
fieldkit sync --sf
```

This runs: gmail sync → account-tags → enrich-pursuits → ingest → watchers → sf listview --all.
Report completion status and any step failures.

---

## Step 2: Pipeline review

```bash
fieldkit pipeline --no-llm
```

Read output. Surface:
- Deals in negotiate+ with close dates within 90 days
- Native qualification state (`pending` for linked pursuits needing a live
  ClosePlan read; `unavailable` when no Opportunity link exists)
- Highest-risk deal per account
- Any deals that crossed a stage gate this week

The pipeline output does not prove a qualification pass. Offer `/grill` for an
exact read-only ClosePlan question review on the deal that needs attention.

---

## Step 3: Per-account snapshots (skip if --skip-snapshots)

For each account in `config/accounts.yaml` (or the one specified with --account):

Invoke the `meeting` skill's `src/fieldkit/skills/meeting/ops/account-snapshot.md` logic for [account].

Save to `accounts/<account>/meetings/YYYY-WNN-weekly-snapshot.md`.

Report: "N account snapshots generated."

---

## Step 4: Engagement health

```bash
fieldkit pursuit projects
```

Surface:
- Projects expiring within 30 days → flag as 'needs renewal conversation this week'
- Zombie projects (past end date) → flag as 'needs formal closeout call'
- Any account with score < 60 from Backstory → flag as 'low engagement — needs attention'

---

## Step 5: Backstory risk check

Already covered by `fieldkit sync --sf` watchers in Step 1.
Read `<fieldkit_home>/watchers/backstory-alerts.md` for accounts with score drops.
Any account with score < 60: "flag as 'needs attention this week.'"

---

## Step 6: Set week priorities

Present a suggested priority list (top 5) derived from:
- Close dates within 30 days (ranked by urgency)
- Deals stuck in stage > 21 days (ranked by days stalled)
- Projects expiring within 30 days
- Backstory scores below 60
- TASKS.md Active items carried from last week

Ask: "Here are your suggested top 5 priorities for this week. Edit and confirm — I will add them to the Today section of TASKS.md."

Write confirmed priorities to the **Today** section of TASKS.md.

---

## Step 7: Sync tasks

Run `/task-sync` logic: push TASKS.md Today section to Google Tasks.

---

## Output Format

```markdown
## Week of [DATE]

### 🔄 Data Refreshed
SF: N pursuit files updated | Gmail: synced | Transcripts: N ingested | Watchers: N alerts

### 📊 Pipeline Summary
Commit (negotiate+): $N | Weighted: $N | Deals closing this month: N

### 🏗️ Project Alerts
- [project] at [account]: ZOMBIE — needs closure call
- [project] at [account]: EXPIRING in N days — schedule renewal conversation

### 📉 Backstory Alerts
- [account]: score N/100 — engagement low — escalate this week

### 🎯 This Week's Priorities (confirmed)
1. [priority]
2. [priority]
3. [priority]
4. [priority]
5. [priority]
```
