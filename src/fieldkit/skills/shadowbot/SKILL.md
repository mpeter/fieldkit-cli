---
name: shadowbot
description: >
  Route a request to the right capability of your organization's ShadowBot-compatible AI
  assistant (configured via `fieldkit shadowbot`), assemble account context from local files,
  execute via `fieldkit shadowbot query`, and save output to scratch/. Use when the operator
  types /shadowbot, asks to "run shadowbot" or "ask the assistant", or describes a workflow
  that matches a capability in this workspace's local ShadowBot routing reference (if one
  exists).
metadata:
  opencode/slash: "true"
  category: product
---

# ShadowBot Skill

Single entry point for interacting with `fieldkit shadowbot` — fieldkit's
generic integration with an organization-provided ShadowBot-compatible AI
assistant. This skill handles the mechanics (auth, prompt construction,
output capture, chaining, presentation); it does **not** ship a specific
assistant's capability catalog, because that catalog is entirely specific to
whichever assistant your organization has deployed.

**Your organization's routing table lives outside this skill.** If this
workspace has a local reference describing which assistant capability to
invoke for which request, and how to identify an account to it (for example
`playbooks/shadowbot/routing.md`, or wherever your workspace's
`AGENTS.md`/`CLAUDE.md` says it keeps one), read it before constructing a
prompt. If no such reference exists, ask the operator which capability to
invoke and how the assistant expects the account to be identified, then
treat the answer as durable enough to propose adding to a local reference
file for next time.

## Gotchas

- **A capable assistant's default failure mode is often "right answer, wrong
  shape," not wrong answer** — verbose output when brevity was implied,
  unrequested detail for a simple lookup. State a length ceiling when the ask
  implies brevity, and ask for the headline answer first with detail gated
  behind a follow-up.
- **Always use `--new`** unless the operator explicitly asks to continue a
  prior thread — omitting it can let stale thread context bleed into new
  output.
- **Take system-of-record facts (stage, ACV, close date) from
  `fieldkit sf`.** Assistant output is unverified synthesis: use it for
  intelligence, not ground truth.
- **Auth check before long workflows** — if the assistant returns an auth
  error or the stream dies immediately, run `fieldkit auth shadowbot`.
- **Capability name must be exact in the prompt** when your organization's
  assistant supports named capabilities — use the exact name from your local
  routing reference for deterministic routing. Natural language works but can
  misroute.
- **Never rely on the assistant for CRM writes** — even if it self-reports
  write access to your CRM, verify independently before trusting it; use
  `fieldkit sf` for Salesforce writes.

## Constraints

- All output goes to `scratch/` — never write assistant output directly to
  account files or pursuit notes.
- Never present assistant output as SF-verified fact.
- Run the `draft-review` skill on any assistant-generated customer-facing
  content before sending.
- Do not chain more than ~3 capability invocations in one session without
  presenting output to the operator between steps.

## Gateway Prerequisite

```bash
fieldkit auth shadowbot      # checks for a valid token; exit 2 means re-authenticate
```

---

## Step 1 — Identify Account and Capability

**From the operator's prompt, extract:**

1. **Account** — resolve to the account slug this workspace uses, then read
   whatever canonical identifiers your workspace stores for it in
   `config/accounts.yaml` (account IDs, territory IDs, or whatever scheme
   your CRM integration and assistant expect — this is workspace-specific,
   not fixed by this skill).
2. **Capability to invoke** — check your workspace's local routing reference
   first. If none exists or the request doesn't match an entry, ask the
   operator: "Which assistant capability should I use?"

If the operator explicitly names a capability, use that name directly
without re-routing.

---

## Step 2 — Assemble Context

Pull canonical identifiers and deal context before constructing the prompt:

```bash
# Pursuit context (for deal-specific capabilities)
ls accounts/<account>/pursuits/ 2>/dev/null
cat accounts/<account>/pursuits/<relevant-pursuit>.md 2>/dev/null | head -40
```

