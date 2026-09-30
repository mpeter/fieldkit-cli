---
last_reviewed: 2026-09-29
covers:
  - src/fieldkit/commands/datasync/cli.py
  - src/fieldkit/commands/pursuit/
  - src/fieldkit/pursuit/
  - src/fieldkit/commands/pipeline/cli.py
  - src/fieldkit/pipeline/
  - src/fieldkit/commands/sf/
  - src/fieldkit/sf/quota.py
  - src/fieldkit/commands/ingest/run.py
  - src/fieldkit/ingest/replay.py
  - src/fieldkit/ingest/prepared.py
  - src/fieldkit/commands/ingest/reprocess.py
  - src/fieldkit/ingest/reprocess_replay.py
  - src/fieldkit/ingest/reprocess_journal.py
audience: ae-user
---

# Pipeline Workflow

## Overview

The pipeline workflow is a four-step sequence for keeping your pursuit files
accurate and your forecast current:

1. **Sync** — process local ingest data and optionally refresh configured providers
2. **Audit** — validate pursuit structure and qualification-independent timeline risks
3. **Advance** — evaluate current stage policy, then move a pursuit or record an override
4. **Forecast** — generate a pipeline forecast from your pursuit files

Use the steps that match your task. Audit and forecast can run on existing local
pursuits without syncing a provider or advancing a stage first.

## Step 1: Sync your data

```
fieldkit sync
```

Sync plans transcript ingest discovery and processing plus the local
pursuit-stall watcher. Selected-provider preflight must succeed before steps run. Gmail runs only when its integration is configured,
Backstory health runs only when its explicit endpoint is configured, and the
summary identifies optional inputs that were not run. Salesforce and Slack are
never inferred from credentials or account text; select them explicitly with
`--sf` and `--slack`.

Key flags:

- `--quick` — skip configured Gmail phases
- `--sf` — include `fieldkit sf listview` as a final step
- `--slack` — include the optional Slack thread watcher
- `--dry-run` — show what would run without reading credentials, contacting providers, or executing steps
- `--account SLUG` — scope people-index rebuilding, Gmail account tagging and
  enrichment, and selected watchers to one account; it does not scope Gmail sync,
  transcript discovery or processing, or the optional Salesforce listview step
- `--verbose` — show a bounded tail of subprocess stdout/stderr (up to 100 lines and 16,384 characters per stream)

Output is a step-by-step table showing each phase and its result, followed by a
summary line. Provider runtime depends on only the integrations selected for
that run; the local base path has no provider-duration estimate.

### Resume interrupted transcript ingestion

Run `fieldkit ingest run --pipeline transcript-ingest` again after an interrupted
run. Only one transcript run may use a given pipeline database at a time. A busy
run exits `1`; wait for the active run to finish rather than deleting its lock.

Before writing output, transcript ingestion saves the prepared note and exact
task decisions in the local pipeline database. A non-dry rerun reclaims
interrupted sources. Sources with retained prepared output replay those saved
decisions without fetching the document, calling an LLM, or reclassifying tasks.
Sources interrupted before preparation still need the normal processing inputs
and credentials. Dry runs do not reclaim interrupted sources.

Completion requires the meeting note, every selected pursuit update, and the
saved active-task updates to succeed. Waiting-on decisions are not automatically
added to `TASKS.md`; use `fieldkit ingest promote` to triage them. A failed output
write leaves the prepared intent available for retry and makes the run exit `1`.
Other sources may already have completed.

Keep the ownership comments in generated notes, pursuit activity entries, and
tasks. Matching ownership lets a retry preserve your edited text; missing,
conflicting, or ambiguous ownership can stop replay. Restore a missing selected
pursuit or repair the reported output conflict before retrying. Do not delete
prepared checkpoints to force completion. See
[troubleshooting](../reference/troubleshooting.md) for recovery guidance and
[privacy](../privacy.md) for retained local content.

This recovers from process interruption. It is not an atomic transaction across
all files and does not promise recovery from power loss.

### Resume interrupted meeting-note reprocessing

Retry `fieldkit ingest reprocess --pipeline transcript-ingest` with the original
`--from-version` selection after an interrupted replacement. Reprocessing saves
the exact replacement in the pipeline database before publishing it. Retained
replacements finish before fresh provider work; recovery does not fetch the
document or call an LLM. If the replacement was already written, recovery verifies
its exact contents and permissions before completing the database update.

The selection must include every retained replacement. If `--account`,
`--from-version`, or `--limit` excludes pending recovery, the command exits `1`
without applying replacements. Account selection matches a literal,
case-sensitive path segment. A skipped or failed recovery stops the batch;
any failed fresh artifact also stops further processing. A dry-run with retained
recovery exits `1` and leaves it pending.

Repair conflicting file contents, ownership, permissions, or workspace paths
before retrying; do not remove the recovery journal. If saved replacements target
an older pipeline version than the installed pipeline, recovery finishes those
replacements and exits `1`; invoke reprocessing again for newer work. File
replacement and database completion are not one atomic transaction, and this
does not promise power-loss recovery.

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

For one local pursuit at `acme-corp/pursuits/acme-corp-q3.md` with stage
`discover`, gate status `pending`, a null `last-transition`, an empty
`transition-history`, and no other findings, the summary is:

```
Files scanned : 1
Compliant     : 1
  closed-won  : 0
Errors        : 0
Warnings      : 0
Critical flags: 0

Details:
  ✓ acme-corp/pursuits/acme-corp-q3.md — qualification unavailable (0 finding(s))
```

