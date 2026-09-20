# Engagement Health

Classifies active delivery projects by contract end date proximity.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today; this skill reads from cached files
- **Trigger overlap with adjacent skills** — check that you need this specific skill and not a closely named one (e.g. `pipeline` skill's `ops/pipeline-health.md` vs `ops/forecast.md`)

## Constraints

- **Never write to account files without explicit confirmation**
- **Do not modify pursuit frontmatter mid-workflow** — only write at designated save steps
- **Always surface output for review before sending externally**

## Quick Reference

```bash
# All accounts
fieldkit pursuit projects

# Single account
fieldkit pursuit projects --account acme-corp

# Treat ZOMBIE or UNKNOWN findings as an exit-1 policy failure
fieldkit pursuit projects --strict
```

## Health Tiers

| Tier | Condition |
|------|-----------|
| ZOMBIE | Contract end date past, stage not Completed/Closed |
| EXPIRING | Contract ends within 30 days |
| SOON | Contract ends within 90 days |
| ACTIVE | Contract end date > 90 days out |
| UNKNOWN | No contract end date in frontmatter |

## Exit Codes

- `0` — valid report by default; with `--strict`, no ZOMBIE or UNKNOWN projects
- `1` — `--strict` found one or more ZOMBIE or UNKNOWN projects
- `3` — data error

## When to Use

- Before QBRs to understand active delivery portfolio
- When planning renewals ("what's expiring?")
- After `fieldkit ingest` run to see updated project state
- When asked "how are my projects doing?"

## Output

Ranked table: ZOMBIE first (oldest overdue at top), then EXPIRING, SOON, ACTIVE. Shows project name, SF stage, contract end date, and days until end.

## Related Skills

- `pipeline` skill's `ops/pipeline-health.md` — deal risk scan instead of delivery/contract status
- `contract` skill's `ops/contract-extract.md` — extract contract terms feeding contract end dates
