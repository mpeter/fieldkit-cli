---
name: grill
description: >
  Read-only ClosePlan evidence coaching and pipeline review using exact Salesforce deal,
  question, choice, maximum, and metadata records. Use for /grill, deal qualification or
  health checks, pursuit reviews, and pipeline overviews.
metadata:
  opencode/slash: "true"
  argument-hint: "[account or pursuit]"
  category: ops
---

# /grill — native ClosePlan evidence review

Current qualification comes from a fresh Salesforce ClosePlan read. Keep Salesforce and pursuit
files unchanged; local scorecards are historical provenance. The guarded writer requires exact
operator-selected scores and review of a CLI preview. `read` is observed, not qualified or forecast-safe.

## Resolve the opportunity first

Resolve exactly one active pursuit before gathering evidence:

- Use a valid Salesforce Opportunity ID directly. A pursuit slug resolves through
  `accounts/*/pursuits/<slug>.md`; names search pursuit titles and accounts.
- Multiple pursuit matches: list account, pursuit, stage, and Opportunity ID and ask the
  operator to select one.
- No match or no `sf_opportunity_id`: report **Native Qualification: unavailable**.

## Read the exact native contract

Always obtain the current scorecard through the installed CLI (use `uv run fieldkit` only when verifying an uninstalled worktree change):

```bash
fieldkit sf meddpicc <opp_id> --json
```

Interpret the top-level result before coaching:

| Reader result | Native Qualification | Required response |
|---|---|---|
| `complete` with one `selected_deal_id` | `read` | Review that exact deal. |
| `ambiguous` | `pending` | List every returned deal ID and name. Ask the operator for one exact ID, then re-read with `fieldkit sf meddpicc <opp_id> --deal-id <closeplan_deal_id> --json`. |
| `invalid_selection` | `pending` | Report that the supplied exact deal ID is not linked and list the returned linked IDs. |
| `not_found` | `unavailable` | State that no ClosePlan is linked. |
| `incomplete` or `complete: false` | `unavailable` | Show `issues`, counts, and incomplete collections. |
| Authentication or Salesforce read failure | `unavailable` | Propagate the failure and required reauthentication/action. |

Several linked deals require the operator's exact `--deal-id`; order and display text are not
selectors. Keep non-zero ambiguity or incompleteness JSON ephemeral and re-read for each review.

## Present every exact native question

Show the selected deal ID/name, template ID/deployment date, question completeness, and
Salesforce package rollups. Preserve `total_score` and `score_ratio` exactly; do not
derive or rescale them.

Present every question in `elements[].questions` and `unmapped` without collapsing shared
categories or wording. Use this table shape:

| Exact question | Question ID | Current native value | Package choices / maxima | Metadata status | Staged evidence | Evidence needed |
|---|---|---|---|---|---|---|

For each row:

- **Exact question:** reproduce `name`; use category only for display grouping.
- **Question ID:** show `question_id`. A missing ID makes that row unavailable for
  identity-bound coaching; never substitute the name.
- **Current native value:** preserve `raw_*`; distinguish null, zero, unanswered, and
  unsupported. Display `answer` never replaces raw fields.
- **Package choices / maxima:** show `question_type` from the package's
  `TSPC__Mode__c`, then enumerate each
  `template_question.answer_choices[]`: answer ID, label/text, `max_score`, and `sort_order`.
  Show question/template-question `score_maximum`, deal `template_total_maximum`,
  `question_maximum_total`, and `native_maximums_consistent`. The package exposes no separate
  question weight; preserve `weight: null`, never infer one, and use `unknown` when absent.
- **Metadata status:** show `template_metadata_status`, `answer_model`, template-question
  and category IDs, `field_metadata`, `metadata_gaps`, and `metadata_issues`. Keep observed
  values visible when metadata is incomplete and mark interpretation **pending**. `updateable`
  describes metadata; it does not authorize writing.
- **Version evidence:** show `template_version`, `LastModifiedDate`, deployment date, and
  concurrency strength. The reader remains mutation-disabled.

Only `question_type = Answers` with complete package choices can enter the future
guarded score-writer path. Keep every other mode readable and read-only; do not infer
edit semantics from its current score, maximum, or visible text.

Review each question independently when categories contain different choices or maxima.
Keep category values and totals native.

## Stage evidence against question IDs

After selecting exact deal and question IDs, gather evidence from pursuit/account notes,
transcripts, Gmail, Salesforce, Slack, or Backstory. Record:

- the exact `question_id` it bears on;
- who provided it, when, and through which medium;
- whether it is direct customer evidence, internal context, or an inference;
- the source location or query needed to verify it.

Ask Salesforce's exact native question, then one concise question testing missing evidence.
Present choices without recommending one; answers only clarify staged evidence.

Slack and Backstory are context, never confirmation. Assign Backstory only with opportunity-specific
evidence. Treat web inference, SalesAI, colleagues, and absent results as context. Customer evidence needs a source and date.

## Optional guarded native score update

Use this only when the operator explicitly selects a package-defined native score for an exact
`Answers` question. Do not infer a choice from evidence, wording, category, or a local 0–3 score.
The command rechecks identity, complete metadata, choices, and Salesforce concurrency:

```bash
fieldkit sf update-closeplan <opp_id> --deal-id <closeplan_deal_id> \
  --score <question_id>=<native_score>
```

