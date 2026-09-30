# Week end

Review the week's work, identify open loops, and prepare next-week proposals.
This is an agent-assisted review, not a timed automation or a task-reset command.

## Review hygiene and follow-ups

Confirm the reporting interval and account scope. Read available task and pursuit
records from the configured workspace. Flag missing or old Salesforce pull
timestamps and absent next steps without claiming a fresh Salesforce comparison.

For the implemented pursuit audit:

```console
fieldkit pursuit audit --json
```

Omit the account filter for all accounts; `<all>` is not an account slug. JSON
mode suppresses the report-file write. Exit `1` means findings were reported,
including per-file parse errors; inspect the results for unreadable or malformed
pursuits. Exit `3` means a fatal configuration or input-availability error,
such as a missing accounts directory. If the operator wants the
Markdown report, agree on its destination before running without `--json`.
`--fix` is a separate pursuit mutation that requires a preview and explicit
approval. Audit findings do not prove current ClosePlan qualification.

Identify overdue follow-ups from explicit dates. If an item's creation date
is unknown, report unknown age rather than assigning one.

## Summarize accomplishments

Count completed tasks only when completion evidence falls within the reporting
interval. A checked item without a completion date is not proof it was completed
this week.

Use dated meeting content to identify meetings and dated transition records to
identify stage changes. A note's creation or modification timestamp alone does
not prove a meeting occurred or a pursuit advanced. Deduplicate records and label
drafts, cancellations, and incomplete histories.

## Capture lessons and prepare a manager update

Ask whether the operator wants lessons captured. Agree on a workspace destination
and the exact addition before writing; do not assume a private harness memory
directory. Preserve existing content and read back the addition.

If a manager update is requested, load the
[one-on-one workflow](../../meeting/ops/one-on-one.md), verify its prerequisites,
and agree on a destination. Keep it a draft until reviewed; sending it is a
separate authorized action.

## Propose next-week task changes

Present completed items, carry-forward candidates, and unresolved follow-ups.
Do not automatically clear Today, move managed entries between sections, delete
tasks, or reset Google Tasks.

When reconciliation is requested, use the
[task-sync workflow](../../task-sync/SKILL.md). Preserve task identifiers and
anchors, require confirmation for destructive changes, and verify remote
read-back before regenerating managed local state. A failed or partial read
must not become a deletion or a successful reset claim.

## Report

Separate evidenced accomplishments, uncertain dates, hygiene findings, overdue
follow-ups, reviewed drafts, and proposed task changes. Report persisted or
synchronized changes only after read-back. Include unfinished requested steps;
do not substitute a template's success wording for observed results.
