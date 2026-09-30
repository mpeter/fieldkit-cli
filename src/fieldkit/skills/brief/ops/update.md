# Mid-session update

Summarize what changed since a stated baseline and propose follow-up actions.
This workflow is distinct from the [daily brief](../SKILL.md) and
[weekly planning](week-start.md). It is not a CLI command with a
`--comprehensive` flag.

## Establish the baseline

Confirm the accounts and time interval. Read available workspace tasks, pursuits,
and relevant account context. If no earlier snapshot or dated change record
exists, describe current state rather than claiming a change since the last run.

Resolve paths through the configured workspace and data locations. A missing
memory file does not mean initialization failed, and a private harness memory
layout is not a fieldkit prerequisite.

## Review local evidence

Compare tasks with available pursuit stages, close dates, and dated transitions.
Suggest actions for approaching deadlines, unresolved follow-ups, and missing
Salesforce linkage. Do not infer a current qualification score from local
historical fields.

Report missing, invalid, or old source timestamps. Seven days for Salesforce
pulls and ninety days for contact verification can be useful review heuristics,
but they are not freshness guarantees. A recent cached record may still be
wrong. Re-reading a cache does not justify updating a contact's verification date.

Use the installed pursuit audit when an audit report is requested; its report
writes must be in scope. Do not invoke an assumed `pursuit-auditor --fix`
interface or automatically rewrite frontmatter. Review findings and obtain
approval for specific corrections.

## Optional communication review

Only inspect communication sources when authorized for the intended accounts and
date range. Establish available tools, authentication, pagination, result limits,
and a finite timeout before a live read. No private MCP server name, Slack CLI,
or Google Workspace CLI installation is assumed.

- Gmail: prefer an existing authorized cache for a local review. A live sync
  changes cache state and is a separate operation; select explicit message bounds
  and an isolated cache for a rehearsal rather than starting an unbounded sync.
- Calendar: distinguish complete results from a limited or interrupted page set.
  A missing prep task is a proposal, not proof a meeting lacks preparation.
- Slack: identify channel scope and source timestamps. Do not assume every
  accessible channel is internal-only or copy private message bodies into
  public evidence.
- Engagement providers: attribute signals and frame suggested tasks as questions
  to verify. Do not promote inferred signals into pursuit or account facts.

Authentication errors and incomplete reads remain explicit failures or gaps.
Independent local review may continue, but the requested live review is not
complete. Empty complete results and unavailable results are different outcomes.

## Triage and persistence

Present overdue tasks, potential commitments, follow-up drafts, and possible new
contacts with their evidence. Ask whether to complete, reschedule, retain, or
remove each ambiguous item. Unknown shorthand needs clarification, not an
invented account or person mapping.

Do not send messages, update CRM records, save private memory, or overwrite a
watcher cache merely because an update was requested. Agree on each write's
destination and contents first.

For requested task reconciliation, use the
[task-sync workflow](../../task-sync/SKILL.md). Do not manually overwrite its
managed region; retain anchors and require complete remote read-back before
claiming synchronization. Saving a draft follow-up is not sending it.

## Report

Separate observed changes from current-state observations and proposals. State
the interval, source coverage, freshness limits, failures, and approved writes
actually verified. Do not claim all accounts were scanned from a truncated
result set or label a partial communication scan complete.
