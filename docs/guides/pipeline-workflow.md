---
last_reviewed: 2026-09-12
covers:
  - src/fieldkit/commands/pursuit/
  - src/fieldkit/pursuit/
  - src/fieldkit/commands/sf/
audience: ae-user
---

# Pipeline Workflow

## Overview

The pipeline workflow is a four-step sequence for keeping your pursuit files
accurate and your forecast current:

1. **Sync** — pull the latest data from Salesforce and Gmail into your workspace
2. **Audit** — validate pursuit structure and qualification-independent timeline risks
3. **Advance** — evaluate current stage policy, then move a pursuit or record an override
4. **Forecast** — generate a pipeline forecast from your pursuit files

Run these steps in order. Each step depends on the output of the previous one.

## Step 1: Sync your data

```
fieldkit sync
```

This runs a multi-phase pipeline: Gmail sync (incremental), ingest discovery and
processing, and watcher updates. By default it does **not** include Salesforce
listview — add `--sf` to include it.

Key flags:

- `--quick` — skip the Gmail sync phases (SF and ingest only)
- `--sf` — include `fieldkit sf listview` as a final step
- `--dry-run` — show what would run without executing
- `--account SLUG` — scope to a single account
- `--verbose` — show full step output (truncated to last 100 lines per step)

Output is a step-by-step table showing each phase and its result, followed by a
summary line. Runtime depends on account count and Gmail volume (typically 60–180
seconds for a full run).

### Import ambient transcripts

Completed ambience-companion sessions under `scratch/ambient/` can enter the
same account meeting-note workflow:

```bash
fieldkit ingest discover --pipeline ambient-transcript-ingest --dry-run
fieldkit ingest discover --pipeline ambient-transcript-ingest
fieldkit ingest run --pipeline ambient-transcript-ingest
```

Discovery excludes the newest JSONL file because it may still be receiving
segments. After the recorder has stopped, pass `--include-latest` to include it.
Discovery output identifies files and hashes without printing transcript text.

Ambient sessions route from configured account keywords in the transcript.
Sessions with no unique account match remain pending and make the run exit 1;
short noise sessions are marked processed without writing a meeting note.

## Step 2: Audit pursuits

```
fieldkit pursuit audit
```

This validates all pursuit files in your workspace for frontmatter structure, SF
field naming, transition history, Backstory contamination, and close-date/timeline
risks. It does not score qualification from local MEDDPICC data. Current native
qualification is reported as `unavailable` because the audit does not perform a
live ClosePlan read.

Output format:

```
Files scanned : 12
Compliant     : 10
Errors        : 1
Warnings      : 1
Critical flags: 0

✗ acme-corp-q3: close date in 12d but stage='discover'  [WARNING]
✓ globalpay-renewal: current qualification unavailable  [COMPLIANT]
```

**Reading audit output:**

- `ERROR` — required structure is missing or the file cannot be parsed. Fix before advancing.
- `WARNING` — a timeline, naming, transition-history, or data-integrity issue needs review.

Old top-level `meddpicc` content is accepted for read compatibility and exposed in
memory as versioned historical `legacy_meddpicc`. It is not required for compliance,
totaled, or used as current qualification. A later separately authorized pursuit
write emits the canonical historical shape without changing the preserved values.

Additional flags:

- `--fix` — auto-correct fixable issues (hyphenated SF fields, legacy field names)
- `--account SLUG` — audit a single account
- `--json` — machine-readable output
- `--check-yaml` — detect duplicate YAML keys (exits 1 if found)
- `--output PATH` — write report to a specific file

## Step 3: Advance a pursuit

```
fieldkit pursuit advance PURSUIT_SPEC
```

where `PURSUIT_SPEC` is the full file path or `<account>/<slug>` shorthand. This
command is **not interactive** — it requires you to specify the pursuit on the command
line.

Key flags:

- `--to STAGE` — target stage (if omitted, advances to the next stage in sequence)
- `--override REASON` — advance past a pending policy with a written justification
- `--dry-run` — show what would change without writing
- `--account SLUG` / `--name SLUG` — alternative to the positional argument

