---
name: brief
description: >
  The AE OS "what needs my attention" verb — daily and weekly orientation. Refreshes live
  sources (SF, Gmail), renders the brief, syncs tasks, and synthesizes what happened and
  what to do about it. Use when the operator types /brief or asks what needs attention or
  what to focus on today — "orient me", "morning", "start my day" — and as the first
  command of a session. Also covers week start ("start the week", "Monday morning", "set
  up my week"), week close-out ("end of week", "Friday wrap", "close out the week", "prep
  for next week"), and mid-session refresh ("catch me up", "what's changed", "what did I
  miss", "quick update"). Also maintains the config it reads — clocks, engines, people,
  watchlist: add a dated commitment, update an engine burn percentage, check config status.
metadata:
  opencode/slash: "true"
  argument-hint: "[--no-refresh | --fresh | push]"
  category: ops
---

# /brief — refresh, synthesize, triage

**Working directory:** the fieldkit workspace root.

The one command that turns raw sources into the operator's attention list.
SF is truth — brief queries it live; local files are never deal-truth.

Renamed from `pulse` (D1 skill taxonomy, Wave 2).

Groups needed: none for the daily run itself (the pipeline is CLI-native). The
on-demand ops do need them — **fieldkit-sales** (Backstory signals, used by
[`ops/update.md`](ops/update.md) and [`ops/week-start.md`](ops/week-start.md)) and `gws tasks` (Google Tasks
sync, used by [`ops/week-start.md`](ops/week-start.md) and [`ops/week-end.md`](ops/week-end.md)). Declared here because
`docs/dependency-map.md` is generated from root `SKILL.md` files only, so an op's
group need is invisible to it unless the root says so.

## On-demand ops

Read the relevant file when the request matches. The root handles the daily
start-of-day run; these cover the other cadences.

- [`ops/week-start.md`](ops/week-start.md) — full start-of-week orchestration (Monday morning):
  `fieldkit sync --sf`, pipeline review, per-account snapshots, engagement and
  Backstory risk checks, then confirming the week's top-5 priorities into TASKS.md.
  Use for "start the week", "Monday morning", "set up my week", "begin my week".
- [`ops/week-end.md`](ops/week-end.md) — end-of-week close-out (Friday afternoon): SF hygiene,
  follow-up audit, done-this-week summary, lessons learned, the manager 1:1 draft,
  and the TASKS.md reset for next week. Use for "end of week", "Friday wrap",
  "close out the week", "prep for next week", "EOW checklist".
- [`ops/update.md`](ops/update.md) — mid-session refresh, **not** a full start-of-day run: syncs
  tasks against pursuits and Backstory, sweeps Slack, runs staleness checks,
  triages stale items. `--comprehensive` adds a deep Gmail/Calendar scan. Use for
  "catch me up", "what's changed", "what did I miss", "quick update",
  "refresh my memory", "sync my context".
- [`ops/brief-config.md`](ops/brief-config.md) — maintaining the four judgment-as-data config files the
  brief reads (`clocks.json`, `engines.json`, `people.json`, `watchlist.json`):
  adding a dated commitment, updating an engine's burn percentage, managing the
  silence watchlist, or printing config status.

`week-start`, `week-end` and `update` were standalone skills until D1 Wave 5,
when they were folded here per the D4 fold mechanics. Their top-level
directories are deleted — R25, no shim and no alias — so these op files are the
only copy. Shared protocol references live in `references/`.

## Context awareness (check before running)

Before invoking the brief pipeline, check whether this is a **follow-up** in a
session that already has today's edition:

- If `scratch/out/brief.json` exists **and is dated today**, and the operator is
  asking a follow-up ("what about that account?", "re-rank Today", "who's gone
  quiet?"), **read the existing brief.json and answer from it — do not re-run
  the pipeline.** Re-pulling live SF/Gmail for a follow-up is wasted
  round-trips and can flap the attention list mid-session.
- Only run the full pipeline when there is **no** today-dated brief.json, or
  the operator wants fresh data (see the escape hatch below).

**Force-fresh escape hatch — the operator can always demand latest.** If the
operator says any of "pull latest", "re-pull", "fresh", "actually refresh it",
or types `/brief --fresh`, **ignore the cache and run the full live refresh
even if today's brief.json exists.** A stated wish for current data always
beats the cache. When in doubt about whether "again" means reuse or refresh,
ask — or default to fresh, since the cost is round-trips, not correctness.

## Invocation patterns

### /brief (default)
```bash
tools/brief/pulse
```
Refreshes every watchlist opp from live SF + syncs Gmail, then collects,
renders to `scratch/out/` (morning-sheet.html, email-plain.html,
slack_blocks.json, brief.json).

If the workspace has no `tools/brief/pulse` wrapper, the CLI-native path is:
```bash
fieldkit sync             # gmail sync → account-tags → enrich-pursuits → ingest → watchers
fieldkit brief generate   # alerts + calendar + pipeline review; --pipeline-only for the bare synthesis
```

### /brief --no-refresh
```bash
tools/brief/pulse --no-refresh
```
Skip the live refresh (offline, or sources refreshed minutes ago).

### /brief --fresh — force a full live refresh, ignore any cached edition
Same command as the default brief; the point is intent: run the full live
refresh even when today's `brief.json` already exists. `--fresh` is the
opposite of `--no-refresh` — one guarantees a re-pull, the other guarantees no
pull.

### /brief push — send the edition to the bus
```bash
python3 tools/brief/push_bus.py
```
Pushes `brief.json` to the morning-email / GAS surfaces. Run after brief when
the operator wants surfaces updated now.

## Sync tasks first — pull mobile edits

Before synthesizing, run **`/task-sync`** (Google-wins reconcile of the
`fieldkit` list ⇄ TASKS.md managed region). The operator may have added or
completed tasks on their phone overnight; this makes the morning Today reflect
reality. The managed region is sourced from Google Tasks — brief never writes
raw items into it; anything brief wants on Today it proposes in chat, and the
operator (or /sweep) promotes it into the `fieldkit` list.

## After running — synthesize, don't dump

Read `scratch/out/brief.json` and present to the operator, in order:

1. **Whispers that fired** (clock/drift/silence rules) — these are the point.
2. **Today** — the synced `fieldkit` managed region; flag anything you'd
   re-rank and why.
3. **Unanswered Slack threads** — if `watchers/slack-thread-alerts.md` has
   entries, surface account, channel, thread age, and recommended action.
4. **Staleness flags** — dead SF session, old gmail cache, stale pursuit
   frontmatter. Never present stale data as current; say its age.

For today's external meetings, hand off to the `meeting` skill rather than
duplicating prep here.

## Failure modes

- `SF session dead` → tell the operator: `fieldkit auth sf`
  (brief continues on cache with age flags — degraded, not broken).
- `REFUSING TO RENDER: brief.json is Nd old` → rerun the full brief (the
  collect step regenerates brief.json).
- Gmail sync failure → non-fatal; note the cache age in the synthesis.

### Tool bug vs operational signal — RAISE vs SKIP

When a step misbehaves, classify before reacting:

- **RAISE** (`fieldkit issue create`, after checking `fieldkit issue list
  --status open` for duplicates): a command crashes, exits non-zero without
  useful output, prints raw Python literals (`None`/`False`/`[]`), emits
  duplicated or malformed content, or shows wrong date fields. These are tool
  bugs — the brief is the earliest place they surface.
- **SKIP** (report as an operational signal in the synthesis, never as a bug):
  low engagement scores, stalled deals, empty watcher output on a quiet day,
  auth failures (those go to the operator per the failure modes above).

Misfiling an operational signal as a bug pollutes the tracker; swallowing a
crash as "degraded mode" hides a real defect. Classify every anomaly as one
or the other — never both, never neither.

## Constraints

- **Writes only to** `scratch/`, `.cache/`, and the Today section of TASKS.md —
  never pursuit notes, scores, or account notes.
- **Never edit `sf_*` frontmatter by hand** — that is sync-script territory.
- **Current qualification is live ClosePlan state** — brief never reads historical
  local scores as current. A linked pursuit is `pending (live ClosePlan fetch
  required)` and an unlinked or failed read is `unavailable`; use `/grill` for the
  exact read-only question review.
- **Never present stale data as current** — always state cache age when refresh
  fails; a failed fetch is a RED flag, not a skip.
- **Synthesize, don't dump** — whispers → Today → Slack threads → staleness
  flags, in that order.
