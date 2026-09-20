---
name: pipeline
description: >
  Pipeline and portfolio health checks across your accounts — scan for stale or
  at-risk deals before a leadership review, generate a probability-weighted forecast
  with gap-to-quota, or check delivery/engagement health before a QBR or renewal.
  Trigger with "pipeline health", "how healthy is my pipeline", "check my deals",
  "deal health overview", "pipeline check", "portfolio health", "any stale deals?",
  "deals at risk", "which deals are slipping?", "forecast", "pipeline forecast",
  "what's my number", "weighted pipeline", "call forecast", "what do I expect to
  close", "scenario analysis", "gap to quota", "what's my coverage", "engagement
  health", "project health", "delivery status", "how are my projects", "what's
  expiring", or "engagement review".
metadata:
  opencode/slash: "true"
  category: product
---

# Pipeline Skill

Pipeline and portfolio health checks across your accounts: scan active deals for
risk before a leadership review, generate a probability-weighted forecast against
quota, or check delivery/engagement health before a QBR or renewal.

Groups needed: none — all three ops read directly from pursuit/project frontmatter
via the `fieldkit pursuit` CLI.

## Folded Ops

This skill absorbs 3 previously-standalone skills as on-demand references. Read the
relevant file when the request matches:

- `ops/pipeline-health.md` — risk-tiered scan of active pursuits (staleness,
  overdue close dates, stage timing, and Salesforce linkage). Current
  qualification is shown as unavailable because this local scan does not fetch
  ClosePlan.
- `ops/forecast.md` — probability-weighted forecast with scenario analysis and
  gap-to-quota
- `ops/engagement-health.md` — delivery project health by contract end date proximity

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been
  refreshed today
- **Trigger overlap between ops** — pipeline-health is a risk scan of deals,
  forecast is a weighted revenue projection, engagement-health is delivery/contract
  status. Confirm which op matches before proceeding

## Constraints

- **Never write to account files without explicit confirmation**
- **Do not modify pursuit frontmatter mid-workflow** — only write at designated
  save steps
- **Always surface output for review before sending externally**

---

Folded from pipeline-health, forecast, engagement-health (D1 skill taxonomy, Wave 4, PR2).