Stage sequence:
`pre-pipeline → prospect → qualify → discover → validate → propose → negotiate → closed-won`

Example output:

```
Pursuit: accounts/acme-corp/pursuits/acme-corp-q3.md
Current:  discover
Target:   validate

Gate: … PENDING — current qualification policy unavailable:
  Salesforce-native qualification policy is not yet ratified for this transition

[dry-run] Gate pending — would NOT advance (use --override REASON to force)
```

The `stage` field in the pursuit frontmatter is updated (not `sf_stage`). The command
also writes to `last-transition` and appends to `transition-history`. Transitions
that formerly depended on local 0–3 scores remain `pending` until a native policy is
ratified; historical scores never make them pass. Qualification-independent
transitions retain their existing behavior. Use `--dry-run` first, and provide an
explicit `--override REASON` only when you intend to proceed despite pending policy.

## Step 4: Forecast

```
fieldkit pursuit forecast
```

This reads all pursuit files and generates a pipeline forecast with weighted and
scenario views.

Output format:

```
| Deal              | Stage     | Weight | ACV        | Close      |
|-------------------|-----------|--------|------------|------------|
| acme-corp-q3      | propose   |  50%   | $450,000   | 2026-08-31 |
| midwestins-expand | propose   |  50%   | $120,000   | 2026-09-15 |
| globalpay-renewal | negotiate |  75%   | $800,000   | 2026-07-31 |

Commit      : $600,000  (negotiate + closed-won)
Weighted    : $685,000
Best Case   : $1,370,000
Closed Won  : $0
```

ACV is resolved from `sf_consulting_acv`, then `sf_acv`, then `sf_arr` in the
pursuit frontmatter.

Key flags:

- `--account SLUG` — scope to one account
- `--quota AMOUNT` — override the quota target for gap calculation
- `--json` — machine-readable output

## Pipeline quota

```
fieldkit pipeline quota
```

Set your quota in `~/.config/fieldkit/config.yaml` first:

```yaml
pipeline:
  quota:
    target: 5000000
    period: "2026-H2"
```

Expected output:

```
Quota Gap (2026-H2)
  Target      : $5,000,000
  Closed-won  : $620,000
  Weighted    : $685,000
  Gap         : $4,315,000
```

## SF listview

```
fieldkit sf listview
```

This pulls your current pipeline view directly from Salesforce and prints a summary
of open opportunities to stderr. Use it to spot opportunities that are in Salesforce
but not yet tracked as pursuit files in your workspace.

Add `--json` to redirect output to stdout in machine-readable format.

## Pipeline review

Generate the global pipeline review and reopen its newest saved artifact:

```text
fieldkit pipeline
fieldkit pipeline open
```

To work with one configured account, use the same account slug for generation
and opening:

```text
fieldkit pipeline --account acme-corp
fieldkit pipeline open --account acme-corp
```

Scoped reviews are saved separately as
`briefs/pipeline-review-<account>-YYYY-MM-DD.md`. The global command continues
to use `briefs/pipeline-review-YYYY-MM-DD.md`. Opening one scope never falls
back to the other, so a missing account review reports the exact generation
command instead of opening a broader document. Add `--json` to `pipeline open`
to receive the selected path, URI, date, age, stale status, and account scope.

## SF quote

```
fieldkit sf quote <quote_id>
```

Reads a Salesforce CPQ Quote and its quote lines directly from Salesforce. Use it to
check a quote's status, net/list/customer amounts, and average discount, or to see
the line-item breakdown (product, quantity, list/net totals, discount) without
opening the Salesforce UI.

`<quote_id>` is the 15- or 18-character Salesforce ID of the `SBQQ__Quote__c`
record. Find it in the Salesforce UI from the Quote record's URL (the ID segment
after `/lightning/r/SBQQ__Quote__c/` and before `/view`), or from the Opportunity's
related Quotes list.

When a CPQ object does not support SOQL or SOSL, fieldkit fetches quote lines
through the UI API `related-list-records` route rather than a query. That behavior
is expected and is not an error.

