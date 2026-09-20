# Forecast

Generates a probability-weighted pipeline forecast from pursuit frontmatter.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today; this skill reads from cached files
- **Trigger overlap with adjacent skills** — check that you need this specific skill and not a closely named one (e.g. `pipeline` skill's `ops/pipeline-health.md` vs `ops/engagement-health.md`)

## Constraints

- **Never write to account files without explicit confirmation**
- **Do not modify pursuit frontmatter mid-workflow** — only write at designated save steps
- **Always surface output for review before sending externally**

## Quick Reference

```bash
# All accounts
fieldkit pursuit forecast

# With quota target
fieldkit pursuit forecast --quota 5000000

# Single account
fieldkit pursuit forecast --account acme-corp --quota 2000000
```

## Stage Weights

| Stage | Weight |
|-------|--------|
| closed-won | 100% |
| negotiate | 75% |
| propose | 50% |
| validate | 25% |
| discover | 10% |

## Scenarios

- **Commit** — closed-won + negotiate (high confidence)
- **Weighted** — probability-weighted sum across all active deals
- **Best Case** — sum of all active deals at face value

## Output

Prints a per-deal table sorted by stage weight (highest confidence first), then a scenario summary.
If `--quota` is passed, shows gap-to-quota for both commit and weighted scenarios.

## When to Use

- Before forecast calls or pipeline reviews
- When asked "what's my weighted number?" or "gap to quota"
- After `fieldkit sf listview` to refresh ACV data in frontmatter

## Related Skills

- `pipeline` skill's `ops/pipeline-health.md` — risk scan of deals instead of a weighted projection
- `deal-desk` — see `contract` skill's `ops/deal-desk.md` for pricing lookup and margin analysis
