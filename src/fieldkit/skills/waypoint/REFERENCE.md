# waypoint — Reference (loaded on demand)

Lookup material the core `SKILL.md` points to. Read the section you need;
don't load this file for gate checks or pure synthesis iterations. The
decision rules (when to use a cheap model, assistant auth behavior, read-only
guardrails) live in `SKILL.md` — this file is command syntax + calibration
tables only.

## Exploration-verb command templates

These examples use a Google Workspace CLI as a worked illustration — swap in
whatever document-access tool your environment actually provides (a
different Drive CLI, a wiki search API, a file-share mount).

**Document-store search** (names are usually the reliable index; full-text
search is noisier). Search terms often come from documents or assistant
output, so never splice one into shell or query syntax. Read it through a
quoted heredoc and let `jq` build the query, escaping `\` and `'` for the
Drive query language:
```bash
term=$(cat <<'TERM'
<TERM>
TERM
)
params=$(jq -nc --arg t "$term" '([39] | implode) as $sq
  | {q: ("name contains " + $sq + ($t | gsub("\\\\"; "\\\\") | gsub($sq; "\\" + $sq)) + $sq + " and trashed=false"),
     pageSize: 20, fields: "files(id,name,mimeType,modifiedTime)", orderBy: "modifiedTime desc"}')
<your-drive-cli> drive files list --params "$params"
```

**Folder walk** (the highest-yield expansion move). Use only IDs taken from a
files listing, and only if they match `^[A-Za-z0-9_-]+$`:
```bash
<your-drive-cli> drive files list --params '{"q":"'\''<FOLDER_ID>'\'' in parents and trashed=false","pageSize":30,"fields":"files(id,name,mimeType,modifiedTime)","orderBy":"modifiedTime desc"}'
```

**Read a doc** (summarize into map/work product; don't paste bulk content):
```bash
<your-drive-cli> docs documents get --params '{"documentId":"<ID>"}'
<your-drive-cli> sheets spreadsheets values get --params '{"spreadsheetId":"<ID>","range":"A1:Z50"}'
```
Check `mimeType` from the files list before choosing the read verb — a
sheet-read call against a Doc (or vice versa) typically 404s.

**AI assistant** (program/policy waypoints — if your organization has one
configured):
```bash
fieldkit shadowbot query <<'PROMPT'
<question — ask for document names + links>
PROMPT
```

## Model & token economics

The phases have very different judgment density — pay for reasoning only
where it changes the outcome:

| Work | Model tier | Why |
|---|---|---|
| The loop session itself | Mid-tier | EXPLORE/EXECUTE are read-digest-write; a mid-tier model handles them at a fraction of a top-tier model's cost. |
| Bulk doc/sheet/folder reads | Cheap/fast, as a subagent | Payload digestion, no judgment (see the SKILL.md bulk-read rule). |
| NARROW candidate scoring | Strong, once per mission | Goal selection is the one judgment-dense step. Delegate to a single subagent with the map + derived goals; it returns scored candidates + recommendation. The loop writes the Mission block and announces — the operator override in chat is the real safety net. |
| Your top-tier/flagship model | Only on demand | Reserve for design, oversight, and judgment calls outside the loop. |

If the loop is already running on your strongest model, skip the NARROW
subagent and score inline — don't pay for the same reasoning twice.

## Slide/deck work products — EXECUTE handoff mechanics

When the chosen work product is deck content, markdown is only the
intermediate: the final EXECUTE iteration hands off to the `exec-review-deck`
skill (citation gate, value-realization framing, de-AI pass, brand
adherence) and produces a **new, private staging deck** in whatever
presentation tool you use, created only after the operator approves it, in
the operator's own private location, with its sharing confirmed before any
content goes in. Hard rules (also enforced by the SKILL.md
guardrails): NEVER edit an existing/live deck, never share the staging file,
never touch permissions — the operator reviews the staging deck and copies
slides into the live deck themselves. Speaker notes carry the citations;
anything unverified stays flagged on the slide, not silently dropped.

## Work-product index

`scratch/out/waypoint/INDEX.md` is the single lookup, in three status
buckets:

- **In progress** — row added at NARROW.
- **Ready for review** — row moved here at DONE. This is the pinned review
  queue at the top of the file: every completed draft awaiting the
  operator's sign-off.
- **Reviewed** — archived by account once the operator signs off.

Rows read `date · account · [link] — one-liner`.

**Supersession, not accretion.** A new work product covering a story an
existing pack already tells absorbs what is still true and retires the old
row, marked `↪ superseded by [new]`. Stacked addenda on old packs are a
smell; current truth should never need archaeology.

**Freshness.** Every work product carries `verified_as_of:` (the newest
underlying source-read date) in frontmatter. Reviewing a pack whose sources
have since moved starts with a re-verify tick, not a read.

## Goal shapes

Calibration for NARROW, not a menu: a technical-overview pack anchored to a
named product precedent; a services-upside sizing brief built from
usage/drawdown data; a competitive-displacement approach built from a
peer-account pattern; a renewal-risk brief that bridges into an expansion
ask.

## Loop cadence

20–30 min during EXPLORE, because document stores change on human
timescales. EXECUTE iterations can run tighter (10–15 min) since they are
synthesis-bound rather than waiting on the world.
