# Brief Config

Maintains the four judgment-as-data config files the morning brief depends on:
`config/clocks.json`, `config/engines.json`, `config/people.json`, and
`config/watchlist.json`.

These hold operator judgment that no source system knows — a commitment made in a
hallway, an estimated burn percentage, a deliberate decision to watch a quiet
account. The brief reads them; nothing else writes them.

**Working directory:** the fieldkit workspace root.

Previously a standalone skill named `handoff`, which collided with the unrelated
session-continuity skill of nearly the same name and described a capability it did
not have. Folded here under D1 Wave 5 — this is brief configuration, so it lives
with the brief.

## Gotchas

- **Never run a brief refresh from within a config edit** — the brief calls live
  Salesforce; config maintenance is config-only and read-only against SF.
- **Never write to `scratch/out/brief.json`** — it is generated output from
  `collect.py`, not a source file. Writes are overwritten on the next refresh.
- **`est: true` on engines must be preserved** — when updating `pct_burned`, keep
  the `est` flag intact. It signals the value is an operator estimate, not a PSA
  hours feed.
- **Clocks are human commitments, not SF close dates** — close dates live in
  Salesforce. Add a clock only when a person made a real commitment or deadline.
- **Today in `TASKS.md` is a Google-Tasks-managed region** — never write it
  directly. `triage.py`'s regex write strips gtask anchors, which `/task-sync`
  then re-adds as duplicate or orphaned Google Tasks.

## Constraints

- **Only write on the operator's explicit word** — propose the change, wait for
  confirmation, then write.
- **After any config write**, print the updated entry and confirm the file was
  written.
- **Never modify the `watchlist.json` opportunities list** — that is a deliberate
  AE decision, not a config edit.
- At end-of-day close, run `/task-sync` so the day's completions and additions
  reconcile both ways. Propose new Today items as `fieldkit` list adds, never as
  direct `TASKS.md` edits.

## Operations

### triage

Generate Today candidates, then route them through the Google-Tasks-managed
region. The `fieldkit` list is the source of Today.

```bash
python3 tools/brief/triage.py --dry-run   # print candidates without writing — the only safe mode
```

Reads `scratch/out/brief.json` and proposes 3–6 prioritized Today items from:
whispers → clocks ≤7d → overdue SF deals → ball-ours people → engines near end →
top active task per account.

Propose each candidate as a `fieldkit` Google Tasks list add, then run
`/task-sync` to reconcile it into Today. Never use `triage.py --force` — that
direct overwrite is the managed-region violation the Constraints forbid.

### clock add `"<label>" <YYYY-MM-DD> [<time>] "<state>" <account>`

Append a dated commitment to `config/clocks.json`:

- `label` — short description (e.g. "Brooke Phillips review")
- `date` — ISO 8601 (YYYY-MM-DD)
- `time` — optional (HH:MM, 24h)
- `state` — one sentence on what is owed, or what this gates
- `account` — an account slug; see `config/watchlist.json` for valid values

Read → append → write back → confirm with `days_until` computed.

### clock remove `"<label>"`

Remove the matching clock from `config/clocks.json`. Show the entry before
removing; confirm the deletion.

### clock list

Print all clocks with computed days-until, using today's date as reference.

### people add `"<name>" <email> <account> "<why>"`

Add a person to the silence watchlist in `config/people.json`.

### people remove `"<email>"`

Remove by email match. Show the entry before removing.

### watchlist

Print the current watchlist with note-file status (present/missing) and native
qualification availability. A local watchlist read does not fetch ClosePlan:
linked pursuits are `pending (live ClosePlan fetch required)` and missing linkage
is `unavailable`.

### engines update `<key> <field> <value>`

Update an engine field — `pct_burned`, `runway_date`, `milestone`, or `evidence`.
Read `config/engines.json`, find the engine by its `key` field, update, write
back, and print the updated entry.

### status

Quick summary without a live refresh. Read all four config files and print:
clocks count + nearest, engines count + highest burn %, people count, watchlist
count. Check which note files referenced by `watchlist.json` exist versus are
missing.

## File paths

```
config/clocks.json
config/engines.json
config/people.json
config/watchlist.json
scratch/out/brief.json   ← read-only; run collect.py to refresh
TASKS.md                 ← Today is the Google-Tasks-managed region; reconcile via /task-sync
```
