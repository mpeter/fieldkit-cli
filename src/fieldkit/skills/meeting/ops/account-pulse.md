# Account pulse

Use this workflow for a current, cross-account view of delivery, pursuit risk,
and relationship gaps. Use the account-snapshot workflow for one account and
the main meeting workflow for a specific call.

The pulse is a read-only draft. It does not refresh Gmail, change Salesforce,
or update account and pursuit files.

## Choose scope

Read the configured workspace's `config/accounts.yaml`. By default, include
configured accounts that are not marked `internal: true`. For a focused pulse,
require one exact configured account key. Do not guess a key or silently include
an unknown directory.

Record the report date and requested scope before collecting evidence. Use the
installed command registry and each command's current `--help` if an option is
uncertain.

## Collect shipped fieldkit evidence

Run these read-only commands for each account:

```bash
fieldkit pursuit projects --account <account> --json
fieldkit pursuit health --account <account> --json
fieldkit gmail query blindspots <account> --since <YYYY-MM-DD> --limit 10 --json
fieldkit gmail backstory-gap --account <account> --limit 10 --json
```

Treat each invocation independently:

- `pursuit projects` supplies its own `ZOMBIE`, `EXPIRING`, `SOON`, `ACTIVE`,
  and `UNKNOWN` classifications. Preserve them instead of inventing a different
  date threshold.
- `pursuit health` supplies current local risk reasons. Current qualification is
  unavailable in this report; do not substitute historical scores.
- The Gmail commands require a ready published local cache. They do not refresh Gmail or
  prove the cache's coverage. Record the cache date or known coverage when
  available.
- `backstory-gap` supplies CRM review candidates, not proof that a contact is
  absent from CRM. It performs no CRM comparison. Keep that comparison pending
  until an authorized, attributable CRM source establishes the result.
  A work-budget interruption emits `scan_truncated: true` and partial exit `1`;
  retain `scanned_rows` and the incomplete result as pending evidence. Even a
  completed bounded candidate report is not a complete inventory or an absence finding.
- A missing cache, invalid configuration, nonzero exit, or malformed result is
  `unavailable`, not an empty successful result. Continue with the other
  accounts and sources, but preserve the failure.

Read `accounts/<account>/account.md` when present for operator-authored context.
A dated dossier or watcher report may be background evidence when its path,
generation date, and scope are known. Missing files are `unavailable`; stale
files remain dated background and do not become current signals.

## Optional sources

An operator may separately authorize an account-intelligence, Slack, calendar,
or public-research tool. fieldkit does not install or require one. Before use,
inspect the installed tool's current read-only interface, confirm identity and
account scope, bound the query and pagination, and record source and date.

Do not silently switch providers. A provider suggestion is not a customer
commitment, an internal comment is not customer evidence, and failed or partial
retrieval remains `unavailable` or `pending`. Never publish private message
content or customer identifiers as release evidence.

## Classify with traceable evidence

Assign a bucket only when a dated source supports it:

- **Needs action** — a returned delivery or pursuit risk has a concrete next
  step.
- **Active** — a dated record shows current work or engagement.
- **Trigger** — a dated, attributed event merits review; external research is a
  lead until corroborated.
- **Quiet** — the completed evidence scope contains no recent activity. If a
  source was missing or partial, use **Insufficient evidence** instead.

Do not infer sentiment, a relationship owner, a reply obligation, or a customer
priority from thread counts or silence.

## Draft the pulse

Replace sample statuses with observed outcomes. A template row marked `verified`
is not evidence: missing, failed, partial, or unknown coverage stays unavailable
or pending. Keep proposed actions separate from observed facts.

```markdown
# Account Pulse — [date]

## Source status
| Account | Source | As of / scope | Status | Note |
|---|---|---|---|---|
| [account] | pursuit projects | [date] | verified | [coverage] |
| [account] | Gmail cache | [range or unknown] | unavailable | [reason] |

## Needs action
### [account]
- [observed risk] — Source: [source, date]
- Proposed next step: [reviewable recommendation]

## Active
- [account]: [observed activity] — Source: [source, date]

## Triggers to review
- [account]: [event] — Source: [URL or named source, date]; confidence: [level]

## Delivery alerts
- [account / project]: [returned classification and date] — Source: pursuit projects

## Contact review candidates
- [account / contact]: [blindspot or candidate observation] — Source: [command, cache scope]; CRM comparison: not performed

## Insufficient evidence
- [account]: [missing, failed, stale, or partial source]

## Suggested priority
- [recommendation and the evidence it depends on]
```

Present the draft for review. Do not write a file, update Salesforce, edit
frontmatter, or send a message. If the operator asks to save the draft, propose
an exact workspace-relative destination, obtain confirmation, and require
separate overwrite approval when that path already exists.
