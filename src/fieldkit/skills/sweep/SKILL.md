---
name: sweep
description: >
  One bounded iteration: read the small actionable items the system has already surfaced
  (TASKS.md, any open-actions ledger your other skills produce) and clear 2-3 of them — prep
  each to one-keystroke-from-done (a drafted email, an exact teed-up command, a one-line ask),
  retire what's dead, and surface what's blocked on the operator. Use when the operator types
  /sweep, runs it in a loop, or asks to "clear the small stuff", "knock out the little things",
  or "prep my follow-ups".
metadata:
  opencode/slash: "true"
  category: ops
---

# /sweep — clear the small stuff

A net **consumer** of system state: every iteration should leave fewer open
items than it found, never more artifacts. It takes the small, dated,
concrete items your task ledger (and any other skill's output) keeps
surfacing and gets each to the point where the operator's remaining effort is
one keystroke or one decision.

## Inputs (read in this order)

1. `TASKS.md` — Today first, then Waiting-On, then Active.
2. Any dated "open-actions ledger" or "so-what" summary a document-discovery
   or research skill in this workspace produces (optional — skip this input
   entirely if no such skill is in use; sweep doesn't require one).
3. Live SF / Gmail / calendar only when prepping a specific item requires it — never a
   general sweep of them (that's `brief`'s job).

## One iteration

1. **Pick 2–3 items**, ranked by: date-sensitivity first (anything due within
   ~3 days), then unblock value (items gating other work), then effort
   (prefer ≤15-min items; anything bigger belongs to a dedicated session or
   the operator). Skip items that are pure operator judgment — surface those,
   don't attempt them.
2. **Prep each to one keystroke:**
   - Email/Slack follow-up → full draft to `scratch/out/drafts/NN-slug.md`,
     in the operator's voice (see Style). **Drafts only — never send.**
   - SF field update → the exact command or script snippet, ready to paste,
     with current + proposed value shown. **Never execute a write yourself**
     unless the operator has said so for that specific item in this session.
   - Info lookup → answer it now from live sources, cite, mark CLEARED.
   - Scheduling → the proposed slot(s) from live calendar, ready to confirm.
3. **Retire the dead:** items already done (e.g. a pack delivered what the
   task asked) or overtaken by events → propose the strike-through with a
   one-line pointer to the evidence. **TASKS.md edits and ledger
   strike-throughs are proposals presented in chat; apply only on the
   operator's word** (exception: the operator can grant a standing "groom
   freely" for a session).
4. **Write the sweep sheet** — `scratch/out/sweep/YYYY-MM-DD-sweep.md`
   (append to today's if it exists), three buckets:
   - **⚡ Teed up** — item, the artifact/command, the one keystroke left.
   - **✅ Cleared** — item, what resolved it, citation.
   - **🧍 Blocked on you** — item, the specific decision/fact needed,
     deadline.
5. Present the three buckets in chat, tersely. Stop.

## Google Tasks — the visible surface (if you use `task-sync`)

If this workspace syncs TASKS.md with a Google Tasks list (see the
`task-sync` skill), that list is the operator's mobile-visible task surface
and its source of truth. So:

- A **new tactical item** worth tracking → offer to add it to the list, not
  just to TASKS.md. The anchor flows into the managed region on the next
  `task-sync` run.
- An item you **clear** that maps to a list task → propose marking it
  completed in the list; apply on the operator's word, same as a TASKS.md
  strike.
- Don't hand-edit the TASKS.md managed region directly — go through the list
  + `task-sync` so the two never diverge.

If this workspace doesn't use `task-sync`, skip this section entirely and
treat TASKS.md as the sole surface.

## Style (for drafted comms)

- The operator's voice: short, direct, warm-professional. Customer language,
  never vendor jargon.
- Match the operator's established date/time conventions in customer-facing
  drafts; ISO 8601 everywhere else.
- Never fabricate a fact, number, or commitment in a draft — `[CHECK: …]`
  inline if unsure, and say so in the tee-up note.
- Customer-facing drafts get a `draft-review` reminder in the tee-up line.

## Guardrails

- **Never send, post, or submit anything.** Emails are drafts; Slack messages
  are proposed text; forms are never touched. The operator fires every shot.
- **SF writes:** teed-up commands only, clearly marked DRY. Execution
  requires the operator's explicit word per item.
- Writes go to `scratch/out/sweep/`, `scratch/out/drafts/`, and (on approval)
  TASKS.md / the ledger. Never notes/, never accounts/, never a commit.
- Bounded: 2–3 items per iteration, ≤6 external calls. An item that balloons
  gets returned to the list annotated "bigger than it looks" — not smeared
  across the iteration.
- External intel stays labeled ([SF], [Gmail], [Transcript …], [Drive]).

## Loop usage

```
/sweep                      # one iteration, on demand
```

Best cadence: after a morning `brief` (Today is fresh), and after any
longer-running discovery/research skill completes (new items to clear). A
sweep that finds nothing ≤15-min-sized says so and stops — an idle sweep is a
correct sweep.