Add `--json` for machine-readable output.

## SF meddpicc

```
fieldkit sf meddpicc <opp_id> [--deal-id <closeplan_deal_id>] [--json]
```

Reads every ClosePlan/TSPC deal linked to a Salesforce Opportunity and every
question returned for each deal. It never treats the first related record as the
selected scorecard. If exactly one deal is linked, that deal is selected
automatically. If several are linked, the command reports `ambiguous`, prints all
deal IDs and questions, exits 1, and requires `--deal-id` to select one exact
deal. An unlinked `--deal-id` reports `invalid_selection` and exits 3.

The read contract preserves the Salesforce org URL, Opportunity ID, every deal
ID, deal template ID, numeric template version, deployment date, template total
maximum, every exact question ID, raw score, plain-text answer, rich-text answer,
native answer value, package question mode, native maximum, and
`LastModifiedDate`. Human output includes exact deal/question/template/choice
IDs plus native maxima, their consistency with the template total, and
completeness; `--json` emits the complete typed contract, including raw answer
fields, directly.

Each element retains all question records, including duplicate names or category
prefixes, and reports one of four aggregate states:

- `unpopulated` — no score or answer exists;
- `answered_unscored` — answer text exists but the score is blank;
- `scored_zero` — at least one score is explicitly zero and no score is positive;
- `scored` — at least one score is nonzero.

The element's `complete` flag and `unanswered_question_ids` keep an unanswered
question visible even when another question in the same element has a score. The
final `gaps` list contains only wholly `unpopulated` elements. An explicit zero is
therefore visible and is never reported as missing. Questions with an unexpected
category prefix remain visible under `unmapped` and in the human
`Unmapped ClosePlan Questions` section.

Field metadata comes from the generic Salesforce
`TSPC__DealQuestion__c` describe response: field type, updateability, calculated
status, precision, scale, length, and active picklist values. Each exact
`TSPC__TemplateQuestion__c` reference adds its template and category IDs, native
`TSPC__Mode__c` question type, maximum, text/shared-score flags, nullable sync
fields, and exact
`TSPC__TemplateQuestionAnswer__c` choices. Choices retain their IDs, labels,
text, maxima, sort order, attitude, and nullable sync fields. The exact deal
template adds its numeric `TSPC__Version__c` and calculated total maximum.
ClosePlan exposes no separate question-weight field: the deployed question
maxima are the native contributions and must sum to the template's calculated
total when both are complete. The JSON contract therefore preserves `weight` as
`null` instead of inventing a fieldkit weight.

Only `Mode = Answers` is eligible for the first guarded score writer, and only
when the complete native choice collection also proves the proposed score plus
ownership, maximum, version, and updateability metadata. Every other mode remains
readable and read-only. The current command itself performs no mutation.

The UI API requests the endpoint-supported maximum page size (100) and accepts a
deal, question, or template-answer collection as complete only when
Salesforce's response `count` equals the number of returned records. Template
and template-question IDs are deduplicated before their exact records and answer
relationships are read. Missing template metadata, mismatched ownership, a
calculated-maximum mismatch, or a missing/invalid collection count propagates
through the question, deal, and whole read as `incomplete` and exits 1. There is
no invented continuation-token contract.

`LastModifiedDate` is exposed only as weak timestamp evidence for Salesforce
REST's documented `If-Unmodified-Since` conditional request. A separately
approved disposable-record probe established that a stale exact-record PATCH is
rejected with HTTP 412 and a fresh conditional PATCH can succeed. The timestamp
is not represented as an ETag or a strong token, and the reader still reports
`mutation_enabled: false`; writes require the separately reviewed guarded writer.
See Salesforce's
[conditional request](https://developer.salesforce.com/docs/platform/api-rest/guide/intro-rest-conditional-requests.html)
and [sObject Rows](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-retrieve-patch.html)
documentation.

`<opp_id>` is the 15- or 18-character Salesforce Opportunity ID.

Not every opportunity has a ClosePlan scorecard. When no ClosePlan is linked, the
command reports `not_found`, returns an empty `deals` collection, and exits 0.
