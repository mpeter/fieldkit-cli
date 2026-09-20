# Pipeline Health

Scans all active (non-closed) pursuit files and ranks them by risk tier.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Quick Reference

```bash
# All accounts
fieldkit pursuit health

# Single account
fieldkit pursuit health --account acme-corp

# Treat HIGH findings as an exit-1 policy failure
fieldkit pursuit health --strict

# Apply the same strict gate to delivery-project findings
fieldkit pursuit projects --strict
```

**Risk tiers:**
- **HIGH** — overdue close date
- **MEDIUM** — close date within 30 days while still in an early stage, or missing `sf_opportunity_id`
- **LOW** — no immediate timeline or linkage concerns

The command never classifies risk from historical local qualification values. It
reports Native Qualification as `unavailable`; use `/grill` for a fresh read-only
ClosePlan review. A successful native read means observed state, not a stage-gate pass.

**Exit codes:**
- `0` — valid report by default; with `--strict`, no attention findings
- `1` — `--strict` found HIGH pursuits, or ZOMBIE/UNKNOWN projects
- `3` — data error

## When to Use

- Start of day / before pipeline reviews
- After `fieldkit sf listview` to surface newly stale deals
- When asked "what needs attention in my pipeline?"

## Output

Prints a ranked table with deal name, stage, days in stage, Native Qualification
(`unavailable`), close date, and qualification-independent risk reasons.

## Related Skills

- `pipeline` skill's `ops/forecast.md` — weighted revenue projection instead of risk scan
- `pipeline` skill's `ops/engagement-health.md` — delivery/contract status instead of deal risk
