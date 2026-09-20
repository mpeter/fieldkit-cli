---
name: start
description: >
  You're setting up a fieldkit workspace for the first time, or need to reinitialise it after
  a fresh install. Creates TASKS.md, loads account and pursuit context, and bootstraps working
  memory so the session is fully primed from the first request.
  Trigger with "set this up", "first time running", "bootstrap my workspace", "I'm new to this",
  "fresh install", "initialise fieldkit", "start from scratch", "set up fieldkit",
  "initialize my workspace", "get me started".
metadata:
  opencode/slash: "true"
  category: ops
---

# Start Command

Initialize the task and memory systems for this workspace.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Steps

### 1. Check What Exists

Check the working directory for:

- `TASKS.md` — task list
- `config/identity.yaml` — operator identity (role, company, accounts, motions)
- `config/accounts.yaml` — account registry
- `accounts/*/account.md` — account context files (Acme Corp, GlobalPay Inc, Midwest Insurance)
- `accounts/*/pursuits/*.md` — active pursuits
- `memory/system/lessons-learned.md` — lessons and observations

### 2. Create What's Missing

- **No TASKS.md?** Create with standard template (see task-management skill)
- **No memory/system/lessons-learned.md?** Create with format: `## YYYY-MM-DD — <account> — <topic>` then 3-5 bullet observations

### 3. Orient the User (If Already Initialized)

If TASKS.md and memory exist:

```
System ready. Your tasks and memory are loaded.
- `/brief`'s update op (`ops/update.md`) to sync tasks and check memory
- `/brief` --comprehensive for a deep scan across Gmail, Calendar, and Backstory
```

Stop here — no need to re-bootstrap.

### 4. Load Account Context (First Run Only)

Groups needed for this step: **fieldkit-sales** (Backstory in Step 6). Account and pursuit files are read directly from disk (native file reads).

Read config files directly (YAML):
1. `config/identity.yaml` — operator identity, role, company, engagement motions
2. `config/accounts.yaml` — account list, domains, team members

Read account files directly:
3. For each account: read `accounts/<account>/account.md`
   — stakeholder maps and org charts; any local MEDDPICC values are historical
   
   Also check for dossier: `accounts/<account>/dossier/dossier.md`
   If present and recent (< 14 days), use it to seed context — contracts, email intelligence, strategic priorities are pre-synthesized.

Enumerate pursuits with a glob:
4. `ls accounts/*/pursuits/*.md`
   For each path, read the file — note stages, close dates, Salesforce linkage,
   and current next steps; never interpret historical local scores as current

Build a working picture: which accounts are active, what pursuits are in play, who the key stakeholders are.

### 5. Bootstrap Task List

Ask the user about their current task list. For each task, decode shorthand using loaded account context:

```
Task: "Send PSR to Todd re: Phoenix blockers"

I see terms I want to confirm:
1. PSR — What does this stand for?
2. Todd — <contact-name> (VP Engineering, Acme Corp) from stakeholder map? Or someone else?
3. Phoenix — The Platform Modernization pursuit at Acme Corp? Or different?
```

Skip terms already covered in account.md files, stakeholder maps, or accounts.yaml. Use what's already in the system before asking.

### 6. Enrich from Live Sources

Pull live context from available tools. This is not optional — it's how the system gets current.

**Backstory (People.ai):** For each account in `config/accounts.yaml`:
- `find_account` → get `peopleai_account_id`
- `get_account_status` → risks, next steps, trending topics
- `get_recent_account_activity` → last 30 days of engagement

**Gmail cache pipeline:** Run a sync if not recently run:
```bash
fieldkit gmail sync
fieldkit gmail account-tags
```

**Pursuit files:** Cross-reference pursuit frontmatter against Backstory signals:
- Stage mismatches (pursuit says "proposal" but Backstory shows no recent activity)
- Qualification evidence needs already documented in current next steps or meeting notes
- Close dates within 30 days

Present findings grouped:

- **Ready to add** (high confidence) — offer to add to TASKS.md or memory
- **Needs clarification** — ask the user
- **Low signal / unclear** — note for later

### 7. Write Memory

From everything gathered, update:

- `memory/system/lessons-learned.md` — append a dated bootstrap entry with key context discovered
- Auto-memory files at `~/.claude/projects/.../memory/` — save user profile, preferences, decoded shorthand

Do NOT modify `CLAUDE.md` — that is the operating manual, not working memory.

**Memory guardrail:** Backstory-derived signals written to `memory/system/lessons-learned.md` must be labeled `[Backstory — unverified]`. Do not present Backstory synthesis as confirmed fact in memory entries. Only write to memory what the user has explicitly confirmed or what comes from local files (account.md, pursuit frontmatter, meeting notes).

### 8. Report

```
Workspace ready:
- Identity: <role> @ <company> — <N> accounts (<list>)
- Tasks: TASKS.md (X items across Today/Active/Waiting On)
- Accounts: 3 active (Acme Corp, GlobalPay Inc, Midwest Insurance)
- Pursuits: X active across accounts (X in proposal+, X pre-pipeline)
- Engagement: [any decay alerts or stale accounts from Backstory]
- Memory: lessons-learned.md initialized
```

## Reference

For comprehensive scan workflow and detailed bootstrap steps:

```
Read skills/start/bootstrap-details.md
```