Review the plan ID, current/proposed values, and bindings. Only then run the printed
`--confirm <plan_id>` command. Never substitute `set-field`, edit answer text, retry an uncertain
result, or recreate a stale plan. A delayed rollup is `pending`, not a reason to write again.
Report the receipt ID and native result; use a fresh `/grill` read for further coaching.

## Close the deal review

Report:

1. **Native Qualification:** `read`, `pending`, or `unavailable`, with the exact reason.
2. **Selected ClosePlan deal:** exact deal ID and template/version evidence.
3. **Salesforce package rollups:** raw values or `unknown`.
4. **Question review:** every exact question ID, current native value, available package
   choices/maxima, metadata status, evidence, and remaining evidence need.
5. **Priority evidence needs:** 2–3 conversations or artifacts tied to exact question IDs,
   prioritized by business impact and timing.

Native business gate policy is pending ratification, so report evidence rather than an
invented qualification verdict. Never advance stages yourself; the operator uses the
designated pursuit workflow.

## Constraints

- Never invoke a score writer except `fieldkit sf update-closeplan` through its reviewed plan and
  `--confirm <plan_id>` path. Never invoke answer, frontmatter, timestamp, or stage writers.
- Require exact Opportunity, deal, and question IDs before interpretation.
- Show duplicate and `unmapped` questions; missing data stays `unknown`, `pending`, or
  `unavailable`.
- Keep native records and answer text unchanged and ephemeral.

---

# On-demand operation: Pursuit Review

Scan active pursuits and report native ClosePlan qualification as `read`, `pending`, or
`unavailable` without local arithmetic.

## 1. Discover active pursuits

```text
accounts/*/pursuits/*.md
```

Exclude `template.md` and closed stages unless requested. Read each file fully. Deduplicate
Opportunity IDs before Salesforce reads, then map results back to pursuit rows.

Extract only these local fields:

| Field | Source | Fallback |
|---|---|---|
| Deal name | First H1 heading | file name without `.md` |
| Account | Parent account directory | `[unknown]` |
| Stage | `stage` | `[unknown]` |
| Gate | explicit `override` and `override-reason`, otherwise current non-legacy gate status | `pending` |
| Last transition | `last-transition` | `[unknown]` |
| Opportunity ID | `sf_opportunity_id` | none; native status is unavailable |
| SF stage/close date/last pulled | `sf_stage`, `sf_close_date`, `sf_last_pulled` | `[unknown]` |

Treat local scorecards as historical. Preserve explicit overrides and reasons. Render
qualification-dependent stored gates `pending` unless current native policy supports
them. Keep date and stall signals visible.

**Days in stage** is today minus parseable `last-transition`; otherwise `[unknown]`.

## 2. Read native status

For every unique, valid Opportunity ID, run the native JSON command from the earlier
section. Classify each pursuit:

- `✓ read` — complete read with one selected deal; observed, not passed. Preserve package
  rollups.
- `… pending` — ambiguous/invalid deal selection or missing interpretation metadata.
  List exact IDs or metadata gaps.
- `! unavailable` — no Opportunity ID, authentication/read failure, no linked ClosePlan,
  or incomplete collection. State the reason.

For `read`, select one evidence need from an exact unanswered/incomplete question ID by
business impact and timing. If all are observed, write `review current evidence`.

## 3. Pipeline table

Sort by stage (negotiate, propose, validate, discover), then days in stage descending:

```text
## Pipeline Review — [today's date]

| Deal | Account | Stage | Gate | Days | Native Qualification | SF Stage | SF Close Date | Evidence Need | Next Action |
|---|---|---|---|---|---|---|---|---|---|
| [deal] | [account] | [stage] | [gate] | [N or unknown] | [read/pending/unavailable + reason] | [value] | [value] | [exact question ID or reason] | [one imperative] |
```

Every active pursuit gets a row. The Native Qualification cell must include the status
and reason or selected deal ID. Never hide an unavailable row.

Next actions come from observable state:

| Condition | Next action |
|---|---|
| No Opportunity ID | Link the exact Salesforce Opportunity before qualification review. |
| Authentication/read failure | Restore Salesforce read access, then rerun the native read. |
| Several linked deals | Select one exact ClosePlan deal ID. |
| Incomplete collection | Resolve the reported collection issue and reread. |
| Missing question metadata | Verify the named metadata gap; keep interpretation pending. |
| Exact unanswered question | Gather the named evidence for `[question_id]`. |
| Explicit override | Monitor the stated override condition; do not erase its reason. |
| Complete observed questions | Review current evidence and package rollups with the operator. |

## 4. Summary

Summarize rows with `read`, `pending`, and `unavailable`; timing risk; and `sf_last_pulled`
missing or older than 24 hours. Write `None.` for empty categories. Readable native state is not on track.

## 5. Linked delivery projects

```text
accounts/*/projects/*.md
```

For each pipeline account, report a project link only when its exact `sf_opportunity`
identity matches the pursuit. Name overlap is context, not identity.

## Pursuit Review output contract

Every active row includes deal, account, stage, gate, days in stage, Native
Qualification (`read`, `pending`, or `unavailable`), SF stage, SF close date, one exact
question-level evidence need or status reason, and next action. Use `[unknown]` for
missing display fields and keep the row visible.
