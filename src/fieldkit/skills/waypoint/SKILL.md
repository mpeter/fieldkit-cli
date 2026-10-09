---
name: waypoint
description: >
  A document-intelligence crawler that converges on a deliverable. Each run is one bounded
  iteration of a three-phase mission: EXPLORE (search your document store, growing a
  persistent waypoint map), NARROW (when discovery dries up, self-select ONE strategic goal
  the map best supports), and EXECUTE (build that work product iteration by iteration until
  its done-criteria are met). Use when the operator types /waypoint, runs it in a loop, or
  asks to keep building the account document map — even if they just say "keep digging" or
  "find more account docs".
metadata:
  category: product
---

# waypoint — discovery that narrows into a finished work product

A waypoint is a document whose location you've confirmed and whose value to
your stated goals you've written down. A map is not the end state — the
mission converges: explore while exploration pays, then pick the single most
valuable thing the map makes possible, then build it. Each run of this skill
executes ONE bounded iteration of whichever phase the mission is in, persists
state, and stops. Designed to be safe under a loop.

It needs a tool that can search and read your account documents (Google
Drive, a file share, a wiki); the exploration verbs below are generic.

## State (persistent across iterations)

- **Map:** `scratch/waypoints/MAP.md` — the living waypoint map. Sections:
  `## Mission` (phase + goal, see below), one section per waypoint kind
  (`## Money docs` — pricing, usage, contracts, SOWs; `## Program kits`;
  `## Peer precedents`; `## Expansion leads`; `## Archives`), `## Queue`
  (unexplored hops, ranked), `## Explored log` (dated one-liners).
- **Work product:** recorded in `## Mission`, under `scratch/out/waypoint/` — a
  dedicated folder so deliverables never mix with other scratch exhaust. Name
  `<account>-<slug>-<date>.md`.
- **Index:** `scratch/out/waypoint/INDEX.md` — In progress / Ready for
  review / Reviewed buckets. A new pack that retells an existing story
  supersedes the old row instead of stacking an addendum; every pack carries
  `verified_as_of:`. Bucket and row rules: `REFERENCE.md` → "Work-product
  index".
- **Raw output:** `scratch/waypoints/*.json` — sweep results, kept for
  reference.
- If `MAP.md` does not exist, seed it from any prior waypoint-map export you
  have, normalizing field names to match the schema below. If none exists,
  start fresh from whatever account notes and pursuit files already link out
  to external documents.

The `## Mission` block at the top of MAP.md is the state machine:

```
## Mission
phase: EXPLORE | NARROW | EXECUTE | DONE
strategic_goals:        # derived at NARROW from live sources, in the skill's own words
  - <goal>
goal: <one sentence, set at NARROW; "-" before that>
work_product: <scratch/out/ path; "-" before NARROW>
done_criteria:
  - [ ] <checkable criterion>
  - [ ] ...
progress: <one line, updated every EXECUTE iteration>
```

Everything lives in `scratch/` (gitignored). **Never commit any of this.**

## Phase logic — what one iteration does

Read `## Mission` first. Run exactly one phase's iteration, update state, stop.

### Phase EXPLORE — grow the map

1. Take the top 1–2 items from `## Queue` and explore them (verbs below).
2. Write findings back: new docs → their kind section with **ID, date,
   one-line "why it matters"** tied to a pursuit, play, or goal (no entry
   without a why). Deal-changing finds → also `## Expansion leads`. New
   hops → `## Queue`, ranked: money docs > program kits > peer precedents >
   archives.
3. Append to `## Explored log`: `YYYY-MM-DD — <hop> → <n> waypoints, <n>
   leads, <n> queued`.
4. **Convergence check (every iteration, honestly):** flip `phase: NARROW`
   when ANY of these hold —
   - the queue is empty and a freshness pass (rotating account name-sweep,
     7-day modifiedTime diff) found nothing new;
   - the last 3 iterations each added ≤1 money-doc or program-kit waypoint;
   - the map already contains everything a specific high-value work product
     needs (don't keep exploring past sufficiency — that's stalling).

### Phase NARROW — self-select the goal (one iteration, done once)

