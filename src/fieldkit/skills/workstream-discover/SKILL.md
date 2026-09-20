---
name: workstream-discover
description: >
  You have an active delivery relationship and want to surface expansion opportunities or
  stalled workstreams before an account planning or QBR session. Identifies candidate
  workstreams grounded in real customer priorities, not product lists.
  Trigger with "discover workstreams", "what workstreams are active", "workstream status",
  "find all workstreams", "active deliverables", "delivery workstreams",
  "workstream discovery", "expansion opportunities", "what can we grow at [account]".
metadata:
  opencode/slash: "true"
  category: product
---

# Workstream Discovery Skill

## Gotchas

- **Stale vault signals** — run `/brief` first if data hasn't been refreshed today
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Always surface generated output for review before any external send**

## Purpose
Surface candidate expansion workstreams in a landed account.
Every workstream candidate must: (1) connect to a real customer pain or priority,
(2) have a natural bridge from existing engagement, and (3) have a plausible champion.
A list of vendor products is not an expansion plan.

## Input Sources (use all available)

Routes needed: **fieldkit-sales** (Backstory MCP exception), `gws gmail` (Gmail threads), `gws drive` (SOWs and delivery docs), and `tvly` (web research). Account/project files are read and edited directly on disk.

- Backstory (fieldkit-sales) — account activity, engagement signals, new stakeholder activity
- Slack — `slackcli search messages "<account name>"` — scope creep signals, delivery team concerns
- Google Drive (`gws drive`) — SOWs, project status reports, delivery docs
- Gmail (`gws gmail`) — recent threads for signals of new initiatives, pain, exec changes
- Salesforce pursuit frontmatter — open opportunities, past closed deals, renewal dates
- Web search (`tvly search`) — customer earnings calls, press releases, hiring patterns
- Account file: read `accounts/<account>/account.md`
- Active delivery projects: `ls accounts/<account>/projects/*.md` — read frontmatter for contract dates, project type, and opportunity linkage.

---

## Signal Types to Look For

### Active Project Signals (from `accounts/<account>/projects/*.md`)
- Contract ending within 90 days — natural renewal + expand conversation
- Consulting Unit projects with unused credits — propose new redemption workstreams
- Multiple projects with the same delivery lead — consolidation or umbrella SOW opportunity
- Project type mismatch (Customer Project doing CU-style work) — pricing model conversation

### Delivery Signals (from Slack + Drive)
- Scope creep requests ("can you also help with X?")
- Delivery team feedback on adjacent pain areas
- Customer requests for capability beyond current SOW
- SOW end dates approaching (creates natural renewal + expand conversation)
- Project success metrics that suggest the customer is ready for more

### Relationship Signals (from People.AI + Gmail)
- New exec engaged — new exec = new agenda = new budget
- Increased meeting frequency with a stakeholder
- Customer sharing problems outside current scope
- Executive sponsor asking "what else can you do?"
- Expansion signals from champion ("have you thought about doing X with us?")

### Market Signals (from Web Search)
- Customer earnings call mentioning a strategic initiative we can support
- Hiring patterns (e.g., hiring AI/ML engineers → RHOAI opportunity)
- Announced IT modernization initiatives
- Competitive pressure on their business creating urgency
- Regulatory or compliance changes creating new needs

---

## Vendor Expansion Playbook

Common expansion patterns from existing OpenShift Virtualization / migration engagements:

| Current Engagement | Natural Expansion | Bridge Narrative |
|-------------------|------------------|-----------------|
| OpenShift Virtualization migration | RHOAI on same cluster | "You've modernized the platform — now run AI/ML on it without a separate stack" |
| OpenShift Virtualization | AAP for automation | "Manual operations at scale will slow you down — automate day 2 operations" |
| OpenShift (any) | Advanced Cluster Management / GitOps | "Multi-cluster governance becomes critical at this scale" |
| RHEL / OpenShift | Security hardening, compliance | "Platform is in place — now certify and secure it" |
| Platform delivery | Training / enablement | "Sustain the investment — build internal capability" |
| Any active engagement | Extended support / ELS | "Protect the investment with long-term support" |

Adjust for the specific account context. Don't recommend a product — recommend a
solution to a problem they've described or a goal they've stated.

---

## Output Format

```
# Expansion Workstream Analysis — [Account]
Date: YYYY-MM-DD

## Signals Detected

**Delivery Signals:**
- [signal] — Source: [Slack channel / SOW / delivery team] — Date: [date]

**Relationship Signals:**
- [signal] — Source: [email / People.AI / meeting note] — Date: [date]

**Market Signals:**
- [signal] — Source: [web / earnings / press release] — Date: [date]

---

## Candidate Workstreams

### 1. [Workstream Name]

**Customer Problem / Priority:** [one sentence — their language]
**Our solution:** [one sentence — capability, not product name]
**Bridge from Current Engagement:** [why now, why us, why natural]
**Estimated Value:** [ARR range or PLACEHOLDER]
**Proposed Champion:** [name and why they're the right sponsor]
**Proposed Economic Buyer:** [name or "to be identified"]
**Readiness:** [Ready Now / 1–2 Quarters / Longer-Term]
**First Step:** [specific action to test the appetite]

### 2. [Workstream Name]
[repeat structure]

---

## Prioritized Recommendation

**Top Priority:** [Workstream 1 name] — [one sentence on why this is first]
**Rationale:** [evidence from signals, relationship readiness, customer priority alignment]

**Suggested Approach:**
1. [step 1 — e.g., "Raise in QBR as natural follow-on"]
2. [step 2 — e.g., "Schedule dedicated discovery call with [champion name]"]
3. [step 3 — e.g., "Present mini-business case to [EB name] by [date]"]
```

---

## After Generating

1. Edit the `## Expansion Opportunities` body section of `accounts/<account>/account.md` directly (an operator-authored section preserved across `fieldkit sf account` regeneration — never hand-edit the `sf_*` frontmatter)
2. Add highest-priority workstream as a new pursuit file if customer has expressed
   any appetite: `accounts/<account>/pursuits/<workstream-name>.md`
3. If a QBR is coming up, feed this analysis into the `meeting` skill's
   [QBR prep](../meeting/ops/qbr-prep.md) — expansion opportunities should be a named section of every QBR

---

## Related Skills

- `playbooks/expansion.md` — Read the expansion playbook before running this skill
- `meeting`'s [stakeholder map](../meeting/ops/stakeholder-map.md) — Map stakeholders in the expansion area
