"""Finite whole-page pipeline guide claims and real offline CLI boundaries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
import yaml
from markdown_it import MarkdownIt

from fieldkit.commands.datasync.cli import MAX_VERBOSE_CHARS, _truncate_output
from fieldkit.pursuit.io import load_pursuit
from fieldkit.pursuit.stages import PIPELINE_STAGES
from fieldkit.sf.meddpicc import normalize_question, normalize_questions
from scripts.check_documentation_contract import fenced_blocks
from scripts.documentation_commands import DOCUMENT_COMMANDS
from scripts.documentation_manual import MANUAL_SCENARIOS
from tests.documentation_workflow_support import invoke_workflow, snapshot_workflow

pytestmark = pytest.mark.integration
PAGE = Path("docs/guides/pipeline-workflow.md")
OWNER = "pipeline_workflow_guide_contract"


@dataclass(frozen=True)
class GuideClaim:
    identifier: str
    classification: Literal["structure", "guidance", "behavior", "manual"]
    evidence: str
    text: str


# Literal reviewed blocks, not fingerprints or fragments matched by keywords.
INVENTORY = (
    GuideClaim(
        "pipeline.b01",
        "structure",
        "page-layout",
        "---\nlast_reviewed: 2026-09-29\ncovers:\n  - src/fieldkit/commands/datasync/cli.py\n  - src/fieldkit/commands/pursuit/\n  - src/fieldkit/pursuit/\n  - src/fieldkit/commands/pipeline/cli.py\n  - src/fieldkit/pipeline/\n  - src/fieldkit/commands/sf/\n  - src/fieldkit/sf/quota.py\n  - src/fieldkit/commands/ingest/run.py\n  - src/fieldkit/ingest/replay.py\n  - src/fieldkit/ingest/prepared.py\n  - src/fieldkit/commands/ingest/reprocess.py\n  - src/fieldkit/ingest/reprocess_replay.py\n  - src/fieldkit/ingest/reprocess_journal.py\naudience: ae-user\n---",
    ),
    GuideClaim("pipeline.b02", "structure", "page-layout", "# Pipeline Workflow"),
    GuideClaim("pipeline.b03", "structure", "page-layout", "## Overview"),
    GuideClaim(
        "pipeline.b04",
        "guidance",
        "operator-guidance",
        "The pipeline workflow is a four-step sequence for keeping your pursuit files\naccurate and your forecast current:",
    ),
    GuideClaim(
        "pipeline.b05",
        "behavior",
        "sync",
        "1. **Sync** — process local ingest data and optionally refresh configured providers\n2. **Audit** — validate pursuit structure and qualification-independent timeline risks\n3. **Advance** — evaluate current stage policy, then move a pursuit or record an override\n4. **Forecast** — generate a pipeline forecast from your pursuit files",
    ),
    GuideClaim(
        "pipeline.b06",
        "behavior",
        "sync",
        "Use the steps that match your task. Audit and forecast can run on existing local\npursuits without syncing a provider or advancing a stage first.",
    ),
    GuideClaim("pipeline.b07", "structure", "page-layout", "## Step 1: Sync your data"),
    GuideClaim("pipeline.b08", "manual", "manual-context", "```\nfieldkit sync\n```"),
    GuideClaim(
        "pipeline.b09",
        "behavior",
        "sync",
        "Sync plans transcript ingest discovery and processing plus the local\npursuit-stall watcher. Selected-provider preflight must succeed before steps run. Gmail runs only when its integration is configured,\nBackstory health runs only when its explicit endpoint is configured, and the\nsummary identifies optional inputs that were not run. Salesforce and Slack are\nnever inferred from credentials or account text; select them explicitly with\n`--sf` and `--slack`.",
    ),
    GuideClaim("pipeline.b10", "structure", "page-layout", "Key flags:"),
    GuideClaim(
        "pipeline.b11",
        "behavior",
        "sync",
        "- `--quick` — skip configured Gmail phases\n- `--sf` — include `fieldkit sf listview` as a final step\n- `--slack` — include the optional Slack thread watcher\n- `--dry-run` — show what would run without reading credentials, contacting providers, or executing steps\n- `--account SLUG` — scope people-index rebuilding, Gmail account tagging and\n  enrichment, and selected watchers to one account; it does not scope Gmail sync,\n  transcript discovery or processing, or the optional Salesforce listview step\n- `--verbose` — show a bounded tail of subprocess stdout/stderr (up to 100 lines and 16,384 characters per stream)",
    ),
    GuideClaim(
        "pipeline.b12",
        "behavior",
        "sync",
        "Output is a step-by-step table showing each phase and its result, followed by a\nsummary line. Provider runtime depends on only the integrations selected for\nthat run; the local base path has no provider-duration estimate.",
    ),
    GuideClaim("pipeline.b13", "structure", "page-layout", "### Resume interrupted transcript ingestion"),
    GuideClaim(
        "pipeline.b14",
        "behavior",
        "prepared",
        "Run `fieldkit ingest run --pipeline transcript-ingest` again after an interrupted\nrun. Only one transcript run may use a given pipeline database at a time. A busy\nrun exits `1`; wait for the active run to finish rather than deleting its lock.",
    ),
    GuideClaim(
        "pipeline.b15",
        "behavior",
        "prepared",
        "Before writing output, transcript ingestion saves the prepared note and exact\ntask decisions in the local pipeline database. A non-dry rerun reclaims\ninterrupted sources. Sources with retained prepared output replay those saved\ndecisions without fetching the document, calling an LLM, or reclassifying tasks.\nSources interrupted before preparation still need the normal processing inputs\nand credentials. Dry runs do not reclaim interrupted sources.",
    ),
    GuideClaim(
        "pipeline.b16",
        "behavior",
        "prepared",
        "Completion requires the meeting note, every selected pursuit update, and the\nsaved active-task updates to succeed. Waiting-on decisions are not automatically\nadded to `TASKS.md`; use `fieldkit ingest promote` to triage them. A failed output\nwrite leaves the prepared intent available for retry and makes the run exit `1`.\nOther sources may already have completed.",
    ),
    GuideClaim(
        "pipeline.b17",
        "behavior",
        "prepared",
        "Keep the ownership comments in generated notes, pursuit activity entries, and\ntasks. Matching ownership lets a retry preserve your edited text; missing,\nconflicting, or ambiguous ownership can stop replay. Restore a missing selected\npursuit or repair the reported output conflict before retrying. Do not delete\nprepared checkpoints to force completion. See\n[troubleshooting](../reference/troubleshooting.md) for recovery guidance and\n[privacy](../privacy.md) for retained local content.",
    ),
    GuideClaim(
        "pipeline.b18",
        "behavior",
        "prepared",
        "This recovers from process interruption. It is not an atomic transaction across\nall files and does not promise recovery from power loss.",
    ),
    GuideClaim("pipeline.b19", "structure", "page-layout", "### Resume interrupted meeting-note reprocessing"),
    GuideClaim(
        "pipeline.b20",
        "behavior",
        "reprocess",
        "Retry `fieldkit ingest reprocess --pipeline transcript-ingest` with the original\n`--from-version` selection after an interrupted replacement. Reprocessing saves\nthe exact replacement in the pipeline database before publishing it. Retained\nreplacements finish before fresh provider work; recovery does not fetch the\ndocument or call an LLM. If the replacement was already written, recovery verifies\nits exact contents and permissions before completing the database update.",
    ),
    GuideClaim(
        "pipeline.b21",
        "behavior",
        "reprocess",
        "The selection must include every retained replacement. If `--account`,\n`--from-version`, or `--limit` excludes pending recovery, the command exits `1`\nwithout applying replacements. Account selection matches a literal,\ncase-sensitive path segment. A skipped or failed recovery stops the batch;\nany failed fresh artifact also stops further processing. A dry-run with retained\nrecovery exits `1` and leaves it pending.",
    ),
    GuideClaim(
        "pipeline.b22",
        "behavior",
        "reprocess",
        "Repair conflicting file contents, ownership, permissions, or workspace paths\nbefore retrying; do not remove the recovery journal. If saved replacements target\nan older pipeline version than the installed pipeline, recovery finishes those\nreplacements and exits `1`; invoke reprocessing again for newer work. File\nreplacement and database completion are not one atomic transaction, and this\ndoes not promise power-loss recovery.",
    ),
    GuideClaim("pipeline.b23", "structure", "page-layout", "### Import ambient transcripts"),
    GuideClaim(
        "pipeline.b24",
        "behavior",
        "ambient",
        "Completed ambience-companion sessions under `scratch/ambient/` can enter the\nsame account meeting-note workflow:",
    ),
    GuideClaim(
        "pipeline.b25",
        "manual",
        "manual-context",
        "```bash\nfieldkit ingest discover --pipeline ambient-transcript-ingest --dry-run\nfieldkit ingest discover --pipeline ambient-transcript-ingest\nfieldkit ingest run --pipeline ambient-transcript-ingest\n```",
    ),
    GuideClaim(
        "pipeline.b26",
        "behavior",
        "ambient",
        "Discovery excludes the newest JSONL file because it may still be receiving\nsegments. After the recorder has stopped, pass `--include-latest` to include it.\nDiscovery output identifies files and hashes without printing transcript text.",
    ),
    GuideClaim(
        "pipeline.b27",
        "behavior",
        "ambient",
        "Ambient sessions route from configured account keywords in the transcript.\nSessions with no unique account match remain pending and make the run exit 1;\nshort noise sessions are marked processed without writing a meeting note.",
    ),
    GuideClaim("pipeline.b28", "structure", "page-layout", "## Step 2: Audit pursuits"),
    GuideClaim("pipeline.b29", "behavior", "audit", "```\nfieldkit pursuit audit\n```"),
    GuideClaim(
        "pipeline.b30",
        "behavior",
        "audit",
        "This validates all pursuit files in your workspace for frontmatter structure, SF\nfield naming, transition history, Backstory contamination, and close-date/timeline\nrisks. It does not score qualification from local MEDDPICC data. Current native\nqualification is reported as `unavailable` because the audit does not perform a\nlive ClosePlan read.",
    ),
    GuideClaim(
        "pipeline.b31",
        "behavior",
        "audit",
        "For one local pursuit at `acme-corp/pursuits/acme-corp-q3.md` with stage\n`discover`, gate status `pending`, a null `last-transition`, an empty\n`transition-history`, and no other findings, the summary is:",
    ),
    GuideClaim(
        "pipeline.b32",
        "behavior",
        "audit",
        "```\nFiles scanned : 1\nCompliant     : 1\n  closed-won  : 0\nErrors        : 0\nWarnings      : 0\nCritical flags: 0\n\nDetails:\n  ✓ acme-corp/pursuits/acme-corp-q3.md — qualification unavailable (0 finding(s))\n```",
    ),
    GuideClaim(
        "pipeline.b33",
        "behavior",
        "audit",
        "The default command also writes a non-empty Markdown report beneath the\nworkspace's `accounts/.audit/` directory and prints its location after the\nsummary. It does not change pursuit files unless `--fix` is requested. Use\n`--json` for machine-readable results without the report write. Exit `0` means\nno findings, `1` means findings need attention, and `3` means invalid\nconfiguration or no pursuit files in the selected scope. Compliance does not\nprove current qualification.",
    ),
    GuideClaim("pipeline.b34", "structure", "page-layout", "**Reading audit output:**"),
    GuideClaim(
        "pipeline.b35",
        "behavior",
        "audit",
        "- `ERROR` — required structure is missing or the file cannot be parsed. Fix before advancing.\n- `WARNING` — a timeline, naming, transition-history, or data-integrity issue needs review.",
    ),
    GuideClaim(
        "pipeline.b36",
        "behavior",
        "audit",
        "Old top-level `meddpicc` content is accepted for read compatibility and exposed in\nmemory as versioned historical `legacy_meddpicc`. It is not required for compliance,\ntotaled, or used as current qualification. A later separately authorized pursuit\nwrite emits the canonical historical shape without changing the preserved values.",
    ),
    GuideClaim("pipeline.b37", "structure", "page-layout", "Additional flags:"),
    GuideClaim(
        "pipeline.b38",
        "behavior",
        "audit",
        "- `--fix` — auto-correct fixable issues (hyphenated SF fields, legacy field names)\n- `--account SLUG` — audit a single account\n- `--json` — machine-readable output\n- `--check-yaml` — detect duplicate YAML keys (exits 1 if found)\n- `--output PATH` — write report to a specific file",
    ),
    GuideClaim("pipeline.b39", "structure", "page-layout", "## Step 3: Advance a pursuit"),
    GuideClaim("pipeline.b40", "behavior", "advance", "```\nfieldkit pursuit advance PURSUIT_SPEC --dry-run\n```"),
    GuideClaim(
        "pipeline.b41",
        "behavior",
        "advance",
        "where `PURSUIT_SPEC` is the full file path or `<account>/<slug>` shorthand. This\ncommand is **not interactive** — it requires you to specify the pursuit on the command\nline.",
    ),
    GuideClaim("pipeline.b42", "structure", "page-layout", "Key flags:"),
    GuideClaim(
        "pipeline.b43",
        "behavior",
        "advance",
        "- `--to STAGE` — target stage (if omitted, advances to the next stage in sequence)\n- `--override REASON` — advance past a pending policy with a written justification\n- `--dry-run` — show what would change without writing\n- `--account SLUG` / `--name SLUG` — alternative to the positional argument",
    ),
    GuideClaim(
        "pipeline.b44",
        "behavior",
        "advance",
        "Stage sequence:\n`pre-pipeline → prospect → qualify → discover → validate → propose → negotiate → closed-won`",
    ),
    GuideClaim(
        "pipeline.b45",
        "behavior",
        "advance",
        "For the same `discover` pursuit, the preview exits `1` and leaves files unchanged.\nAfter the line identifying the resolved pursuit path, it prints:",
    ),
    GuideClaim(
        "pipeline.b46",
        "behavior",
        "advance",
        "```\nCurrent:  discover\nTarget:   validate\n\nGate: … PENDING — current qualification policy unavailable:\n  Salesforce-native qualification policy is not yet ratified for this transition\n\n[dry-run] Gate pending — would NOT advance (use --override REASON to force)\n```",
    ),
    GuideClaim(
        "pipeline.b47",
        "behavior",
        "advance",
        "After reviewing the preview, remove `--dry-run` only when you intend to apply the\ntransition. A successful write updates `stage` (not `sf_stage`), updates\n`last-transition`, and appends to `transition-history`. Transitions\nthat formerly depended on local 0–3 scores remain `pending` until a native policy is\nratified; historical scores never make them pass. Qualification-independent\ntransitions retain their existing behavior. Use `--dry-run` first, and provide an\nexplicit `--override REASON` only when you intend to proceed despite pending policy.",  # noqa: RUF001 -- literal reviewed guide text
    ),
    GuideClaim("pipeline.b48", "structure", "page-layout", "## Step 4: Forecast"),
    GuideClaim("pipeline.b49", "behavior", "forecast", "```\nfieldkit pursuit forecast\n```"),
    GuideClaim(
        "pipeline.b50",
        "behavior",
        "forecast",
        "This reads all pursuit files and generates a pipeline forecast with weighted and\nscenario views.",
    ),
    GuideClaim(
        "pipeline.b51",
        "behavior",
        "forecast",
        "For three fictional pursuits with standard contract types, the following\nillustrative calculation shows the scenario totals. This is not literal terminal\nformatting; the command also displays pursuit paths, close dates, and the current\ndate. Commit includes the full ACV of negotiate and closed-won pursuits, whereas\nweighted applies each stage's probability.",
    ),
    GuideClaim(
        "pipeline.b52",
        "behavior",
        "forecast",
        "```\n| Deal                | Stage     | Weight | ACV      |\n|---------------------|-----------|--------|----------|\n| acme-corp-expansion | propose   | 50%    | $450,000 |\n| midwestins-expand   | propose   | 50%    | $120,000 |\n| globalpay-renewal   | negotiate | 75%    | $800,000 |\n\nCommit      : $800,000  (negotiate + closed-won)\nWeighted    : $885,000\nBest Case   : $1,370,000\nClosed Won  : $0\n```",
    ),
    GuideClaim(
        "pipeline.b53",
        "behavior",
        "forecast",
        "For standard or unknown contract types, ACV is resolved from `sf_consulting_acv`,\nthen `sf_acv`, then `sf_arr` in the pursuit frontmatter. A `fixed_price` contract\nprefers `sf_acv` before `sf_consulting_acv`, then falls back to `sf_arr`. Explicit\nzero values are preserved. Best Case includes active deals but excludes\nclosed-won and closed-lost pursuits; Closed Won reports won ACV separately.",
    ),
    GuideClaim("pipeline.b54", "structure", "page-layout", "Key flags:"),
    GuideClaim(
        "pipeline.b55",
        "behavior",
        "forecast",
        "- `--account SLUG` — scope to one account\n- `--quota AMOUNT` — override the quota target for gap calculation\n- `--json` — machine-readable output",
    ),
    GuideClaim("pipeline.b56", "structure", "page-layout", "## Pipeline quota"),
    GuideClaim("pipeline.b57", "behavior", "quota", "```\nfieldkit pipeline quota\n```"),
    GuideClaim(
        "pipeline.b58",
        "behavior",
        "quota",
        "Set your quota in the active fieldkit `config.yaml` first. By default that is\n`~/.config/fieldkit/config.yaml`; with an absolute `XDG_CONFIG_HOME`, use\n`$XDG_CONFIG_HOME/fieldkit/config.yaml`. Replace this example's target and period\nwith your own reporting values; the period accepts a calendar half-year\n(`YYYY-H1` or `YYYY-H2`) or quarter (`YYYY-Q1` through `YYYY-Q4`):",
    ),
    GuideClaim(
        "pipeline.b59",
        "behavior",
        "quota",
        '```yaml\npipeline:\n  quota:\n    target: 5000000\n    period: "2026-H2"\n```',
    ),
    GuideClaim(
        "pipeline.b60", "behavior", "quota", "With no local pursuit files, the output after its period/date heading is:"
    ),
    GuideClaim(
        "pipeline.b61",
        "behavior",
        "quota",
        "```\n  Weighted pipeline                     : $0\n  Closed-won (configured pursuits only) : $0\n  Quota target                          : $5,000,000\n\n  Attainment gap: n/a — pursuit-scope closed-won is not comparable\n  to a full-book quota. Pass --source sf to pull live\n  territory-scoped attainment from Salesforce.\n```",
    ),
    GuideClaim(
        "pipeline.b62",
        "behavior",
        "quota",
        "The default source reads local pursuits only. Its closed-won total is not a\nfull-book attainment figure, so the human-readable report deliberately omits a\nnumeric attainment gap and JSON output returns `gap: null`. The optional\n`--source sf` mode requires authorized\nSalesforce access and territory configuration; it uses live fiscal-year,\nterritory-scoped closed-won revenue for that gap. A zero local total does not\nprove that Salesforce has no closed-won revenue.",
    ),
    GuideClaim("pipeline.b63", "structure", "page-layout", "## SF listview"),
    GuideClaim("pipeline.b64", "manual", "manual-context", "```\nfieldkit sf listview\n```"),
    GuideClaim(
        "pipeline.b65",
        "behavior",
        "listview",
        "This reads your current Salesforce pipeline, matches opportunities to local pursuits,\nand by default updates their cache and Salesforce frontmatter before printing a\nsummary. Add `--dry-run` to read and match without those local writes; it still\ncontacts Salesforce. Use the untracked-opportunity summary to spot records not yet\nrepresented locally.",
    ),
    GuideClaim(
        "pipeline.b66", "behavior", "listview", "Add `--json` for machine-readable results; it does not disable writes."
    ),
    GuideClaim("pipeline.b67", "structure", "page-layout", "## Pipeline review"),
    GuideClaim(
        "pipeline.b68",
        "guidance",
        "operator-guidance",
        "Generate the global pipeline review and reopen its newest saved artifact:",
    ),
    GuideClaim("pipeline.b69", "manual", "manual-context", "```text\nfieldkit pipeline\nfieldkit pipeline open\n```"),
    GuideClaim(
        "pipeline.b70",
        "guidance",
        "operator-guidance",
        "To work with one configured account, use the same account slug for generation\nand opening:",
    ),
    GuideClaim(
        "pipeline.b71",
        "manual",
        "manual-context",
        "```text\nfieldkit pipeline --account acme-corp\nfieldkit pipeline open --account acme-corp\n```",
    ),
    GuideClaim(
        "pipeline.b72",
        "behavior",
        "review",
        "Scoped reviews are saved separately as\n`briefs/pipeline-review-<account>-YYYY-MM-DD.md`. The global command continues\nto use `briefs/pipeline-review-YYYY-MM-DD.md`. Opening one scope never falls\nback to the other, so a missing account review reports the exact generation\ncommand instead of opening a broader document. Add `--json` to `pipeline open`\nto receive the selected path, URI, date, age, stale status, and account scope.",
    ),
    GuideClaim("pipeline.b73", "structure", "page-layout", "## SF quote"),
    GuideClaim("pipeline.b74", "manual", "manual-context", "```\nfieldkit sf quote <quote_id>\n```"),
    GuideClaim(
        "pipeline.b75",
        "behavior",
        "quote",
        "Reads a Salesforce CPQ Quote and its quote lines directly from Salesforce. Use it to\ncheck a quote's status, net/list/customer amounts, and average discount, or to see\nthe line-item breakdown (product, quantity, list/net totals, discount) without\nopening the Salesforce UI.",
    ),
    GuideClaim(
        "pipeline.b76",
        "manual",
        "manual-context",
        "`<quote_id>` is the 15- or 18-character Salesforce ID of the `SBQQ__Quote__c`\nrecord. Find it in the Salesforce UI from the Quote record's URL (the ID segment\nafter `/lightning/r/SBQQ__Quote__c/` and before `/view`), or from the Opportunity's\nrelated Quotes list.",
    ),
    GuideClaim(
        "pipeline.b77",
        "behavior",
        "quote",
        "fieldkit always fetches quote lines through the UI API `related-list-records`\nroute, without first attempting SOQL or SOSL. This supports CPQ objects that do\nnot allow queries; the route is not a query-error fallback.",
    ),
    GuideClaim("pipeline.b78", "behavior", "quote", "Add `--json` for machine-readable output."),
    GuideClaim(
        "pipeline.b79",
        "behavior",
        "quote",
        "A successful read exits 0, including a quote with no line items. Missing session\nor organization configuration, or authentication failure during either lookup,\nexits 2; refresh credentials with `fieldkit auth sf` when needed. If the header\nor quote-line API lookup fails, the command exits 1 with retry guidance and emits\nno successful summary or JSON payload. A failed lookup is never presented as an\nempty quote.",
    ),
    GuideClaim("pipeline.b80", "structure", "page-layout", "## SF meddpicc"),
    GuideClaim(
        "pipeline.b81",
        "manual",
        "manual-context",
        "```\nfieldkit sf meddpicc <opp_id> [--deal-id <closeplan_deal_id>] [--json]\n```",
    ),
    GuideClaim(
        "pipeline.b82",
        "behavior",
        "scorecard",
        "Reads every ClosePlan/TSPC deal linked to a Salesforce Opportunity and every\nquestion returned for each deal. It never treats the first related record as the\nselected scorecard. If exactly one deal is linked, that deal is selected\nautomatically. If several are linked, the command reports `ambiguous`, prints all\ndeal IDs and questions, exits 1, and requires `--deal-id` to select one exact\ndeal. An unlinked `--deal-id` reports `invalid_selection` and exits 3.",
    ),
    GuideClaim(
        "pipeline.b83",
        "behavior",
        "scorecard",
        "The read contract preserves the Salesforce org URL, Opportunity ID, every deal\nID, deal template ID, numeric template version, deployment date, template total\nmaximum, every exact question ID, raw score, plain-text answer, rich-text answer,\nnative answer value, package question mode, native maximum, and\n`LastModifiedDate`. Human output includes exact deal/question/template/choice\nIDs plus native maxima, their consistency with the template total, and\ncompleteness; `--json` emits the complete typed contract, including raw answer\nfields, directly.",
    ),
    GuideClaim(
        "pipeline.b84",
        "behavior",
        "scorecard",
        "Each element retains all question records, including duplicate names or category\nprefixes, and reports one of four aggregate states:",
    ),
    GuideClaim(
        "pipeline.b85",
        "behavior",
        "scorecard",
        "- `unpopulated` — no score or answer exists;\n- `answered_unscored` — answer text exists but the score is blank;\n- `scored_zero` — at least one score is recorded and every recorded score is zero;\n- `scored` — at least one score is nonzero.",
    ),
    GuideClaim(
        "pipeline.b86",
        "behavior",
        "scorecard",
        "The element's `complete` flag and `unanswered_question_ids` keep an unanswered\nquestion visible even when another question in the same element has a score. The\nfinal `gaps` list contains only wholly `unpopulated` elements. An explicit zero is\ntherefore visible and is never reported as missing. Questions with an unexpected\ncategory prefix remain visible under `unmapped` and in the human\n`Unmapped ClosePlan Questions` section.",
    ),
    GuideClaim(
        "pipeline.b87",
        "behavior",
        "native_metadata",
        "Field metadata comes from the generic Salesforce\n`TSPC__DealQuestion__c` describe response: field type, updateability, calculated\nstatus, precision, scale, length, and active picklist values. Each exact\n`TSPC__TemplateQuestion__c` reference adds its template and category IDs, native\n`TSPC__Mode__c` question type, maximum, text/shared-score flags, nullable sync\nfields, and exact\n`TSPC__TemplateQuestionAnswer__c` choices. Choices retain their IDs, labels,\ntext, maxima, sort order, attitude, and nullable sync fields. The exact deal\ntemplate adds its numeric `TSPC__Version__c` and calculated total maximum.\nClosePlan exposes no separate question-weight field: the deployed question\nmaxima are the native contributions and must sum to the template's calculated\ntotal when both are complete. The JSON contract therefore preserves `weight` as\n`null` instead of inventing a fieldkit weight.",
    ),
    GuideClaim(
        "pipeline.b88",
        "guidance",
        "operator-guidance",
        "For a separately reviewed guarded score writer, `Mode = Answers` is the required\ninitial policy, together with complete native choices and proven score,\nownership, maximum, version, and updateability metadata. This is a writer-design\nconstraint, not a mutation capability of this reader. All modes remain readable\nand read-only; the current command performs no mutation.",
    ),
    GuideClaim(
        "pipeline.b89",
        "behavior",
        "native_metadata",
        "The UI API requests the endpoint-supported maximum page size (100) and accepts a\ndeal, question, or template-answer collection as complete only when\nSalesforce's response `count` equals the number of returned records. Template\nand template-question IDs are deduplicated before their exact records and answer\nrelationships are read. Missing template metadata, mismatched ownership, a\ncalculated-maximum mismatch, or a missing/invalid collection count propagates\nthrough the question, deal, and whole read as `incomplete` and exits 1. There is\nno invented continuation-token contract.",
    ),
    GuideClaim(
        "pipeline.b90",
        "behavior",
        "scorecard",
        "`LastModifiedDate` is exposed only as weak timestamp evidence for Salesforce\nREST's documented `If-Unmodified-Since` conditional request. A separately\nreviewed guarded writer is required before any mutation. The timestamp is not\nrepresented as an ETag or a strong token, and the reader reports\n`mutation_enabled: false`. The read command does not prove that a conditional\nwrite will succeed against a live service.\nSee Salesforce's\n[conditional request](https://developer.salesforce.com/docs/platform/api-rest/guide/intro-rest-conditional-requests.html)\nand [sObject Rows](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-retrieve-patch.html)\ndocumentation.",
    ),
    GuideClaim(
        "pipeline.b91", "behavior", "scorecard", "`<opp_id>` is the 15- or 18-character Salesforce Opportunity ID."
    ),
    GuideClaim(
        "pipeline.b92",
        "behavior",
        "scorecard",
        "Not every opportunity has a ClosePlan scorecard. When no ClosePlan is linked, the\ncommand reports `not_found`, returns an empty `deals` collection, and exits 0.",
    ),
)
EXPECTED_TEXT = tuple(claim.text for claim in INVENTORY)
EXPECTED_IDS = tuple(claim.identifier for claim in INVENTORY)
EVIDENCE_NODES = {
    "sync": (
        "tests/test_documentation_pursuit_workflow.py::test_documentation_sync_account_scope_matches_selected_steps",
        "tests/test_documentation_pursuit_workflow.py::test_documentation_sync_forwards_account_to_in_process_people_index",
        "tests/test_sync.py::test_cli_dry_run_never_preflights_credentials",
        "tests/test_sync.py::test_selected_sync_preflight_failure_maps_to_auth_exit_two",
        "tests/test_sync.py::test_cli_json_reports_optional_inputs_that_were_not_run",
        "tests/test_sync_verbose.py::test_truncate_output_truncation_keeps_tail_not_head",
        "tests/test_sync_verbose.py::test_verbose_help_discloses_output_limit",
        "tests/test_sync_verbose.py::test_truncate_output_single_line_respects_character_cap",
    ),
    "prepared": (
        "tests/test_ingest_run_lock.py::test_busy_run_has_no_database_or_discovery_effects",
        "tests/test_ingest_prepared_store.py::test_new_source_commits_intent_before_output",
        "tests/test_ingest_prepared_store.py::test_replay_batch_needs_no_google_credentials",
        "tests/test_ingest_prepared_store.py::test_command_recovers_real_interrupted_state_only_under_lock",
        "tests/test_ingest_prepared_store.py::test_replay_failure_retains_intent",
        "tests/test_ingest_prepared_store.py::test_replay_missing_pursuit_then_repair_preserves_note_edits",
        "tests/test_ingest_prepared.py::test_prepared_task_replay_preserves_original_positions_and_edits",
        "tests/test_pursuit_effects.py::test_activity_rejects_ambiguous_marker",
        "tests/test_ingest_promote_extended.py::test_auto_task_then_promotion_skips_mine_and_continues_waiting",
    ),
    "reprocess": (
        "tests/test_reprocess_journal.py::test_reprocess_command_recovers_without_credentials",
        "tests/test_reprocess_journal.py::test_reprocess_excluded_recovery_blocks_fresh_work",
        "tests/test_reprocess_journal.py::test_new_recovery_stops_before_second_fresh_artifact",
        "tests/test_reprocess_journal.py::test_reprocess_command_recovers_recorded_version_without_repreparing",
        "tests/test_reprocess_journal.py::test_reprocess_replay_refuses_changed_state",
    ),
    "ambient": (
        "tests/test_ingest_ambient.py::test_discover_cli_dry_run_hides_transcript_text_and_excludes_latest",
        "tests/test_ingest_ambient.py::test_discovery_include_latest_reads_all_stable_sessions",
        "tests/test_ingest_ambient.py::test_process_ambiguous_route_defers_and_restores_pending",
        "tests/test_ingest_ambient.py::test_run_cli_reports_noise_as_successful_skip",
    ),
    "audit": (
        "tests/test_documentation_pursuit_workflow.py::test_documentation_audit_output_and_default_report",
        "tests/test_pursuit_audit_cmd.py::test_cli_no_results_exits_3",
        "tests/test_pursuit_audit_cmd.py::test_write_audit_report_json_flag_suppresses_report_write",
        "tests/test_pursuit_legacy_meddpicc.py::test_former_meddpicc_loads_as_typed_historical_data_without_mutating_file",
        "tests/test_pursuit_legacy_meddpicc.py::test_authorized_model_write_canonicalizes_in_place_and_preserves_content",
        "tests/test_pursuit_audit.py::test_audit_file_missing_stage",
        "tests/test_pursuit_audit.py::test_audit_file_overdue_close_date",
        "tests/test_pursuit_audit.py::test_audit_file_backstory_in_frontmatter",
        "tests/test_pursuit_audit.py::test_audit_file_backstory_in_body",
        "tests/test_pursuit_audit.py::test_audit_file_invalid_gate_result_in_history_produces_warning",
        "tests/test_pursuit_audit.py::test_apply_fixes_renames_hyphenated",
        "tests/test_pursuit_audit_cmd.py::test_cli_check_yaml_with_dups_exits_1",
        "tests/test_pursuit_audit_cmd.py::test_cli_output_path_respected",
    ),
    "advance": (
        "tests/test_documentation_pursuit_workflow.py::test_documentation_advance_preview_is_pending_without_writes",
        "tests/test_pursuit_advance_branches.py::test_advance_cmd_override_advances_despite_failure",
        "tests/test_pursuit_advance_branches.py::test_apply_transition_appends_history_entry",
    ),
    "forecast": (
        "tests/test_forecast.py::test_documentation_forecast_example_matches_implemented_scenarios",
        "tests/test_sf_components.py::test_effective_net_consulting_acv",
        "tests/test_contract_type_acv_anchor.py::test_forecast_fixed_price_anchors_on_net_acv",
    ),
    "quota": (
        "tests/test_pipeline_quota.py::test_documentation_local_quota_output_matches_cli",
        "tests/test_quota_period.py::test_quota_period_end_date_matches_calendar",
        "tests/test_pipeline_quota.py::test_quota_cli_source_sf_shows_territory_scoped_gap",
    ),
    "listview": (
        "tests/test_pipeline_sf_sync_documentation.py::test_documented_listview_json_format_does_not_choose_write_authority",
        "tests/test_pipeline_sf_sync_documentation.py::test_documented_sf_provider_failures_remain_nonpassing_and_never_write",
    ),
    "review": (
        "tests/test_pipeline_cli.py::test_pipeline_open_account_selects_only_newest_matching_review",
        "tests/test_pipeline_cli.py::test_pipeline_open_global_ignores_newer_scoped_review",
        "tests/test_pipeline_cli.py::test_pipeline_open_missing_account_review_fails_without_fallback",
        "tests/test_pipeline_cli.py::test_pipeline_open_without_account_fails_when_only_scoped_review_exists",
    ),
    "quote": (
        "tests/test_sf_quote.py::test_actual_cli_quote_failures_are_sanitized",
        "tests/test_sf_quote.py::test_actual_cli_successfully_empty_quote_lines",
        "tests/test_sf_quote.py::test_actual_cli_missing_quote_configuration",
        "tests/test_sf_quote.py::test_fetch_quote_lines_parses_related_list_records",
        "tests/test_sf_quote.py::test_build_payload_header_fields_mapped",
    ),
    "scorecard": (
        "tests/test_sf_meddpicc.py::test_cli_passes_exact_deal_id_to_native_reader",
        "tests/test_sf_meddpicc.py::test_cli_json_reports_ambiguity_with_all_deal_ids_and_exits_partial",
        "tests/test_sf_meddpicc.py::test_cli_json_reports_incomplete_result_and_exits_partial",
        "tests/test_sf_meddpicc.py::test_cli_json_preserves_named_party_evidence_and_zero_state",
        "tests/test_sf_meddpicc.py::test_cli_json_no_closeplan_returns_empty_normalized_collections",
        "tests/test_sf_meddpicc.py::test_cli_exits_3_for_invalid_opp_id",
        "tests/test_sf_meddpicc.py::test_cli_exits_2_when_auth_error_raised",
        "tests/test_sf_meddpicc_domain.py::test_normalize_questions_distinguishes_population_states",
        "tests/test_sf_meddpicc_domain.py::test_normalize_questions_retains_unknown_and_missing_categories",
        "tests/test_sf_meddpicc_domain.py::test_normalize_question_preserves_exact_identity_raw_fields_and_concurrency",
        "tests/test_sf_meddpicc_domain.py::test_normalize_field_metadata_keeps_only_proven_generic_describe_properties",
        "tests/test_sf_meddpicc_domain.py::test_normalize_questions_keeps_duplicate_presentation_text_as_exact_records",
    ),
    "native_metadata": (
        "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_preserves_mode_version_and_native_maximum_consistency",
        "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_rejects_template_question_ownership_drift",
        "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_rejects_native_maximum_total_drift",
        "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_keeps_non_answers_mode_readable_but_mutation_disabled",
        "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_deduplicates_template_reads_and_preserves_exact_choices",
        "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_propagates_incomplete_template_answer_count",
        "tests/test_sf_client_closeplan.py::test_fetch_closeplan_deals_returns_every_record_with_complete_count_evidence",
        "tests/test_sf_client_closeplan.py::test_fetch_closeplan_deals_marks_count_mismatch_incomplete",
        "tests/test_sf_client_closeplan.py::test_fetch_closeplan_deals_marks_missing_count_incomplete",
        "tests/test_sf_client_closeplan.py::test_fetch_closeplan_questions_returns_all_records_with_complete_count_evidence",
        "tests/test_sf_client_closeplan.py::test_fetch_closeplan_template_answers_reads_exact_choices_with_complete_count_evidence",
        "tests/test_sf_client_closeplan.py::test_fetch_closeplan_template_answers_404_is_incomplete_not_empty",
    ),
}
PIPELINE_WORKFLOW_GUIDE_NODES = (
    "tests/test_pipeline_workflow_guide_contract.py",
    "tests/test_documentation_pursuit_workflow.py::test_documentation_sync_account_scope_matches_selected_steps",
    "tests/test_documentation_pursuit_workflow.py::test_documentation_sync_forwards_account_to_in_process_people_index",
    "tests/test_sync.py::test_cli_dry_run_never_preflights_credentials",
    "tests/test_sync.py::test_selected_sync_preflight_failure_maps_to_auth_exit_two",
    "tests/test_sync.py::test_cli_json_reports_optional_inputs_that_were_not_run",
    "tests/test_sync_verbose.py::test_truncate_output_truncation_keeps_tail_not_head",
    "tests/test_sync_verbose.py::test_verbose_help_discloses_output_limit",
    "tests/test_sync_verbose.py::test_truncate_output_single_line_respects_character_cap",
    "tests/test_ingest_run_lock.py::test_busy_run_has_no_database_or_discovery_effects",
    "tests/test_ingest_prepared_store.py::test_new_source_commits_intent_before_output",
    "tests/test_ingest_prepared_store.py::test_replay_batch_needs_no_google_credentials",
    "tests/test_ingest_prepared_store.py::test_command_recovers_real_interrupted_state_only_under_lock",
    "tests/test_ingest_prepared_store.py::test_replay_failure_retains_intent",
    "tests/test_ingest_prepared_store.py::test_replay_missing_pursuit_then_repair_preserves_note_edits",
    "tests/test_ingest_prepared.py::test_prepared_task_replay_preserves_original_positions_and_edits",
    "tests/test_pursuit_effects.py::test_activity_rejects_ambiguous_marker",
    "tests/test_ingest_promote_extended.py::test_auto_task_then_promotion_skips_mine_and_continues_waiting",
    "tests/test_reprocess_journal.py::test_reprocess_command_recovers_without_credentials",
    "tests/test_reprocess_journal.py::test_reprocess_excluded_recovery_blocks_fresh_work",
    "tests/test_reprocess_journal.py::test_new_recovery_stops_before_second_fresh_artifact",
    "tests/test_reprocess_journal.py::test_reprocess_command_recovers_recorded_version_without_repreparing",
    "tests/test_reprocess_journal.py::test_reprocess_replay_refuses_changed_state",
    "tests/test_ingest_ambient.py::test_discover_cli_dry_run_hides_transcript_text_and_excludes_latest",
    "tests/test_ingest_ambient.py::test_discovery_include_latest_reads_all_stable_sessions",
    "tests/test_ingest_ambient.py::test_process_ambiguous_route_defers_and_restores_pending",
    "tests/test_ingest_ambient.py::test_run_cli_reports_noise_as_successful_skip",
    "tests/test_documentation_pursuit_workflow.py::test_documentation_audit_output_and_default_report",
    "tests/test_pursuit_audit_cmd.py::test_cli_no_results_exits_3",
    "tests/test_pursuit_audit_cmd.py::test_write_audit_report_json_flag_suppresses_report_write",
    "tests/test_pursuit_legacy_meddpicc.py::test_former_meddpicc_loads_as_typed_historical_data_without_mutating_file",
    "tests/test_pursuit_legacy_meddpicc.py::test_authorized_model_write_canonicalizes_in_place_and_preserves_content",
    "tests/test_pursuit_audit.py::test_audit_file_missing_stage",
    "tests/test_pursuit_audit.py::test_audit_file_overdue_close_date",
    "tests/test_pursuit_audit.py::test_audit_file_backstory_in_frontmatter",
    "tests/test_pursuit_audit.py::test_audit_file_backstory_in_body",
    "tests/test_pursuit_audit.py::test_audit_file_invalid_gate_result_in_history_produces_warning",
    "tests/test_pursuit_audit.py::test_apply_fixes_renames_hyphenated",
    "tests/test_pursuit_audit_cmd.py::test_cli_check_yaml_with_dups_exits_1",
    "tests/test_pursuit_audit_cmd.py::test_cli_output_path_respected",
    "tests/test_documentation_pursuit_workflow.py::test_documentation_advance_preview_is_pending_without_writes",
    "tests/test_pursuit_advance_branches.py::test_advance_cmd_override_advances_despite_failure",
    "tests/test_pursuit_advance_branches.py::test_apply_transition_appends_history_entry",
    "tests/test_forecast.py::test_documentation_forecast_example_matches_implemented_scenarios",
    "tests/test_sf_components.py::test_effective_net_consulting_acv",
    "tests/test_contract_type_acv_anchor.py::test_forecast_fixed_price_anchors_on_net_acv",
    "tests/test_pipeline_quota.py::test_documentation_local_quota_output_matches_cli",
    "tests/test_quota_period.py::test_quota_period_end_date_matches_calendar",
    "tests/test_pipeline_quota.py::test_quota_cli_source_sf_shows_territory_scoped_gap",
    "tests/test_pipeline_sf_sync_documentation.py::test_documented_listview_json_format_does_not_choose_write_authority",
    "tests/test_pipeline_sf_sync_documentation.py::test_documented_sf_provider_failures_remain_nonpassing_and_never_write",
    "tests/test_pipeline_cli.py::test_pipeline_open_account_selects_only_newest_matching_review",
    "tests/test_pipeline_cli.py::test_pipeline_open_global_ignores_newer_scoped_review",
    "tests/test_pipeline_cli.py::test_pipeline_open_missing_account_review_fails_without_fallback",
    "tests/test_pipeline_cli.py::test_pipeline_open_without_account_fails_when_only_scoped_review_exists",
    "tests/test_sf_quote.py::test_actual_cli_quote_failures_are_sanitized",
    "tests/test_sf_quote.py::test_actual_cli_successfully_empty_quote_lines",
    "tests/test_sf_quote.py::test_actual_cli_missing_quote_configuration",
    "tests/test_sf_quote.py::test_fetch_quote_lines_parses_related_list_records",
    "tests/test_sf_quote.py::test_build_payload_header_fields_mapped",
    "tests/test_sf_meddpicc.py::test_cli_passes_exact_deal_id_to_native_reader",
    "tests/test_sf_meddpicc.py::test_cli_json_reports_ambiguity_with_all_deal_ids_and_exits_partial",
    "tests/test_sf_meddpicc.py::test_cli_json_reports_incomplete_result_and_exits_partial",
    "tests/test_sf_meddpicc.py::test_cli_json_preserves_named_party_evidence_and_zero_state",
    "tests/test_sf_meddpicc.py::test_cli_json_no_closeplan_returns_empty_normalized_collections",
    "tests/test_sf_meddpicc.py::test_cli_exits_3_for_invalid_opp_id",
    "tests/test_sf_meddpicc.py::test_cli_exits_2_when_auth_error_raised",
    "tests/test_sf_meddpicc_domain.py::test_normalize_questions_distinguishes_population_states",
    "tests/test_sf_meddpicc_domain.py::test_normalize_questions_retains_unknown_and_missing_categories",
    "tests/test_sf_meddpicc_domain.py::test_normalize_question_preserves_exact_identity_raw_fields_and_concurrency",
    "tests/test_sf_meddpicc_domain.py::test_normalize_field_metadata_keeps_only_proven_generic_describe_properties",
    "tests/test_sf_meddpicc_domain.py::test_normalize_questions_keeps_duplicate_presentation_text_as_exact_records",
    "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_preserves_mode_version_and_native_maximum_consistency",
    "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_rejects_template_question_ownership_drift",
    "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_rejects_native_maximum_total_drift",
    "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_keeps_non_answers_mode_readable_but_mutation_disabled",
    "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_deduplicates_template_reads_and_preserves_exact_choices",
    "tests/test_sf_meddpicc_template_metadata.py::test_fetch_meddpicc_scorecard_propagates_incomplete_template_answer_count",
    "tests/test_sf_client_closeplan.py::test_fetch_closeplan_deals_returns_every_record_with_complete_count_evidence",
    "tests/test_sf_client_closeplan.py::test_fetch_closeplan_deals_marks_count_mismatch_incomplete",
    "tests/test_sf_client_closeplan.py::test_fetch_closeplan_deals_marks_missing_count_incomplete",
    "tests/test_sf_client_closeplan.py::test_fetch_closeplan_questions_returns_all_records_with_complete_count_evidence",
    "tests/test_sf_client_closeplan.py::test_fetch_closeplan_template_answers_reads_exact_choices_with_complete_count_evidence",
    "tests/test_sf_client_closeplan.py::test_fetch_closeplan_template_answers_404_is_incomplete_not_empty",
)


def semantic_blocks(text: str) -> tuple[str, ...]:
    """Use CommonMark source maps so output fences retain their internal blanks."""
    lines = text.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    frontmatter_end = 0
    if lines and lines[0].rstrip("\n") == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.rstrip("\n") == "---":
                frontmatter_end = index + 1
                ranges.append((0, frontmatter_end))
                break
    for token in MarkdownIt("commonmark").parse(text):
        if token.map is not None and token.level == 0 and token.nesting != -1:
            start, end = token.map
            if start >= frontmatter_end:
                ranges.append((start, end))
    covered = {index for start, end in ranges for index in range(start, end)}
    assert all(not line.strip() or index in covered for index, line in enumerate(lines)), (
        "pipeline guide semantic inventory contains unowned nonblank source"
    )
    return tuple("".join(lines[start:end]).rstrip("\n") for start, end in ranges)


def assert_guide(text: str) -> tuple[str, ...]:
    blocks = semantic_blocks(text)
    assert blocks == EXPECTED_TEXT, "pipeline guide semantic inventory changed"
    return blocks


def test_complete_ordered_pipeline_inventory() -> None:
    blocks = assert_guide(PAGE.read_text(encoding="utf-8"))

    assert len(blocks) == len(INVENTORY) == len(set(EXPECTED_IDS)) == 92
    assert sum(block.startswith("```") for block in blocks) == 16
    assert {claim.evidence for claim in INVENTORY} == set(EVIDENCE_NODES) | {
        "page-layout",
        "operator-guidance",
        "manual-context",
    }
    assert all(node in PIPELINE_WORKFLOW_GUIDE_NODES for nodes in EVIDENCE_NODES.values() for node in nodes)
    assert len(set(PIPELINE_WORKFLOW_GUIDE_NODES)) == len(PIPELINE_WORKFLOW_GUIDE_NODES)


@pytest.mark.parametrize("position", ("prepended", "appended", "interleaved"))
def test_unreviewed_reference_definitions_are_rejected(position: str) -> None:
    blocks = list(EXPECTED_TEXT)
    baseline = semantic_blocks("\n\n".join(blocks))
    assert len(baseline) == 92
    reference = "[unreviewed]: https://example.com/unreviewed"
    index = 0 if position == "prepended" else len(blocks) if position == "appended" else 30
    blocks.insert(index, reference)

    with pytest.raises(AssertionError, match="unowned nonblank source"):
        semantic_blocks("\n\n".join(blocks))


@pytest.mark.parametrize("index", range(len(INVENTORY)), ids=EXPECTED_IDS)
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "negate", "duplicate", "reorder"))
def test_every_semantic_block_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    if mutation == "insert":
        blocks.insert(index, "An unreviewed pipeline claim.")
    elif mutation == "remove":
        del blocks[index]
    elif mutation == "alter":
        blocks[index] += " Altered claim."
    elif mutation == "negate":
        blocks[index] = "It is false that " + blocks[index]
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    else:
        other = index + 1 if index + 1 < len(blocks) else index - 1
        blocks[index], blocks[other] = blocks[other], blocks[index]

    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


FENCE_INDICES = tuple(index for index, text in enumerate(EXPECTED_TEXT) if text.startswith("```"))
INLINE_VALUES = tuple(
    (index, value)
    for index, text in enumerate(EXPECTED_TEXT)
    for value in dict.fromkeys(re.findall(r"(?<!`)`([^`\n]+)`(?!`)", text))
)
LINKS = tuple(
    (index, label, target)
    for index, text in enumerate(EXPECTED_TEXT)
    for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text)
)
LIST_ITEMS = tuple(
    (index, line)
    for index, text in enumerate(EXPECTED_TEXT)
    for line in text.splitlines()
    if re.match(r"^(?:- |\d+\. )", line) and not text.startswith("```")
)


@pytest.mark.parametrize("index", FENCE_INDICES)
@pytest.mark.parametrize("mutation", ("language", "body", "closing"))
def test_every_fence_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    lines = blocks[index].splitlines()
    if mutation == "language":
        lines[0] = "```unreviewed"
    elif mutation == "body":
        lines[1] += " --unreviewed"
    else:
        lines[-1] = "unclosed fence"
    blocks[index] = "\n".join(lines)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(("index", "value"), INLINE_VALUES)
def test_every_inline_command_and_value_mutation_is_rejected(index: int, value: str) -> None:
    blocks = list(EXPECTED_TEXT)
    blocks[index] = blocks[index].replace(f"`{value}`", "`unreviewed-value`")
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(("index", "line"), LIST_ITEMS)
def test_each_list_item_negation_is_rejected(index: int, line: str) -> None:
    blocks = list(EXPECTED_TEXT)
    blocks[index] = blocks[index].replace(line, "It is false that " + line)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(("index", "label", "target"), LINKS)
@pytest.mark.parametrize("mutation", ("label", "target"))
def test_each_link_mutation_is_rejected(index: int, label: str, target: str, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    replacement = f"[unreviewed]({target})" if mutation == "label" else f"[{label}](unreviewed.md)"
    blocks[index] = blocks[index].replace(f"[{label}]({target})", replacement)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    ("index", "before", "overclaim"),
    (
        (8, "Sync plans", "Sync always runs"),
        (76, "always fetches", "fetches only after SOQL fails"),
        (84, "every recorded score is zero", "no score is positive"),
        (87, "not a mutation capability of this reader", "a mutation capability of this reader"),
    ),
)
def test_previous_overclaims_are_rejected(index: int, before: str, overclaim: str) -> None:
    blocks = list(EXPECTED_TEXT)
    assert before in blocks[index]
    blocks[index] = blocks[index].replace(before, overclaim)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


def test_sixteen_fences_and_table_keep_their_independent_owners() -> None:
    parsed = fenced_blocks(PAGE)
    assert len(parsed) == 16
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    entry = contract["documents"][PAGE.as_posix()]
    owners = tuple(record["verification_id"] for record in entry["fenced_blocks"])
    assert owners == (
        "manual.credentialed-integration",
        "manual.credentialed-integration",
        "automated.installed-base-artifact",
        "automated.pipeline-example-contract",
        "automated.installed-base-artifact",
        "automated.pipeline-example-contract",
        "automated.installed-base-artifact",
        "automated.pipeline-example-contract",
        "automated.installed-base-artifact",
        "automated.pipeline-example-contract",
        "automated.pipeline-example-contract",
        "manual.credentialed-integration",
        "manual.credentialed-integration",
        "manual.credentialed-integration",
        "manual.credentialed-integration",
        "manual.credentialed-integration",
    )
    assert tuple(record["id"] for record in entry["fenced_blocks"]) == tuple(
        f"docs.guides.pipeline.workflow.md.block-{index}" for index in range(1, 17)
    )
    assert [(record["id"], record["verification_id"]) for record in entry["tables"]] == [
        ("docs.guides.pipeline.workflow.md.table-1", "automated.pipeline-example-contract"),
    ]
    assert {scenario.identifier for scenario in MANUAL_SCENARIOS if scenario.document == PAGE.as_posix()} == {
        f"docs.guides.pipeline.workflow.md.block-{index}" for index in (1, 2, 12, 13, 14, 15, 16)
    }
    for _index, _label, target in LINKS:
        if not target.startswith("https://"):
            assert (PAGE.parent / target).is_file()


def test_canonical_owner_executes_fixed_manifest() -> None:
    commands = DOCUMENT_COMMANDS.get(OWNER)
    assert commands is not None, "pipeline whole-page owner is not registered"
    assert commands == (("uv", "run", "pytest", *PIPELINE_WORKFLOW_GUIDE_NODES, "-q", "-n", "0"),)


def forbidden_boundary(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("offline pipeline guide reached a provider, credential, process, or viewer boundary")


@pytest.mark.parametrize(
    ("command", "options"),
    (
        (("sync",), ("--quick", "--sf", "--slack", "--dry-run", "--account", "--verbose")),
        (("pursuit", "audit"), ("--fix", "--account", "--json", "--check-yaml", "--output")),
        (("pursuit", "advance"), ("--to", "--override", "--dry-run", "--account", "--name")),
        (("pursuit", "forecast"), ("--account", "--quota", "--json")),
        (("pipeline", "quota"), ("--source", "--json")),
        (("sf", "listview"), ("--dry-run", "--json")),
        (("pipeline", "open"), ("--account", "--json")),
        (("sf", "quote"), ("--json",)),
        (("sf", "meddpicc"), ("--deal-id", "--json")),
    ),
)
def test_real_documented_options_are_available_without_provider_access(
    documented_workspace: Path, command: tuple[str, ...], options: tuple[str, ...]
) -> None:
    before = snapshot_workflow(documented_workspace)

    result = invoke_workflow([*command, "--help"])

    assert result.exit_code == 0, result.output
    assert all(option in result.stdout for option in options)
    assert snapshot_workflow(documented_workspace) == before


def test_documented_stage_sequence_matches_the_canonical_domain() -> None:
    sequence = EXPECTED_TEXT[43].split("`", 2)[1]

    assert sequence.split(" → ") == [*PIPELINE_STAGES, "closed-won"]


def write_pursuit(workspace: Path, account: str = "acme-corp", **fields: object) -> Path:
    path = workspace / "accounts" / account / "pursuits" / "guide-deal.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "stage": "discover",
        "gate-status": "pending",
        "last-transition": None,
        "transition-history": [],
        **fields,
    }
    path.write_text("---\n" + yaml.safe_dump(frontmatter) + "---\n# Fictional pursuit\n", encoding="utf-8")
    return path


def test_real_sync_preview_never_preflights_or_executes(
    documented_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fieldkit.watch.preflight.preflight_check", forbidden_boundary)
    monkeypatch.setattr("fieldkit.commands.datasync.cli.run_bounded_process", forbidden_boundary)
    before = snapshot_workflow(documented_workspace)

    result = invoke_workflow(["sync", "--dry-run", "--account", "acme-corp", "--json"])

    assert result.exit_code == 0, result.output
    assert "ingest discover" in result.stdout
    assert "ingest run" in result.stdout
    assert "pursuit-stalls" in result.stdout
    assert snapshot_workflow(documented_workspace) == before


def test_real_audit_json_has_unavailable_qualification_and_no_writes(documented_workspace: Path) -> None:
    write_pursuit(documented_workspace)
    before = snapshot_workflow(documented_workspace)

    result = invoke_workflow(["pursuit", "audit", "--json"])

    assert result.exit_code == 0, result.output
    assert "unavailable" in result.stdout
    assert snapshot_workflow(documented_workspace) == before
    assert not (documented_workspace / "accounts" / ".audit").exists()


def test_real_advance_pending_then_explicit_override_preserves_historical_data(documented_workspace: Path) -> None:
    path = write_pursuit(documented_workspace, meddpicc={"metrics": 2}, sf_stage="Discover")
    before = snapshot_workflow(documented_workspace)

    preview = invoke_workflow(["pursuit", "advance", "acme-corp/guide-deal", "--dry-run"])

    assert preview.exit_code == 1, preview.output
    assert "PENDING" in preview.stdout
    assert snapshot_workflow(documented_workspace) == before
    applied = invoke_workflow(
        ["pursuit", "advance", "acme-corp/guide-deal", "--override", "Reviewed fictional exception"]
    )
    assert applied.exit_code == 0, applied.output
    loaded = load_pursuit(path)
    assert loaded[0].stage == "validate"
    assert loaded[0].sf_stage == "Discover"
    raw = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert raw["last-transition"] == datetime.now(tz=UTC).date().isoformat()
    assert raw["transition-history"][-1]["override-reason"] == "Reviewed fictional exception"
    assert raw["legacy_meddpicc"]["metrics"] == 2
    assert "meddpicc" not in raw


@pytest.mark.parametrize(
    ("contract", "gross", "net", "arr", "expected"),
    (
        ("standard", 200, 100, 90, 200),
        ("unknown", 200, 100, 90, 200),
        ("standard", None, 100, 90, 100),
        ("standard", None, None, 90, 90),
        ("standard", 0, 100, 90, 0),
        ("fixed_price", 200, 100, 90, 100),
        ("fixed_price", 200, 0, 90, 0),
        ("fixed_price", 200, None, 90, 200),
    ),
)
def test_real_forecast_scopes_account_preserves_acv_precedence_and_overrides_quota(
    documented_workspace: Path,
    contract: str,
    gross: int | None,
    net: int | None,
    arr: int | None,
    expected: int,
) -> None:
    write_pursuit(
        documented_workspace,
        stage="propose",
        sf_contract_type=contract,
        sf_consulting_acv=gross,
        sf_acv=net,
        sf_arr=arr,
    )
    write_pursuit(documented_workspace, "other-corp", stage="negotiate", sf_acv=900)
    before = snapshot_workflow(documented_workspace)

    result = invoke_workflow(["pursuit", "forecast", "--account", "acme-corp", "--quota", "1000", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload["deals"]) == 1
    assert payload["deals"][0]["acv"] == expected
    assert payload["weighted"] == expected * 0.5
    assert payload["best_case"] == expected
    assert payload["commit"] == payload["closed_won"] == 0
    assert payload["quota"] == 1000
    assert snapshot_workflow(documented_workspace) == before


@pytest.mark.parametrize("account", (None, "acme-corp"))
def test_real_pipeline_generation_and_open_use_the_same_scope_without_a_viewer(
    documented_workspace: Path, monkeypatch: pytest.MonkeyPatch, account: str | None
) -> None:
    write_pursuit(documented_workspace, sf_acv=100)
    monkeypatch.setattr("fieldkit.pipeline.main.synthesize", forbidden_boundary)
    monkeypatch.setattr("webbrowser.open", forbidden_boundary)
    args = [] if account is None else ["--account", account]

    generated = invoke_workflow(["pipeline", "--no-llm", *args])

    assert generated.exit_code == 0, generated.output
    component = "" if account is None else account + "-"
    date = datetime.now(tz=UTC).date().isoformat()
    report = documented_workspace / "briefs" / f"pipeline-review-{component}{date}.md"
    assert report.read_text(encoding="utf-8").startswith("# Pipeline Review")
    before = snapshot_workflow(documented_workspace)
    opened = invoke_workflow(["pipeline", "open", *args, "--no-open", "--json"])
    assert opened.exit_code == 0, opened.output
    payload = json.loads(opened.stdout)
    assert payload["path"] == str(report)
    assert payload["account"] == account
    assert payload["opened"] is False
    assert snapshot_workflow(documented_workspace) == before
    missing_args = ["--account", "acme-corp"] if account is None else []
    missing = invoke_workflow(["pipeline", "open", *missing_args, "--no-open", "--json"])
    assert missing.exit_code == 3, missing.output
    assert "fieldkit pipeline" in missing.output
    assert snapshot_workflow(documented_workspace) == before


def test_native_zero_and_negative_scores_are_not_zero_population() -> None:
    questions = [
        normalize_question(
            {
                "fields": {
                    "Id": {"value": f"a1E00000000000{index}AAA"},
                    "Name": {"value": "METRICS - Fictional metric"},
                    "TSPC__Score__c": {"value": score},
                },
            },
            field_metadata={},
        )
        for index, score in enumerate((0, -1), 1)
    ]

    result = normalize_questions(questions)

    assert result.elements[0]["state"] == "scored"
    assert result.elements[0]["complete"] is True
    assert "metrics" not in result.gaps
    assert [question["score"] for question in result.elements[0]["questions"]] == [0, -1]


@pytest.mark.parametrize("character", ("é", "😀"))
def test_verbose_bound_is_unicode_characters_not_bytes(character: str) -> None:
    result = _truncate_output(character * (MAX_VERBOSE_CHARS * 2))

    assert result.startswith("[... truncated")
    assert len(result) == MAX_VERBOSE_CHARS == 16_384
    assert result.endswith(character * 100)
    assert len(result.encode("utf-8")) > MAX_VERBOSE_CHARS
