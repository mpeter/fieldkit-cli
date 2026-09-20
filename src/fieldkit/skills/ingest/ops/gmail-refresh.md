# gmail-refresh Skill

Pull fresh Gmail signals into gmail-intel.md files in one command.
Calls existing gmail-cache commands in sequence — does not modify any scripts.

Trigger with: `/gmail-refresh`, "refresh gmail", "sync gmail", "update email intel",
"pull email signals", "gmail-refresh", "retag emails", "skip sync".

---

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today; this skill reads from cached files
- **Trigger overlap with adjacent skills** — check that you need this specific skill and not a closely named one (e.g. the `contract` skill's `src/fieldkit/skills/contract/ops/contract-check.md` vs `src/fieldkit/skills/contract/ops/contract-extract.md`)

## Constraints

- **Never write to account files without explicit confirmation**
- **Do not modify pursuit frontmatter mid-workflow** — only write at designated save steps
- **Always surface output for review before sending externally**

## Modes

**full** — sync → account-tags → people → enrich-pursuits
**core** — sync → account-tags → enrich-pursuits (skip people index)
**refresh-only** — account-tags → enrich-pursuits (skip Gmail API sync, use existing DB)

If the user doesn't specify a mode, default to `core`.
If the user says "skip sync" or "just retag", use `refresh-only`.
If the user says "full" or "with enrichment", use `full`.

---

## Step 1 — Environment Check

Before running any sync step, verify the environment is ready.

**For sync modes (full, core):** run `fieldkit doctor google` and check that Gmail OAuth
credentials are configured (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in output).
If missing, stop and report:

> Gmail OAuth credentials are not configured. Run `fieldkit init` to add them, or use
> `refresh-only` to retag without hitting the Gmail API.

**For refresh-only mode:** verify the database exists by running `fieldkit version --features`
and checking that `gmail cache` shows a DB path. If not found, stop and report:

> No gmail cache DB found. Run `core` or `full` mode to sync from Gmail first.

---

## Step 2 — Dispatch by Mode

All commands are invoked via the `fieldkit` CLI (not via python -m).
The db path is resolved automatically from `~/.config/fieldkit/config.yaml` (`gmail_db` key).

### full mode

Run all five pipeline stages in order:

```bash
fieldkit gmail sync
fieldkit gmail account-tags
fieldkit sync
fieldkit gmail enrich-pursuits
```

### core mode (default)

Run the three core stages:

```bash
fieldkit gmail sync
fieldkit gmail account-tags
fieldkit gmail enrich-pursuits
```

### refresh-only mode

Skip Gmail API sync; retag and apply intel from existing DB:

```bash
fieldkit gmail account-tags
fieldkit gmail enrich-pursuits
```

---

## Error Handling

- **OAuth credentials missing:** Stop before running sync. Offer `refresh-only` as an alternative.
- **gmail sync exits non-zero (exit 2 = auth, exit 1 = API error):** Do not continue to account-tags — the DB state is unknown. Report and stop.
- **account-tags exits non-zero:** Log the error, continue to enrich-pursuits. Tagging is additive; existing tags remain valid.
- **people exits non-zero:** Log the error, continue to enrich-pursuits. Not fatal for intel application.
- **enrich-pursuits exits non-zero:** Log the error. Report at end.

Do not abort the full pipeline on a single non-sync failure. Collect errors and report all at the end.

---

## Output Format

After the pipeline completes, present a summary:

**Gmail Refresh Complete — [mode] — [timestamp]**

- Sync: ✅ complete | ❌ error (message) | ⏭ skipped (refresh-only)
- Account tags: ✅ complete | ❌ error (message)
- People index: ✅ complete | ❌ error (message) | ⏭ skipped (core/refresh-only)
- Pursuit enrichment: ✅ complete | ❌ error (message)

gmail-intel.md files updated: list files printed by enrich-pursuits.

Errors (if any): one line per failure with command name and exit message.

---

## Related Skills

- `meeting`'s `src/fieldkit/skills/meeting/ops/account-pulse.md` — morning health check; benefits from up-to-date gmail intel
- `meeting`'s `src/fieldkit/skills/meeting/ops/qbr-prep.md` — pulls account activity; runs better after gmail-refresh populates signals
- `meeting` — pre-meeting brief; uses gmail signals for recent contact context
