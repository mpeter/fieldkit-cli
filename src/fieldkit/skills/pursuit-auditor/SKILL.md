---
name: pursuit-auditor
description: >
  Pursuit files have accumulated over time and you need to confirm frontmatter compliance
  and timeline risks before a push, a pipeline review, or a sync. Audits every pursuit
  file without interpreting historical local qualification values and produces a dated
  compliance report with structural and timeline findings called out.
  Trigger with "audit pursuits", "check frontmatter", "qualification availability",
  "frontmatter compliance", "validate pursuit files", "pursuit health check",
  "missing fields audit", "compliance check", "pursuit compliance", "frontmatter audit".
metadata:
  opencode/slash: "true"
  category: product
---

# Pursuit Auditor

Audits every pursuit file for frontmatter structure, SF field naming, transition
history, Backstory contamination, and timeline risks. Writes a dated report to
`accounts/.audit/`. It performs no live ClosePlan read, so current qualification is
reported as `unavailable`; historical local values are never interpreted.

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Do not use audit output as a qualification pass or stage-advance decision**
- **Always surface generated output for review before any external send**

## Quick Reference

```bash
# Audit all accounts
fieldkit pursuit audit

# Audit a single account
fieldkit pursuit audit --account acme-corp

# Preview — show findings without writing report
fieldkit pursuit audit --account acme-corp

# Auto-fix hyphenated SF field names
fieldkit pursuit audit --fix

# Write report to custom path
fieldkit pursuit audit --output /tmp/my-report.md
```

**Exit codes:**
- `0` — all files compliant
- `1` — one or more files have errors or warnings
- `3` — data error (config missing, accounts directory not found)

## What It Checks

The command runs deterministic Python validation — no LLM, no MCP required:

- **Frontmatter structure:** required fields (`stage`, `gate-status`, `last-transition`, `transition-history`) and allowed stage/gate-status values
- **SF field naming:** hyphenated `sf-*` keys are errors; rename to `sf_*` equivalents
- **Backstory write prohibition:** frontmatter values containing `[Backstory` are flagged as errors
- **Qualification availability:** `unavailable` in this local audit; use `/grill` for a current read-only ClosePlan review
- **Historical containment:** former `meddpicc` input and `legacy_meddpicc`
  surfaces are historical only; the audit does not total or validate their element values
- **Timeline risks:** overdue open opportunities, near-term close dates at an early stage, and missing next steps

## Fix Mode

`--fix` auto-corrects:
- Renames hyphenated `sf-*` fields to underscore equivalents
- Removes legacy `sf-opportunity-number` (hyphen form) fields; the underscore form `sf_opportunity_number` is canonical and must not be removed

Does **not** add missing required fields or alter historical qualification values
(both require separate authorization and, where applicable, human judgment).

## When to Use

- After `fieldkit sf listview` or `fieldkit sf opportunity` to check for field drift
- Before pipeline reviews to surface structural and timeline risks
- After bulk frontmatter edits to verify correctness
- Run from grill (pursuit review op) or the `pipeline` skill's `src/fieldkit/skills/pipeline/ops/forecast.md` to get a compliance baseline

For current qualification, run `/grill` or `fieldkit sf meddpicc <opp_id> --json`.
Treat `read` as observed state, not a pass; ambiguity or missing interpretation
metadata is `pending`, and a failed/missing read is `unavailable`.

## Report Location

Written to: `<data-root>/accounts/.audit/pursuit-compliance-YYYY-MM-DD.md`

Overwritten if run again on the same day.
