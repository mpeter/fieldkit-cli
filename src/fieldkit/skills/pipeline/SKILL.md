---
name: pipeline
description: >
  Read local pursuit forecasts, timeline and linkage risks, or delivery-project
  contract-date health with shipped fieldkit commands.
metadata:
  opencode/slash: "true"
  category: product
---

# Review pipeline and project snapshots

Use this skill for one of three read-only local reports:

- [Pipeline health](ops/pipeline-health.md) ranks pursuit timeline and Salesforce
  linkage findings.
- [Forecast](ops/forecast.md) calculates deterministic commit, weighted, and
  best-case amounts from pursuit frontmatter.
- [Engagement health](ops/engagement-health.md) classifies delivery projects by
  their recorded contract end dates.

These commands read the configured fieldkit workspace; they do not query
Salesforce, ClosePlan, email, calendars, or another external service. They also
do not establish that local frontmatter is current. State the source and its
recorded date when one is present, and call missing currentness evidence
unavailable.

## Choose the report

- Run `fieldkit pursuit health --json` for all locally tracked pursuit risks, or
  add `--account ACCOUNT` for a confirmed literal account directory slug.
- Run `fieldkit pursuit forecast --json` for the local forecast, or add
  `--account ACCOUNT` and an optional `--quota AMOUNT`.
- Run `fieldkit pursuit projects --json` for delivery-project contract dates, or
  add `--account ACCOUNT`.

For `health`, `--account` is a validated literal account directory slug. For
`forecast` and `projects`, it is a filesystem pattern: supply a confirmed
literal slug without a wildcard or path separator, either of which can broaden
or change the scan. Check the reported account scope before using the result.

Use the installed command's `--help` before relying on optional flags. Do not
substitute the weekly `fieldkit pipeline` document generator for these
deterministic reports; it is a separate command with different output and write
behavior.

## Preserve evidence boundaries

The reports calculate from local files. A successful exit proves that the files
were readable and classified, not that Salesforce is synchronized or the
underlying business state is correct. Do not call a result “today's Salesforce
state” unless a separately authorized refresh and read-back prove that claim.

Pipeline health deliberately reports qualification as unavailable. Never revive
historical local MEDDPICC scores, model synthesis, or arithmetic gate gaps as
current qualification evidence.

All three report commands are read-only. If a report suggests a change, show the
source file, observed value, proposed update, and owning workflow before asking
for approval. Salesforce refreshes belong to the separate `sf-sync` skill and
must not run automatically from this skill.