Extract from the pursuit file whatever is relevant to the request —
typically `sf_stage`, `sf_close_date`, `sf_acv`, or named competitors. For
qualification context, read the native ClosePlan scorecard with
`fieldkit sf meddpicc <opp-id>`; local historical scores are not current
evidence.

---

## Step 3 — Execute

Construct and run the query. Use `--new` unless continuing a thread, and
redirect output to `scratch/` so it's reviewable and not lost. Account names
and pursuit text are untrusted, so the prompt never appears in shell syntax,
not even a heredoc. Write it to `scratch/shadowbot-prompt.txt` with your
file-writing tool (not `echo`), keeping it to identifiers and the few fields
the request needs (under about 2,000 characters, never a whole file):

```text
<capability-name, if your assistant supports named capabilities> for <account display name>.
<canonical account identifier(s), per your workspace's scheme>.
<additional context relevant to the request>.
```

Then send it on stdin:

```bash
set -o pipefail   # report fieldkit's exit status, not tee's (bash and zsh)
mkdir -p scratch  # fieldkit init does not create it
fieldkit shadowbot query --new < scratch/shadowbot-prompt.txt 2>>scratch/shadowbot-stderr.log \
  | tee scratch/shadowbot-<capability-slug>-<account-slug>-$(date +%Y%m%d).md
echo "exit: $?"
```

Pipe through `tee` (not `>`) to see streaming output in the terminal while
also saving to scratch. Transport logging goes to
`scratch/shadowbot-stderr.log` rather than the terminal; read its tail when
the exit status is nonzero. Exit 2 means authentication needs operator
action.

If auth fails (exit 2, or the stream dies immediately), run:
```bash
fieldkit auth shadowbot
```

---

## Step 4 — Present Output

After the command completes:

1. **Read the output file** and present key sections to the operator.
2. **Strip boilerplate** the operator did not ask for (support-bot
   disclaimers, "need more help?" footers) from the chat presentation — the
   saved file keeps the full output.
3. **Extract any "what next?" options** the assistant surfaced, if present.
4. Confirm any activity-signal content stays visibly separated from
   SF-sourced facts.
5. If the raw output buries a one-line answer inside a long block, lead with
   that line rather than making the operator hunt for it.

---

## Step 5 — After Presenting

- If the operator selects a continuation option, run the chained capability
  in the same scratch file (same account/context) or a new one (different
  account/context changes).
- If output contains customer-facing content, route it through
  `draft-review` before the operator sends it.
- If output bears on deal qualification, route it to the `post-meeting`
  skill as candidate evidence against native ClosePlan questions, labeled
  `[Assistant]`. It never becomes pursuit-note or qualification state on
  its own; `grill` reviews the evidence.

---

## Skill Chaining Patterns

Some assistants cache intermediate research per-thread and read it back on
follow-up turns — dropping `--new` on a second call can let it skip
re-querying and reuse the first call's cached output. Whether this applies,
and which capability pairs chain safely, is assistant-specific; record
confirmed-safe chains in your local routing reference as you discover them.

**Pattern (if your assistant supports thread continuation):**
```bash
set -o pipefail && mkdir -p scratch
# First call — always --new; prompt file written with your file tool
fieldkit shadowbot query --new < scratch/shadowbot-prompt.txt 2>>scratch/shadowbot-stderr.log \
  | tee scratch/shadowbot-chain-<account>-$(date +%Y%m%d).md

# Chained call — no --new, same thread, appended to same file
fieldkit shadowbot query < scratch/shadowbot-prompt-2.txt 2>>scratch/shadowbot-stderr.log \
  | tee -a scratch/shadowbot-chain-<account>-$(date +%Y%m%d).md
```

**Always use `--new` regardless of chaining, when:**
- The account or deal differs from the prior turn.
- Prior output was bad or hallucinated — a fresh thread resets state.
- A long time has passed since the last turn (some assistants auto-clear
  context near a token limit).
