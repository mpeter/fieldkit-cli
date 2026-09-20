# Pursuit Advance — Gate Reference

## Stage Sequence

```
discover → validate → propose → negotiate → closed-won
                                          └→ closed-lost
```

## Current Policy

| Transition | Result | Meaning |
|---|---|---|
| discover → validate | `pending` | Salesforce-native qualification policy is not ratified |
| validate → propose | `pending` | Salesforce-native qualification policy is not ratified |
| propose → negotiate | `pending` | Salesforce-native qualification policy is not ratified |
| negotiate → closed-won | `pass` | Qualification-independent transition |
| Other forward transitions | `pass` | Qualification-independent transition |

Moving to `closed-lost` is allowed from any stage without a gate check.

## Gate Check Output Format

```markdown
## Gate Check: [current-stage] → [target-stage]

**Policy result:** PASS / PENDING

[If PENDING]
- Salesforce-native qualification policy is not yet ratified for this transition.
- Historical local MEDDPICC evidence was not evaluated.
- An explicit override reason is required to proceed.
```

## Stage Updated Output Format

```markdown
## Stage Updated

**Deal:** [opportunity name]
**Account:** [account]
**Transition:** [from] → [to]
**Policy result:** [pass / override]
[If override] **Override reason:** [reason]
**Date:** [today]

Next steps for [target-stage]:
[2–3 bullet actions appropriate to the new stage]
```

## Transition History Entry Format

```yaml
- date: YYYY-MM-DD
  from: [previous-stage]
  to: [new-stage]
  gate-result: pass        # or "override"
  override-reason: ""      # non-empty if gate-result is "override"
```

## Error Cases

- **Unknown stage in frontmatter:** Stop. Report and ask user to correct.
- **Pursuit file not found:** Stop. List available files in the account folder.
- **Already at closed-won or closed-lost:** Stop. The pursuit is closed.
- **Regression (moving backward):** Allowed without gate check. Record with `gate-result: override` and require a reason.
