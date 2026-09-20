# Bootstrap Details

## Live Source Enrichment

After task list decoding, pull data from all available sources:

- **Backstory (People.ai):** `find_account` → `get_account_status` and
  `get_recent_account_activity` for each account in `config/accounts.yaml`
- **Gmail cache pipeline:** Run `python -m fieldkit gmail sync` then `python -m fieldkit gmail account-tags`
  to tag threads by account domain matching
- **Pursuit files:** Read `accounts/*/pursuits/*.md` for stages, Salesforce
  linkage, close dates, and next steps. Treat any local MEDDPICC values as
  historical evidence only.
- **Account files:** Read `accounts/*/account.md` for stakeholder maps and
  org context

Build a summary of account health, active pursuits, and engagement signals.
Present findings grouped:

- **Ready to add** (high confidence) — offer to add directly to TASKS.md
  or memory
- **Needs clarification** — ask the user
- **Low signal / unclear** — note for later

## Notes

- If memory is already initialized, `/start` just reports status
- Nicknames are critical — always capture how people are actually referred to
- Account context lives in `accounts/*/account.md`, not in separate people/project files
- The auto-memory system at `~/.claude/projects/.../memory/` persists across sessions
- `memory/system/lessons-learned.md` is the project-local memory — append dated entries
- `CLAUDE.md` is the operating manual, never managed by this skill
- If a source isn't available (e.g., Backstory auth expired), skip it and note the gap
- Memory grows organically through conversation after bootstrap
