---
name: contact
description: >
  Contact intelligence and CRM hygiene for accounts — look up a person, research a
  competitor before a call, or discover and enrich sparse contact records. Fronts
  `fieldkit contact find/list/enrich/report`. Trigger with "look up [name]", "find
  contact [email]", "who is [person]", "contact profile for [name]", "background on
  [name]", "competitive intel", "battlecard for [competitor]", "we're up against
  [competitor]", "research [competitor]", "competitive positioning", "how do we beat
  [competitor]", "enrich contacts", "find contacts for [account]", "discover
  contacts", "who should I know at [account]", "contact discovery", "contact
  enrichment", or "refresh contact data".
metadata:
  opencode/slash: "true"
  category: ops
---

# Contact Skill

Contact intelligence for accounts: resolve a person to a unified profile, build a
competitive battlecard, or run the multi-source contact discovery/enrichment
pipeline. Backed by `fieldkit contact find/list/enrich/report`.

Routes: `tvly search` for public competitor/contact research and optional
Backstory reads through the configured account-intelligence route. Directory
enrichment requires a separately configured, authorized directory route.
Contact and pursuit data are read directly from disk (native file reads).

## Folded Ops

This skill absorbs 3 previously-standalone skills as on-demand references. Read the
relevant file when the request matches:

- `ops/contact-lookup.md` — resolving a name or email to a unified contact profile
- `ops/competitive-intel.md` — researching a competitor and building a battlecard
- `ops/contact-enrich.md` — multi-source contact discovery and enrichment pipeline

## Gotchas

- **Backstory availability** — check the configured route only when selecting authorized Backstory reads; `tvly` and local CLI work do not depend on it
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one
- **Missing context** — this skill relies on vault files being up to date; run `/brief` first if signals are stale

## Constraints

- **Read the relevant reference files before acting** — don't guess tool parameters
- **Never modify pursuit frontmatter without explicit instruction**
- **Always confirm before writing back** to any account file or Salesforce

## CLI Surface

```bash
fieldkit contact find "<query>" [--affiliations] [--json]
fieldkit contact list
fieldkit contact enrich [--discover|--apply-web]
fieldkit contact report
```

---

Folded from contact-lookup, competitive-intel, contact-enrich (D1 skill taxonomy,
Wave 3, PR3).

## Route contract

Use [tool routing](../tool-routing/SKILL.md) and the active harness configuration
to select the configured account-intelligence route, when available and authorized.
Use only its permitted read capabilities; tool names and aliases come from that
configuration. If a source or required read is unavailable, note the gap and
continue with authorized local and CLI sources. Do not substitute another group
or identity. Google operations use the configured authorized identity; Google
writes use `gws`, retain the workflow's approval boundary, and require read-back
verification.
