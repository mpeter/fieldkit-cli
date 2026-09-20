---
name: pursuit-advance
description: Advance a pursuit to the next deal stage. Evaluates the ratified stage policy without using historical local MEDDPICC scores, reports score-dependent transitions as pending until native ClosePlan policy exists, and writes only after policy passes or the operator supplies an explicit override reason. Trigger with "advance [deal]", "move [opportunity] to [stage]", "run gate check on [deal]", "try to advance [pursuit]", or "attempt stage transition for [account/deal]".
metadata:
  opencode/slash: "true"
  category: product
---

# Pursuit Advance

Check current stage policy for a pursuit, preview the result, then update the
pursuit only after confirmation. Formerly score-dependent transitions remain
`pending` until a Salesforce-native policy is ratified; historical local values
never make them pass.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Do not advance deal stages without a passing policy or an explicit override reason**
- **Always surface generated output for review before any external send**

## Inputs

Required:
- Pursuit file path (e.g. `accounts/acme-corp/pursuits/project-shift.md`)
  OR account name + opportunity name (skill will resolve the path)

Optional:
- Target stage (if omitting, skill advances to the next stage in sequence)
- Override reason (required only when gate fails and user wants to proceed anyway)

## Stage Sequence

```
discover → validate → propose → negotiate → closed-won
                                          └→ closed-lost
```

## Current Policy Reference

| Transition | Current policy result |
|---|---|
| discover → validate | `pending` — Salesforce-native qualification policy is not ratified |
| validate → propose | `pending` — Salesforce-native qualification policy is not ratified |
| propose → negotiate | `pending` — Salesforce-native qualification policy is not ratified |
| negotiate → closed-won | `pass` — qualification-independent transition |
| Other forward transitions | `pass` — qualification-independent transition |

Moving to `closed-lost` is allowed from any stage without a gate check.

Groups needed: none — the pursuit file is read, searched, and edited directly on disk (native file reads/edits).

## Execution

### Step 1: Resolve pursuit file

If a full path was provided, use it directly.

Otherwise, search on disk:
```
rg -l "<opportunity name>" accounts/*/pursuits/
```
Pick the best match and confirm the resolved path with the user if ambiguous.

### Step 1b: Check internal Slack before running the policy preview

Slack is internal. Before running the gate check, see what colleagues
have posted — a delivery concern, critsit, or competitive signal may affect the
operator's decision to proceed or override.

```bash
# Check daily cache first
grep -i "<account name>" <data-repo>/watchers/slack-signals.md

# Find account-specific channels
slackcli search channels "<account name>"           # look for team-*, proj-*, critsit-*
slackcli conversations read <team-channel-id> --limit 15

# Look for recent blockers or risks
slackcli search messages "<account name>" after:<14-days-ago> --limit 15
```

If new intel is found (delivery risk, competitive threat, stakeholder change),
surface it before the policy preview. Slack is context, not current qualification;
do not convert it into a local score or claim it changed ClosePlan.

See `references/slack-search-protocol.md`.

### Step 2: Read current frontmatter

Read the pursuit file directly:
```
read <pursuit_file_path>
```

Parse the YAML frontmatter block. Extract:
- `stage` — current stage
- `gate-status` — last gate evaluation result
- `transition-history` — full history array

An old `meddpicc` block may load as `legacy_meddpicc`. It is historical evidence
only: do not total it, compare it to thresholds, or use it as a current pass.

Determine the target stage (next in sequence unless overridden by user).

### Step 3: Preview current stage policy

Run the command in dry-run JSON mode so the CLI, not the skill, owns policy:

```bash
fieldkit pursuit advance <account>/<pursuit> --to <target-stage> --dry-run --json
```

Interpret the result exactly:

- `gate_status: pass` — the transition is qualification-independent.
- `gate_status: pending` — no Salesforce-native policy is ratified for this
  formerly score-dependent transition. Exit 1 is expected; no write occurred.
- `gate_status: override` — only possible when an explicit reason is supplied.

Do not turn a successful ClosePlan read into a pass: `read` means observed, not
qualified. Do not use historical local scores when policy is pending.

For `negotiate → closed-won`, ask whether the contract is executed before offering
the write. For `closed-lost`, ask for the deciding factor and use it as the explicit
transition rationale.

### Step 4: Present advisory and ask for confirmation

**If policy PASSES:**

```
Current policy passes for [current-stage] → [target-stage]. Recommend advancing [deal].

Confirm? (yes / no)
```

**If policy is PENDING:**

```
Current Salesforce-native qualification policy is pending for this transition.

Options:
  1. Stop without changing the pursuit
  2. Override — proceed with a documented reason

If override: provide a brief reason (e.g. "EB meeting scheduled for Friday,
advancing to keep proposal timeline on track").
```

Do not advance without either a policy pass or an explicit override reason.

### Step 5: Update pursuit frontmatter

After confirmation, use the CLI that produced the preview:

```bash
# Passing qualification-independent transition
fieldkit pursuit advance <account>/<pursuit> --to <target-stage>

# Pending transition that the operator explicitly overrides
fieldkit pursuit advance <account>/<pursuit> --to <target-stage> --override '<reason>'
```

The command atomically updates `stage`, `gate-status`, `last-transition`, and
`transition-history`. If the file still has the former `meddpicc` key, this
separately authorized write may canonicalize it to `legacy_meddpicc` without
changing its historical values.

### Step 6: Confirm and summarize

After writing, display:

```markdown
## Stage Updated

**Deal:** [opportunity name]
**Account:** [account]
**Transition:** [from] → [to]
**Policy result:** [pass / override]
[If override] **Override reason:** [reason]
**Date:** [today]

Next steps for [target-stage]:
[2–3 bullet actions appropriate to the new stage — drawn from pursuit-stages.md]
```

If the new stage is `closed-won` or `closed-lost`, prompt: "Reminder: append a
win/loss note to `memory/system/lessons-learned.md` within 5 business days."

## Error Cases

- **Unknown stage in frontmatter:** Stop. Report the unrecognized stage value and
  ask the user to correct the frontmatter manually.
- **Pursuit file not found:** Stop. List available pursuit files in the account folder.
- **Already at closed-won or closed-lost:** Stop. The pursuit is closed. No further transitions.
- **Regression (moving backward):** Allowed without gate check. Record in
  `transition-history` with `gate-result: override` and require a reason.

## Related Skills

- **grill** — Read exact native ClosePlan questions and coach evidence without writing
- **meeting** — Prepare questions around current evidence needs before a customer call
