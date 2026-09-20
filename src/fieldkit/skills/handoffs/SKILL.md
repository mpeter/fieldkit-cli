---
name: handoffs
description: >
  A session is ending and work needs to continue elsewhere — a new session, a different agent, a
  different day. Captures intent, decisions, files touched, and pending work into a dated handoff
  file. Also writes fresh-eyes packages that strip this session's conclusions so an unanchored
  reader can judge the work independently.
  Trigger with "create a handoff", "save my progress", "end this session", "wrap up and hand
  off", "continue this later", "save context for next time", "I need to stop here", "hand this
  off", "fresh eyes", "second opinion", "no assumptions from this session", "hand this to a
  judge", "parallel analysis".
metadata:
  opencode/slash: "true"
  category: ops
---

# Session Handoff

Two modes, opposite purposes. **Continuity** preserves this session's conclusions so the next
session resumes without re-derivation. **Fresh eyes** strips them so the next session rules on
the work without inheriting its framing.

## Gotchas

- **Ask the mode first, before assembling anything.** The modes gather different material.
  Drafting a continuity handoff and deleting sections does not yield a fresh-eyes package — the
  framing survives in the structure itself: which sections exist, their order, what was judged
  worth including. Assemble from the session against the chosen mode.
- **No purpose, no handoff** — continuity needs a stated goal for the next session; fresh eyes
  needs the question to rule on. Without one, stop and ask. A handoff without a purpose is a
  transcript dump, not a plan.
- **Trigger overlap with `pickup`** — this skill *writes* a handoff; `pickup` *reads* one.
  Confirm direction before acting.
- **Fresh eyes is a discipline, not an automation.** The separation is judgment work. Run
  without attention it produces a wayfinding file that quietly carries this session's framing —
  the one failure that makes the artifact worthless. Say so when handing it over.

## Constraints

- **Require an explicit purpose** before writing — ask if none was given.
- **Never fabricate pending work** — only list tasks the operator actually asked for.
- **Write under `.planning/` at the working repo root** (`git rev-parse --show-toplevel`),
  falling back to the workspace root outside a repo. Committed by default; a repo that wants
  session artifacts kept out of its history says so in its own `AGENTS.md`.
- **Stamp `type:`** on every artifact (`continuity` or `fresh-eyes`) — `pickup` branches on it.
- **Surface the file path back to the operator.**

## Process

1. **Pick the mode.** If the operator didn't say, ask with the `question` tool — never infer:
   - *Another session* — someone picks this up and continues. Conclusions preserved.
   - *Fresh eyes* — someone rules on this independently. Conclusions stripped.
2. **Confirm the purpose.** Continuity: what should the next session do? Fresh eyes: what should
   it rule on? Do not proceed without an answer.
3. **Walk the conversation chronologically**, collecting the operator's explicit requests, the
   decisions made and why, and the specific files, commands, and code touched.
4. **Assemble against the mode**, using the matching structure below.
5. **Slug it.** Short kebab-case (`fix-issue-42`, `site-disclosure`). Fresh eyes uses the slug as
   its initiative directory — ask for one if the operator hasn't named it.

## Continuity Structure

Favor concrete detail (file paths, exact commands, function names) over paraphrase — the reading
agent has no memory of this session.

```markdown
---
type: continuity
---
# Session Handoff Plan

## 1. Primary Request and Intent
[What the operator explicitly asked for, in detail]

## 2. Key Technical Concepts
- [Concept]

## 3. Files and Code Sections
### [File Name]
- **Why important**: [...]
- **Changes made**: [...]

## 4. Problem Solving
[Problems solved, and anything still being troubleshot]

## 5. Pending Tasks
- [Task the operator explicitly asked for, still open]

## 6. Current Work
[What was in progress immediately before this handoff — files, commands, exact state]

## 7. Next Step
[The single next action, in line with the stated purpose — omit if there isn't one]
```

## Fresh-Eyes Structure

Two files in `.planning/<slug>/`. The reader gets these and nothing else.

**The separation test — apply to every sentence:** *does this survive if the reader disagrees
with me?* If no, it is a conclusion. Withhold it. Recommendations, sequencing, technology
choices, and proposed structures are conclusions.

**Two things that look like conclusions but go in verbatim:** the operator's stated goals, and
any gap the operator caught during the session. Both are inputs, not findings — and the caught
gaps are usually the highest-value content in the file.

`00-WAYFINDING.md`:

```markdown
---
type: fresh-eyes
---
# Wayfinding — <subject>

FACTS AND POINTERS ONLY. No recommendations, no proposed structure, no decisions.
If you find a conclusion in here, it's a bug; flag it.

## The task, as the operator stated it
[Verbatim. Not paraphrased.]

## Read these; they are the ground truth
| Path | What it is |

## Extracted facts
[Grouped by area. Observations, never arguments for a direction.]

## Explicitly out of scope for this file
[Name what was deliberately withheld, so the reader knows it exists.]
```

`01-JUDGE-HANDOFF.md`:

```markdown
---
type: fresh-eyes
---
# For the judgment tier — <subject>

## Read, in this order
1. `00-WAYFINDING.md` — gathered facts, no conclusions. Do not re-derive it.
2. [binding documents]

## Your role
You are the judgment tier. You are NOT being asked to plan, design, or sequence.
The wayfinding file is neutral ground by construction — approach this fresh.

## The questions
1. [Specific question, not a topic]

## Output shape
Numbered rulings, each with the principle behind it — not an essay. Then a
"handed down" list: the calls you deliberately left to whoever plans against
this. That list is the seam.

Write your own handoff for the next tier when you're done.

## Escalation
If a ruling proves unimplementable on contact, flag it back to the operator.
Never silently override it. If you need a fact you don't have, name the gap.
```

Before handing over, grep the wayfinding for `should`, `recommend`, `migrate`, `best`, and
`add`. It is not a sound check, but it catches the obvious leaks.

## Final Step

1. Resolve the target: `.planning/` under `git rev-parse --show-toplevel`, else the workspace
   root. Create it if absent.
2. Write the files:
   - Continuity → `.planning/handoffs/YYYY-MM-DD-HHMMSS-<slug>.md`. The timestamp is not
     decoration: parallel sessions writing the same day would otherwise overwrite each other.
   - Fresh eyes → `.planning/<slug>/00-WAYFINDING.md` and `01-JUDGE-HANDOFF.md`.
3. Tell the operator the path, and how to open it:
   - Continuity → `/pickup <filename>`.
   - Fresh eyes → a new session, on a model of their choosing, pointed at
     `01-JUDGE-HANDOFF.md`. The mode's value comes from that session having none of this one's
     context, so hand over the path, not a summary.