1. **Derive the strategic goals first — do not assume them.** Read whatever
   this workspace keeps as a statement of operator intent (a goals or
   territory-plan file in `notes/`, if you keep one), the `sf_*` blocks,
   native ClosePlan qualification for the few pursuits a candidate goal
   hinges on (`fieldkit sf meddpicc <opp-id>`, within the call budget), **and
   the note body's reversal/status banners + newest
   `accounts/*/meetings/` entry** across `accounts/*/pursuits/*.md` — a note
   body can record that the customer killed a deal while `sf_*` still reads
   an earlier stage; trust the banner, not just the frontmatter. Also
   `notes/<account>.md` expansion surfaces, your task ledger's
   Active/Waiting-On section, and the map itself. From these, write 2–4
   strategic goals in your own words into a `strategic_goals:` list in
   `## Mission` (for example: "protect the renewal gate at [account]",
   "convert drawdown headroom into bookings", "de-risk the renewal with a
   services story"). These are re-derived, not copied from any prior plan
   document — plans go stale; the sources don't.
2. Re-read the map end to end against those goals.
3. Generate 3–5 candidate work products the map can actually support. Score
   each on: **strategic-goal impact** (which derived goal does it advance,
   and how directly?), **evidence sufficiency** (are the source waypoints
   already located?), and **completability** (can it be finished with
   read-only access + scratch writes, no operator dependency in the critical
   path?). **Reject any candidate whose premise the freshest customer signal
   contradicts** — a goal built on an assumed open deal when the pursuit note
   already records a non-renewal is a dead candidate however good the map
   looks.
4. Pick ONE. Write `goal`, a `work_product` path under
   `scratch/out/waypoint/`, and 3–6 concrete, checkable `done_criteria` into
   `## Mission`. Set `phase: EXECUTE`. Add an "in progress" row for this
   mission to the top of `scratch/out/waypoint/INDEX.md` (create the file if
   absent).
5. Announce the selection in chat: the derived strategic goals, the chosen
   work product, and the runner-up list with why they lost — the operator can
   override by editing `## Mission` before the next tick.

Too big if it needs >~8 EXECUTE iterations — split and queue the rest.
Calibration examples: `REFERENCE.md` → "Goal shapes".

### Phase EXECUTE — build the work product

1. Open the work product (create it at `work_product` if absent, with the
   done_criteria as a skeleton). Pick the next unmet criterion.
2. Do the reading/synthesis that criterion needs — pull the specific waypoint
   docs, extract what matters, write the section. Cite every factual claim to
   its waypoint ID or mark `[DATA NEEDED]`. Never fabricate a number, score,
   or SF value. **A dollar figure may reach a work-product headline or
   summary only if it was read from the money doc itself (direct source); a
   value derived from a digest or a pricing heuristic carries `[heuristic —
   verify]` inline and stays out of headlines until a direct read confirms
   it.** **A derived quantitative *method* — pricing, run rate, capacity —
   must be grounded in a source before any figure is written: a playbook, the
   structure of a prior executed work product, or an explicit operator
   directive. State the method and its source before the number; never invent
   a formula from first principles.**
3. Check the criterion off in `## Mission`, update `progress`.
4. If a criterion turns out to need a missing document, add the hop to
   `## Queue` and run it as a mini-EXPLORE next iteration — then return.

**Deck work products** hand off to `exec-review-deck` in the final EXECUTE
iteration, which stops to ask before creating the private staging file —
even under a loop (`REFERENCE.md` → "Slide/deck work products").

5. When ALL criteria are checked: set `phase: DONE`, write a **"So-what"
   block** at the top of the pack — the ≤3 operator actions/decisions this
   pack exists to force (these are `sweep`'s input; a pack that can't name
   its so-what wasn't worth building) — move this mission's `INDEX.md` row
   into the **Ready for review** section, present the finished work product
   in chat (path + summary + the so-what), remind the operator that
   customer-facing material must pass `draft-review` + `humanizer` before
   sending, propose 2–3 candidate next missions, and tell the operator the
   loop can be stopped or left running.

### Phase DONE — idle / handoff
Archive the finished mission into `## Explored log` and re-enter NARROW. If
the operator said to stop after one mission, say so and suggest stopping the
loop.

## Exploration verbs (used in EXPLORE, and by EXECUTE when fetching sources)

**Local first:** `accounts/<acct>/meetings/` and `transcripts/` are
zero-cost, first-class evidence — check them before any external call; cite
as `[Transcript YYYY-MM-DD slug]`. Then the external verbs, in order of
typical yield: search your document store by name, walk a known folder, read
a doc/sheet, and query any organization-provided AI assistant (such as
`fieldkit shadowbot`, if configured) for policy/program documents.

Command syntax is tool-specific; `REFERENCE.md` → "Exploration-verb command
templates" has a worked example to adapt. Two decision rules:

- **Bulk reads go through a cheap subagent, not the main loop.** If a
  doc/sheet read or folder walk is likely to return >~100 lines of payload,
  don't run it in the loop — delegate it to a fast/cheap model whose prompt
  is the exact command(s) plus "return ≤15 lines: candidate waypoints (ID,
  name, date, one-line why-it-matters vs the strategic goals) + suggested
  next hops; no raw content." The loop context only ever holds the digest —
  the single biggest token lever in EXPLORE.
- **Assistant auth failure:** do NOT retry or re-auth yourself — log the
  failure in the Explored log, requeue the hop, move on. The operator fixes
  auth out of band. An AI assistant's citations are policy waypoints; its
  state claims are unverified — label them accordingly.

## Guardrails

- **Read-only against every external system**, with ONE narrow carve-out:
  your document store is searched and read, never reorganized or shared; any
  AI assistant is queried, never actioned. The carve-out: with the
  operator's approval, EXECUTE may create one private staging file for a
  deck work product. Existing documents are never modified, shared, moved,
  or re-permissioned.
- Writes go to `scratch/waypoints/` and `scratch/out/` only. Never `notes/`,
  never `accounts/`, never a commit. If a mission's natural output is a
  pursuit file or note update, produce it as a *proposal* in scratch and say
  so in chat.
- Work products are **internal drafts** until the operator says otherwise.
  Anything customer-facing must clear `draft-review` + `humanizer` — the
  skill never sends, shares, or publishes.
- External intel stays labeled (e.g. `[Drive]`, `[Assistant]`).
- Scope filter: a document earns a waypoint only if it serves your stated
  strategic goals. Interesting-but-irrelevant → skip.
- Bounded work: max ~2 hops or ~6 external calls per iteration, one
  done-criterion per EXECUTE iteration (meet that criterion's check rather than
  smearing across several). If a hop explodes (a folder with 100 children),
  take the top 20 by modifiedTime and queue a "page 2" hop.

## Model & token economics

Mid-tier model for the loop, cheap model for bulk reads, strong model once
per mission for NARROW: `REFERENCE.md` → "Model & token economics".

## Loop usage

```
/loop /waypoint            # Claude Code; use your harness's loop facility
/loop 30m /waypoint        # fixed cadence
```

Cadence guidance: `REFERENCE.md` → "Loop cadence".
