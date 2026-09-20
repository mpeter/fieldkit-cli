# Update — mid-session refresh

You've been in meetings or heads-down on work and signals have drifted since your
last context load. This is a **mid-session refresh, not a full start-of-day run** —
the brief root is the start-of-day run, and `ops/week-start.md` is the Monday
planning pass.

Keep your task list and memory current. Two modes:
- **Default:** Sync tasks against pursuit files and Backstory, triage stale items, check memory gaps
- **`--comprehensive`:** Deep scan Gmail, Calendar, and engagement signals — flag missed todos, surface new contacts

Previously a standalone skill named `update`. Folded here under D1 Wave 5 per the
D4 fold mechanics — it is a brief cadence, so it lives with the brief.

## Gotchas

- **Stale vault signals** — this op is what refreshes them; run it before ops that read cached files
- **Trigger overlap with adjacent ops** — this is the mid-session refresh; the brief
  root is the full start-of-day run and `ops/week-start.md` is Monday planning

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Always surface generated output for review before any external send**

Groups needed: **fieldkit-sales** (Backstory signals). Pursuit files and contact memory are enumerated and read directly from disk (glob + native file reads).

## Default Mode

### 1. Load State

Read `TASKS.md` and `memory/MEMORY.md`. If missing, suggest `/start` first.
Read `config/accounts.yaml` directly.

### 2. Sync from Project Sources

Pull signals from the sources this project actually uses:

**Pursuit files** — enumerate with a glob:

```
ls accounts/*/pursuits/*.md
```

For each file path, read the content directly. Extract:
- New pursuits added since last refresh → offer to add Salesforce linkage and
  qualification-review actions to TASKS.md
- Stage changes in frontmatter (`sf_stage`) → flag if tasks don't reflect current stage
- Close dates within 30 days → ensure paper process / proposal tasks exist
- Native qualification `pending`/`unavailable` with no resolution task → surface
  the exact linkage, read, or evidence-review action as a candidate

**Backstory (People.ai):**
- `find_account` + `get_account_status` for each account
- New risks surfaced → offer to add as tasks
- Next steps from Backstory not in TASKS.md → offer to add
- Engagement decay (account going quiet) → flag for attention

**Backstory guardrail:** Backstory signals may become task items in TASKS.md only. Do NOT write Backstory synthesis into pursuit file bodies, activity logs, stakeholder maps, or `account.md`. Task items derived from Backstory should be framed as actions to verify ("Confirm with [name] whether..."), not as factual assertions.

**Salesforce pursuit frontmatter:**
- Compare `sf_stage` and `sf_close_date` in pursuit files against tasks
- If a pursuit moved to a new stage but TASKS.md has no corresponding action → flag

### 2b. Slack Signal Sweep

Sweep internal Slack for colleague activity about each active account.
Slack is internal only — this captures what your team is saying, not customer signals.

For each account in `config/accounts.yaml`, run a date-filtered search to keep noise low:

```bash
# Use after: filter — raw account keywords return thousands of results without it
slackcli search messages "<account_keyword>" after:<yesterday-ISO-date> --limit 20

# Also check account-specific channels for unread activity
slackcli conversations unread   # scan for account-named channels (team-<account>-*, proj-<account>-*)
```

Write results to the cache — overwrite `watchers/slack-signals.md` on each refresh run:

```
write watchers/slack-signals.md
  <aggregated results per account with timestamp header>
```

Cache format:
```markdown
# Slack Signals — [ISO timestamp] (covers last 48h)

## <Account Name>
- #team-<account>-core | @your-username | 2026-06-03: [message snippet]
- #proj-<account>-automation | @colleague | 2026-06-04: [message snippet]

## GlobalPay Inc
- (no recent activity in last 48h)

## <Another Account>
- #team-sf-ansible | @someone | 2026-06-04: [message snippet]
```

**Signal types to highlight in the cache (flag these explicitly):**
- Critsit channels (`critsit-*-<account>-*`) — active incidents
- Delivery concerns from SAs or CSAs
- Competitive mentions (competitor names alongside account)
- Stakeholder changes (departures, reorgs)

**Graceful failure:** If `slackcli` auth fails, skip and note
`(Slack unavailable — run 'slackcli auth list' to check tokens)` in the report.
Do not fail the whole refresh run.

See `src/fieldkit/skills/brief/references/slack-search-protocol.md`.

### 2a. Staleness Checks

Run these checks on every refresh invocation to surface data that has gone stale.

#### SF Data Freshness

For each pursuit file with a non-empty `sf_last_pulled` field:
- Parse the timestamp (ISO 8601 format: `2026-05-10T21:55:07Z`)
- Compute days since last pull vs. today's date
- Flag any pursuit where `sf_last_pulled` is **> 7 days ago** with: `⚠️ SF data stale (Xd) — [account]/pursuits/[filename].md`
- Flag any pursuit with `sf_last_pulled: ""` (empty) with: `⚠️ SF data never pulled — [account]/pursuits/[filename].md`

Aggregate and report:
```
SF staleness: X of Y active pursuits have data older than 7 days — run /sf-sync to refresh
```

#### Contact Memory Freshness

Enumerate contact memory files with a glob:

