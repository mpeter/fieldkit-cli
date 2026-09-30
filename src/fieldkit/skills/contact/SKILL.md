---
name: contact
description: >
  Look up cached contact context, draft source-attributed competitive research,
  or discover and validate local contact records with shipped fieldkit commands.
metadata:
  opencode/slash: "true"
  category: ops
---

# Work with contacts and competitive context

Use this skill for one of three distinct outcomes:

- [Contact lookup](ops/contact-lookup.md) resolves an email address or name from
  the local Gmail people index and optional workspace affiliations.
- [Competitive research](ops/competitive-intel.md) produces a reviewable,
  source-attributed battlecard draft.
- [Contact enrichment](ops/contact-enrich.md) discovers and validates contact
  records already represented in the configured workspace.

No external service is required for local lookup, listing, or discovery. Public
web research, Slack context, calendars, and live CRM data are optional sources;
use them only when an available tool, intended identity, operator authorization,
and bounded scope are established. A missing optional source stays unavailable.

## Choose the shipped command

- Use `fieldkit contact find QUERY --affiliations --json` for a single lookup
  when workspace affiliations are relevant. Omit `--affiliations` when only the
  local people index is in scope.
- Use `fieldkit contact list --account ACCOUNT --limit N --json` for a bounded
  local list. Supply an exact account slug and positive limit.
- Use `fieldkit contact enrich --discover --account ACCOUNT --json` for scoped
  local discovery, then follow the enrichment workflow before any later stage.
- Use `fieldkit contact report --account ACCOUNT --json` to render the current
  local enrichment report for one account.

Inspect the selected leaf's `--help` in the installed version before use. The
find and list commands are read-only. Enrichment and reporting can write personal
contact data beneath the configured workspace, so confirm the workspace, scope,
and expected outputs first.

## Preserve evidence boundaries

Contact cache fields, affiliation text, web pages, internal messages, and model
synthesis are different evidence classes. Carry the source and observation date
with each important claim. Mark conflicts and missing coverage; do not merge them
into an apparently certain profile.

The CLI's initiation and decay labels are cache-derived heuristics. They are not
proof of a person's influence, employment, consent, buying role, or willingness
to advocate. An affiliation found in Markdown can also be stale.

Do not write by default. Show a contact note, account update, battlecard, task,
or CRM proposal before asking to create or change it. Never copy personal contact
data, private messages, or customer context into public logs, issues, fixtures,
or release evidence.
