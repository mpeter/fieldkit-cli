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

Review local pursuit files for frontmatter structure, SF field naming, transition
history, marked unverified source contamination, and timeline risks. Select the
account and mode before running: ordinary human mode writes a dated report to
`accounts/.audit/`, while `--json` without `--fix` emits findings without a report.
Invoking this agent skill alone does not authorize any write.
It performs no live ClosePlan read, so current qualification is
reported as `unavailable`; historical local values are never interpreted.

## Gotchas

- **Stale sources** — a brief does not refresh source systems; local compliance does not prove current Salesforce data or qualification readiness
- **Trigger overlap with adjacent skills** — confirm you need this skill and not a closely named one

## Constraints

- **Never write to account or pursuit files without explicit confirmation**
- **Do not use audit output as a qualification pass or stage-advance decision**
- **Always surface generated output for review before any external send**

## Quick Reference

For a read-only local findings review, use `fieldkit pursuit audit --account
ACCOUNT --json` without `--fix`. Replace `ACCOUNT` with the confirmed configured
account; omit it only when reviewing all accounts is authorized. This is not a
live integration read.

For proposed repairs, use `fieldkit pursuit audit --account ACCOUNT --fix
--dry-run`. This previews corrections without saving pursuit files or reports.
`--dry-run` requires `--fix` and cannot be combined with `--json`, `--output`,
or `--check-yaml`.

For an approved repair, repeat the scoped command with `--fix` and without
`--dry-run`. Human mode also writes a report. **`--fix --json` still edits
pursuit files**; JSON suppresses only the report, not requested fixes. Show the
destinations and proposed changes first, then obtain explicit write approval.
Do not use `--output` to bypass the approved workspace or overwrite another
file. Resolve and approve a confined destination before any report write.

**Exit codes:**
- `0` — scanned files have no reported errors, warnings, or critical findings
- `1` — one or more reported errors, warnings, or critical findings
- `3` — invalid usage or data, including no pursuit files found

An empty workspace is not a passing audit: it prints a no-files message and
exits 3, including in JSON mode. Missing JSON is not a successful empty array.

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

`--fix` applies the implementation's explicit rename table, not every possible
`sf-*` key. Preview and inspect the actual proposed changes:
- Renames known hyphenated Salesforce fields to their underscore equivalents
- Removes legacy `sf-opportunity-number` (hyphen form) fields; the underscore form `sf_opportunity_number` is canonical and must not be removed

Does **not** add missing required fields or alter historical qualification values
(both require separate authorization and, where applicable, human judgment).
It may canonicalize the former `meddpicc` key to `legacy_meddpicc` while
preserving its historical values. Stop on refused or failed repairs; do not
claim all requested fixes succeeded just because later audit output exists.
Reread each changed file and compare against its reviewed pre-write contents.

## When to Use

- After `fieldkit sf listview` or `fieldkit sf opportunity` to check for field drift
- Before pipeline reviews to surface structural and timeline risks
- After bulk frontmatter edits to verify correctness
- Use alongside grill or the pipeline skill's forecast reference for a local compliance baseline

For current qualification, run `/grill` or `fieldkit sf meddpicc <opp_id> --json`.
Treat `read` as observed state, not a pass; ambiguity or missing interpretation
metadata is `pending`, and a failed/missing read is `unavailable`.

## Report Location

Human-mode reports use the configured workspace's
`accounts/.audit/pursuit-compliance-YYYY-MM-DD.md`. Scoped account reports add
`-ACCOUNT` before `.md`. JSON findings and repair previews write no report.

Repeating the same date and account can overwrite that report. Review an
existing destination and approve replacement before running. A custom output
path requires an existing parent and a separately approved safe destination
inside the configured workspace's `accounts/.audit/`. Relative output names
resolve there; destinations outside that directory or through symlinks are
refused before any repair or report write.
Reports can contain customer context; keep them private, not in public issues
or release evidence. Verify the saved result is nonempty before reporting it
written, and keep read-only findings separate from approved fixes and reports.