```
ls memory/**/contact_*.md
```

For each file path, read the file directly and extract:
- The `metadata.last_verified` field from frontmatter
- Compute days since `last_verified` vs. today's date
- Flag any contact where `last_verified` is **> 90 days ago** with: `⚠️ Contact stale (Xd) — [name] ([account])`
- Flag any contact with no `last_verified` field with: `⚠️ Contact never verified — [name] ([account])`

Aggregate and report:
```
Contact memory: X of Y contacts need verification (last_verified > 90 days or missing)
```

When contacts are flagged as stale, suggest running comprehensive scan to re-derive signals from Gmail cache, then update `last_verified` after confirming data is still accurate.

### 3. Triage Stale Items

Flag Active tasks that are:
- Past due date
- 30+ days old with no update
- Related to a pursuit that has been won, lost, or archived

Present each for triage: done? reschedule? move to Someday? remove?

### 4. Check Waiting On

Review Waiting On items:
- Past follow-up date → flag with account context
- Cross-reference against Backstory activity — did the person respond but the item wasn't cleared?
- Suggest follow-up language if overdue

### 5. Memory Gaps

For each task and pursuit reference, verify the entities are in account context:
- People mentioned in tasks → do they appear in `accounts/*/account.md` stakeholder maps?
- Pursuits referenced → do the pursuit files exist?
- Unknown shorthand → ask the user, save to auto-memory

### 6. Pursuit Compliance Check

Run pursuit-auditor validation against all pursuit files. The auditor reads the
files directly — pursuit files were already read in Step 2, so pass the cached
file list to avoid double enumeration.

For each file path already enumerated (excluding `gmail-intel.md`):
- Verify all 7 sf_* fields present: `sf_opportunity_id`, `sf_stage`, `sf_close_date`, `sf_arr`, `sf_owner`, `sf_next_steps`, `sf_last_pulled`
- Do not require a local scorecard. If `meddpicc` or `legacy_meddpicc` exists,
  treat it as historical evidence only and never calculate a current total or gap
- Flag any legacy `sf-opportunity-number` (hyphen form) fields; `sf_opportunity_number` (underscore) is canonical and valid
- Flag any hyphenated `sf-` field variants

Report compliance summary:
```
Pursuit compliance: X/Y files passing (Z errors)
```

If errors found, automatically run pursuit-auditor --fix to correct them, then re-audit and report the post-fix compliance count. List any errors that --fix could not resolve and require manual intervention.

### 7. Sync Google Tasks

Run `/task-sync` to push/pull changes between TASKS.md and Google Tasks.
This ensures both surfaces reflect the current state after all triage and sync steps.

### 8. Report

```
Update complete:
- Tasks: +X synced from pursuits, X triaged, X waiting-on items overdue
- Pursuits: X active, X with close dates within 30 days
- SF staleness: X of Y pursuits have data > 7 days old [or: all current]
- Contact memory: X of Y contacts need re-verification [or: all current]
- Compliance: X/Y pursuit files passing (Z errors)
- Slack: X accounts swept, cache written to slack-signals.md [or: Slack unavailable]
- Engagement: [account-level signals from Backstory]
- Memory: X gaps filled
```

## Comprehensive Mode (`--comprehensive`)

Everything above, plus deep activity scanning across communication channels.

### Scan Gmail Cache

Run the gmail-cache pipeline if not recently synced:

```bash
fieldkit gmail sync
fieldkit gmail account-tags
```

Then query for recent activity per account (last 7 days):

```bash
fieldkit gmail query account <account> --since $(date -d '7 days ago' +%Y-%m-%d)
```

Surface untracked action items from email threads:
- Commitments the user made ("I'll send that over by Friday")
- Questions asked but not answered
- Threads with no reply in 5+ days

### Scan Calendar

Use `gws calendar events list` to pull upcoming meetings for the next 5 business days.
Cross-reference against TASKS.md:
- Meeting with an account but no prep task → suggest running the `meeting` skill
- Recurring meeting with no active tasks for that account → flag as potential disengagement

### Engagement Health

Run decay analysis per account:

```bash
fieldkit gmail decay <account>
```

Surface accounts with declining engagement:
- No emails sent/received in 14+ days
- Compare against Backstory `get_recent_account_activity` signals
- Flag accounts that were active 30 days ago but have gone quiet

### Surface New Contacts

Surface frequent contacts not in stakeholder maps:

```bash
fieldkit gmail query blindspots <account> --min-messages 5
```

For each account, contacts active in Gmail but absent from account.md stakeholder map
are potential new stakeholders — suggest adding them.

### Report

```
Comprehensive update complete:
- Tasks: +X synced, X triaged, X waiting-on overdue
- Email: X untracked commitments found, X threads need reply
- Calendar: X meetings in next 5 days, X missing prep tasks
- Engagement: [decay alerts by account]
- Contacts: X new frequent contacts not in stakeholder maps
- Memory: X gaps filled
```

## Reference

For detailed scan logic and examples, read
`src/fieldkit/skills/brief/references/comprehensive-scan.md`.

## SF Next Steps

When you have signal that an opp's next step should change, follow the protocol in
`src/fieldkit/skills/brief/references/sf-next-steps-protocol.md`.
