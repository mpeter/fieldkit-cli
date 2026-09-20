# Deal Desk

Look up pricing, compute margins, flag approval tiers, and size engagements against the CY26Q1 rate card.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today; this skill reads from cached files
- **Trigger overlap with adjacent skills** — check that you need this specific skill and not a closely named one (e.g. contract-check vs contract-extract)

## Constraints

- **Never write to account files without explicit confirmation**
- **Do not modify pursuit frontmatter mid-workflow** — only write at designated save steps
- **Always surface output for review before sending externally**

## Rate Card — CY26Q1

All rates in USD per day. **L = List. C = Cost. CR = Customer Rate (List × 0.80).**

| Role | List (L) | Cost (C) | Customer Rate (CR) | Margin |
|------|----------|----------|--------------------|--------|
| Architect | $379.00 | $196.00 | $303.20 | 35.3% |
| Associate Consultant | $211.00 | $120.51 | $168.80 | 28.6% |
| Associate Project Manager | $211.00 | $103.00 | $168.80 | 39.0% |
| Consultant | $260.00 | $123.60 | $208.00 | 40.6% |
| Delivery Lead | $314.00 | $172.01 | $251.20 | 31.5% |
| Engagement Lead | $368.00 | $244.11 | $294.40 | 17.0% |
| Managing Director | $610.00 | $592.25 | $488.00 | -21.4% |
| Principal Architect | $449.00 | $247.20 | $359.20 | 31.2% |
| Principal Consultant | $389.00 | $185.40 | $311.20 | 40.4% |
| Program Manager | $368.00 | $192.61 | $294.40 | 34.6% |
| Project Manager | $281.00 | $141.11 | $224.80 | 37.2% |
| Senior Architect | $281.00 | $141.11 | $224.80 | 37.2% |
| Senior Consultant | $281.00 | $141.11 | $224.80 | 37.2% |
| Senior Project Manager | $314.00 | $164.80 | $251.20 | 34.4% |

Margin formula: `(Customer Rate − Cost) / Customer Rate`

**Note:** These are standard list/cost rates. Always check the customer's MSA or HCS agreement for pre-negotiated rates before quoting — customer-specific rates may differ.

---

## Approval Tiers

| Margin | Status | Action |
|--------|--------|--------|
| ≥ 35% | Green — healthy | No approval needed |
| 30–34% | Yellow — acceptable | Monitor; no approval needed |
| < 30% | Red — tight | Manager approval required |
| < 25% | Hard Stop | Not allowed |

---

## Duration Conversions

| Input | Days |
|-------|------|
| 1 week | 5 days |
| 1 month | 22 days |

---

## Execution

### Step 1: Parse the request

Identify:
- **Role(s)** — fuzzy match to the rate card (e.g., "senior arch" → Senior Architect)
- **Duration** — convert weeks/months to days
- **Pricing model** — T&M, fixed price, or HCS drawdown
- **Customer rate override** — if the customer has a pre-negotiated rate, use that instead of List × 0.80

If any role is ambiguous, list the candidates and ask which applies.

### Step 2: Compute per-role estimates

For each role × duration:

```
Customer Rate = List × 0.80  (or pre-negotiated rate if provided)
Total Customer Cost = Customer Rate × Days
Total Cost = Cost Rate × Days
Margin = (Customer Rate − Cost Rate) / Customer Rate
```

### Step 3: Multi-role totals

Sum across all roles:

```
Total Engagement Revenue = Σ (Customer Rate × Days) per role
Total Engagement Cost    = Σ (Cost Rate × Days) per role
Blended Margin           = (Total Revenue − Total Cost) / Total Revenue
```

### Step 4: Approval tier

Apply the approval tier to the **blended margin** for the engagement total.

### Step 5: Fixed-price and HCS context

**Fixed price:**
- Add contingency: [DATA NEEDED: firm standard contingency %]
- Recommended approach: price T&M first, then add contingency to produce fixed price
- Flag scope risk if the engagement is exploratory

**HCS drawdown:**
- CU-to-day conversion rate: [DATA NEEDED]
- Confirm remaining CU balance and contract window before scoping
- Expiring credits are a forcing function — use them as a deal accelerator

### Step 6: Output

```markdown
## Deal Desk — [Engagement Name or Account]

### Role Breakdown

| Role | Days | List Rate | Customer Rate | Revenue | Cost | Margin | Tier |
|------|------|-----------|---------------|---------|------|--------|------|
| [role] | [N] | $[L] | $[CR] | $[total] | $[cost] | [%] | [Green/Yellow/Red] |

**Totals**
| Metric | Amount |
|--------|--------|
| Total Revenue | $[sum] |
| Total Cost | $[sum] |
| Blended Margin | [%] |
| Approval Required? | [Yes / No / Hard Stop] |

### Pricing Model Notes

[T&M / Fixed / HCS context — contingency flag, CU balance reminder, scope risk]

### Recommended Actions

1. [Highest-impact pricing action — e.g., swap role to improve margin]
2. [Approval path if required]
3. [Scope or model adjustment if margin is in Red or Hard Stop]
```

---

## Pricing Principles

1. Price to value, not to cost. What is the outcome worth to the customer?
2. Never lead with price. Establish value and pain first.
3. Phase large engagements. A $500K SOW is harder to approve than two $250K phases.
4. Use HCS credits as an accelerator — "You've already paid for this."
5. Anchor high, negotiate scope, not rate. Protect the rate card; adjust deliverables.
6. Never discount without getting something in return (longer term, larger scope, reference).

---

## Common Scenarios

**"Too expensive"** → See `objection-handling.md` — Pricing Objections section.

**HCS balance is low** → Scope Phase 1 to fit remaining credits. Propose new CU purchase for Phase 2.

**Customer wants fixed price, scope is unclear** → Propose a paid discovery phase (T&M, 2–4 weeks). Use the output to build a fixed-price SOW for the main engagement.

**Delivery team says "this will take longer"** → Revisit scope with the customer before starting. Never absorb overruns silently.

---

## Related Skills

- `pipeline` skill's `ops/forecast.md` — Weighted pipeline forecast and gap-to-quota analysis
- **grill** — Read-only review of exact native ClosePlan qualification evidence
- `contract` skill's `ops/contract-check.md` — Validate a draft against account contract terms