The default command also writes a non-empty Markdown report beneath the
workspace's `accounts/.audit/` directory and prints its location after the
summary. It does not change pursuit files unless `--fix` is requested. Use
`--json` for machine-readable results without the report write. Exit `0` means
no findings, `1` means findings need attention, and `3` means invalid
configuration or no pursuit files in the selected scope. Compliance does not
prove current qualification.

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
fieldkit pursuit advance PURSUIT_SPEC --dry-run
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

For the same `discover` pursuit, the preview exits `1` and leaves files unchanged.
After the line identifying the resolved pursuit path, it prints:

```
Current:  discover
Target:   validate

Gate: … PENDING — current qualification policy unavailable:
  Salesforce-native qualification policy is not yet ratified for this transition

[dry-run] Gate pending — would NOT advance (use --override REASON to force)
```

After reviewing the preview, remove `--dry-run` only when you intend to apply the
transition. A successful write updates `stage` (not `sf_stage`), updates
`last-transition`, and appends to `transition-history`. Transitions
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

For three fictional pursuits with standard contract types, the following
illustrative calculation shows the scenario totals. This is not literal terminal
formatting; the command also displays pursuit paths, close dates, and the current
date. Commit includes the full ACV of negotiate and closed-won pursuits, whereas
weighted applies each stage's probability.

```
| Deal                | Stage     | Weight | ACV      |
|---------------------|-----------|--------|----------|
| acme-corp-expansion | propose   | 50%    | $450,000 |
| midwestins-expand   | propose   | 50%    | $120,000 |
| globalpay-renewal   | negotiate | 75%    | $800,000 |

Commit      : $800,000  (negotiate + closed-won)
Weighted    : $885,000
Best Case   : $1,370,000
Closed Won  : $0
```

For standard or unknown contract types, ACV is resolved from `sf_consulting_acv`,
then `sf_acv`, then `sf_arr` in the pursuit frontmatter. A `fixed_price` contract
prefers `sf_acv` before `sf_consulting_acv`, then falls back to `sf_arr`. Explicit
zero values are preserved. Best Case includes active deals but excludes
closed-won and closed-lost pursuits; Closed Won reports won ACV separately.

Key flags:

- `--account SLUG` — scope to one account
- `--quota AMOUNT` — override the quota target for gap calculation
- `--json` — machine-readable output

## Pipeline quota

```
fieldkit pipeline quota
```

Set your quota in the active fieldkit `config.yaml` first. By default that is
`~/.config/fieldkit/config.yaml`; with an absolute `XDG_CONFIG_HOME`, use
`$XDG_CONFIG_HOME/fieldkit/config.yaml`. Replace this example's target and period
with your own reporting values; the period accepts a calendar half-year
(`YYYY-H1` or `YYYY-H2`) or quarter (`YYYY-Q1` through `YYYY-Q4`):

```yaml
pipeline:
  quota:
    target: 5000000
    period: "2026-H2"
```

With no local pursuit files, the output after its period/date heading is:

```
  Weighted pipeline                     : $0
  Closed-won (configured pursuits only) : $0
  Quota target                          : $5,000,000

  Attainment gap: n/a — pursuit-scope closed-won is not comparable
  to a full-book quota. Pass --source sf to pull live
  territory-scoped attainment from Salesforce.
```

The default source reads local pursuits only. Its closed-won total is not a
full-book attainment figure, so the human-readable report deliberately omits a
numeric attainment gap and JSON output returns `gap: null`. The optional
`--source sf` mode requires authorized
Salesforce access and territory configuration; it uses live fiscal-year,
territory-scoped closed-won revenue for that gap. A zero local total does not
prove that Salesforce has no closed-won revenue.

## SF listview

```
fieldkit sf listview
```

This reads your current Salesforce pipeline, matches opportunities to local pursuits,
and by default updates their cache and Salesforce frontmatter before printing a
summary. Add `--dry-run` to read and match without those local writes; it still
contacts Salesforce. Use the untracked-opportunity summary to spot records not yet
represented locally.

Add `--json` for machine-readable results; it does not disable writes.

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

fieldkit always fetches quote lines through the UI API `related-list-records`
route, without first attempting SOQL or SOSL. This supports CPQ objects that do
not allow queries; the route is not a query-error fallback.

Add `--json` for machine-readable output.

A successful read exits 0, including a quote with no line items. Missing session
or organization configuration, or authentication failure during either lookup,
exits 2; refresh credentials with `fieldkit auth sf` when needed. If the header
or quote-line API lookup fails, the command exits 1 with retry guidance and emits
no successful summary or JSON payload. A failed lookup is never presented as an
empty quote.

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
- `scored_zero` — at least one score is recorded and every recorded score is zero;
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

For a separately reviewed guarded score writer, `Mode = Answers` is the required
initial policy, together with complete native choices and proven score,
ownership, maximum, version, and updateability metadata. This is a writer-design
constraint, not a mutation capability of this reader. All modes remain readable
and read-only; the current command performs no mutation.

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
reviewed guarded writer is required before any mutation. The timestamp is not
represented as an ETag or a strong token, and the reader reports
`mutation_enabled: false`. The read command does not prove that a conditional
write will succeed against a live service.
See Salesforce's
[conditional request](https://developer.salesforce.com/docs/platform/api-rest/guide/intro-rest-conditional-requests.html)
and [sObject Rows](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-retrieve-patch.html)
documentation.

`<opp_id>` is the 15- or 18-character Salesforce Opportunity ID.

Not every opportunity has a ClosePlan scorecard. When no ClosePlan is linked, the
command reports `not_found`, returns an empty `deals` collection, and exits 0.
