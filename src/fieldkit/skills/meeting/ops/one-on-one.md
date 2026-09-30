# One-on-one update

Use this workflow to prepare a manager-ready pipeline and priority update. The
result is a source-attributed draft, not an automatic status file or Slack post.

Optional request modifiers such as `--since YYYY-MM-DD`, `--qbr-mode`, and
`--slack` describe the requested draft; they are not fieldkit CLI flags. Confirm
an exact lookback start date. A quarterly request changes the reporting window,
but does not create data that the sources do not contain.

## Collect current local evidence

Run the shipped read-only reports:

```bash
fieldkit pursuit forecast --json
fieldkit pursuit health --json
fieldkit pursuit projects --json
```

Use the forecast's returned `closed_won`, `commit`, `weighted`, and `best_case`
values exactly as defined by the command. They are current local scenario totals,
not automatically quarter-to-date or year-to-date results. Preserve skipped or
invalid-data warnings.

Use the health report's returned risk level and reasons. Do not claim that it
detects an arbitrary 21-day stall, qualification gap, champion gap, or executive
buyer gap unless a separate dated source explicitly establishes that fact.
Use the projects report's own delivery classifications for project risk.

To identify events inside the lookback window, read the relevant pursuit records
through the workspace's normal pursuit representation. A closed-won item or stage
advance counts only when `transition-history` provides a parseable transition
date in the window. A bare current stage does not prove when the change happened.

TASKS.md and a watcher report are optional local sources. Use them only when the
file exists, its ownership is understood, and its dates support the claim. Do not
turn an undated task or stale alert into current evidence.

## Optional sources

An operator-authorized account-intelligence or discussion source may add a dated
positive signal. fieldkit does not require or configure that service. Verify the
tool's read-only interface, identity, scope, result bounds, and timestamps before
use. Keep internal commentary distinct from customer statements.

For every source, record a **Source status** of `verified`, `unavailable`,
`pending`, or `failed`. Continue with independent sources after one failure, but
never convert missing, partial, or malformed data into zero activity.

## Draft the update

Recommend a manager ask only as a proposal tied to observed evidence. Do not
invent an owner, deadline, customer commitment, or dollar amount.

Replace sample statuses with observed outcomes. A template row marked `verified`
is not evidence: missing, failed, partial, or unknown coverage stays unavailable
or pending. Keep proposed actions separate from observed facts.

```markdown
## 1:1 Update — [date]

### Source status
| Source | As of / scope | Status | Note |
|---|---|---|---|
| pursuit forecast | [report date] | verified | current local scenarios |
| transition history | [lookback] | unavailable | [reason] |

### Closed / won in the lookback
- [deal and value] — Source: [transition entry, date]
- No verified transitions in the completed scope.

### Wins and positive signals
- [observed event] — Source: [source, date]

### At risk / needs attention
- [deal or project]: [returned reason] — Proposed manager ask: [ask]

### Pipeline numbers
- Closed won (current local scenario): $[N]
- Commit: $[N]
- Weighted: $[N]
- Best case: $[N]

### This week's focus
1. [priority] — Evidence: [source, date]
2. [priority] — Evidence: [source, date]
3. [priority] — Evidence: [source, date]

### Asks / escalations
- [proposed ask] — [evidence and urgency]
```

For a `--slack` request, produce a compact pasteable draft without sending it.
Keep source gaps in the compact version rather than presenting uncertain numbers
as verified.

Present the draft for review. Do not write to `one-on-ones/`, edit account or
pursuit files, or send the update. If the operator asks to save it, propose an
exact workspace-relative path, obtain confirmation, and require separate
overwrite approval when that path already exists.
