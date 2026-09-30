# Week start

Review the available evidence and agree on the week's priorities. This is an
agent-assisted workflow, not a CLI subcommand, and has no guaranteed runtime.

## Establish scope and freshness

Confirm the accounts, week, and whether the operator wants a local review or a
live refresh. Read the configured workspace's task and pursuit data. Missing
sources are unavailable, not evidence that no risks exist.

A local [brief preview](../SKILL.md) does not refresh Salesforce or Gmail.
If a multi-source refresh is requested, first preview the exact phases:

```console
fieldkit sync --sf --dry-run
```

The preview does not execute a phase. Explain that the live command can contact
Gmail and Salesforce, update local caches and workspace records, run ingestion,
and run watchers. After approval for that scope, run `fieldkit sync --sf`.
Inspect every phase's reported status and the process exit status. Do not report
all sources refreshed because one phase succeeded. Failed authentication needs
operator action; keep affected source claims unverified.

## Review pipeline and engagement

For a saved pipeline review without model synthesis:

```console
fieldkit pipeline --no-llm
```

This writes a review artifact; obtain permission for that write when the request
was read-only. State source dates and distinguish local stage/close-date values
from a fresh Salesforce read. A stage crossing requires dated transition evidence,
not merely a current stage. Qualification remains pending a live ClosePlan review
for linked pursuits and unavailable without linkage.

Review available project dates and watcher results. Report missing or stale
results explicitly. Optional engagement-provider signals are attributed leads
to verify, not confirmed customer facts; do not assume a particular MCP server
name or authentication session exists.

For a read-only delivery-health report:

```console
fieldkit pursuit projects --json
```

Use the command's returned classifications. A `ZOMBIE`, `UNKNOWN`, or expiring
project is an observation to review, not authorization to close, renew, or edit
the project.

## Optional account snapshots

When requested, load the [account snapshot workflow](../../meeting/ops/account-snapshot.md).
Check its prerequisites and agree on each destination before writing. Count only
non-empty, read-back artifacts as generated; preserve existing files unless
replacement was authorized.

## Agree on priorities

Propose up to five priorities based on dated commitments, approaching close or
project-end dates, documented stalls, and unfinished tasks. Explain the evidence
and uncertainty behind each priority; numeric alert thresholds are review
heuristics, not qualification policy.

Ask the operator to confirm or edit the list. Confirmation of priorities is not
permission to overwrite a managed task region. If persistence or Google Tasks
reconciliation is requested, use the [task-sync workflow](../../task-sync/SKILL.md)
with its account, pagination, write-approval, and read-back requirements.

## Report

Separate observed source freshness, risks, proposed priorities, confirmed
priorities, and completed writes. Give counts only for inspected evidence.
List failed or omitted sources and remaining actions; do not label the entire
workflow complete while a requested step is pending.
