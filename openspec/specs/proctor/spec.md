# proctor Specification

## Purpose
Define the current behavioral contract for proctor, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Phase 0 pre-mortem hypothesis generation

The proctor MUST generate failure hypotheses from the PR description and diffstat
BEFORE reading implementation code. Hypotheses MUST be drawn from the 24-category failure
catalogue (A1–A10 code fragility, B1–B7 silent failure, C1–C7 system gaps). Each
hypothesis MUST include a category reference and a one-sentence failure prediction.

#### Scenario: Pre-mortem on a feature PR adding a nightly health sensor

- **GIVEN** a PR description stating "adds nightly health sensor with systemd units"
- **WHEN** Phase 0 runs against the description and diffstat
- **THEN** at least one C1 hypothesis is generated ("no timer/scheduler invokes this")
  AND at least one B5 hypothesis is generated ("retry exhaustion without notification")
  AND hypotheses are output as structured JSON conforming to HYPOTHESES_SCHEMA
  AND no implementation files have been read

#### Scenario: Pre-mortem on a docs-only PR

- **GIVEN** a PR whose diffstat contains only `.md` files
- **WHEN** Phase 0 runs
- **THEN** hypotheses are limited to A-categories (code fragility is N/A for docs)
  AND B and C categories produce zero hypotheses
  AND output still conforms to HYPOTHESES_SCHEMA

### Requirement: Risk-based triage and model routing

Phase 1 MUST read the diff, classify risk surfaces, select review lenses, and assign a
model tier per lens. Lenses with zero signal for the change type MUST be skipped. Skipped
lenses MUST be logged with a reason (no-silent-caps). The triage output MUST conform to
TRIAGE_SCHEMA.

#### Scenario: Auth-sensitive change routes security lens to frontier tier

- **GIVEN** a diff that modifies files matching `**/auth/**` or `**/session/**`
- **WHEN** Phase 1 triages the diff
- **THEN** the security lens is selected with tier `fable` or `opus`
  AND the triage output includes `riskSurfaces` containing `"auth"`

#### Scenario: Test-only change skips irrelevant lenses

- **GIVEN** a diff that modifies only `tests/**` files
- **WHEN** Phase 1 triages the diff
- **THEN** the `security` and `operations` lenses are in `skippedLenses`
  AND the `test-quality` and `correctness` lenses are selected
  AND each skipped lens has a non-empty `reason`

### Requirement: Single-context review with directed hunt modes

Phase 2 MUST read affected files exactly once and apply all selected lenses in that single
context. For lenses with assigned hypotheses, Phase 2 MUST run directed hunt mode: the
review prompt MUST include each hypothesis as a specific search target. For lenses assigned
B-hypotheses or encountering error handling code, Phase 2 MUST apply the silent-failure
scrutiny checklist (catch specificity, fallback justification, propagation check, log
quality, user impact).

#### Scenario: Directed hunt for a C1 hypothesis

- **GIVEN** Phase 0 generated hypothesis H3 (category C1: "no scheduler invokes this")
- **AND** Phase 1 mapped H3 to the operations lens
- **WHEN** Phase 2 runs the operations lens
- **THEN** the review prompt includes "specifically, verify whether a timer, cron, or
  scheduler exists that invokes this code path"
  AND findings referencing H3 carry `hypothesis: "H3"` in the output

#### Scenario: B-directed hunt on error handlers

- **GIVEN** the diff contains a `try/except` block in `src/fieldkit/health/runner.py`
- **WHEN** Phase 2 runs the correctness lens
- **THEN** the review evaluates the handler against all 5 scrutiny questions
  AND any finding from this evaluation carries `class: "SILENT_FAILURE"`

### Requirement: Conditional adversarial verification

Phase 3 MUST fire if Phase 2 produced at least one CRITICAL or HIGH finding, or if any
Phase 0 hypothesis was neither confirmed nor refuted by Phase 2. Each finding or
unresolved hypothesis MUST be verified by an independent agent prompted to refute it.
Phase 3 MUST NOT fire if all findings are MEDIUM or lower and all hypotheses are resolved.

#### Scenario: CRITICAL finding triggers verify

- **GIVEN** Phase 2 produced one CRITICAL finding (F1) and two LOW findings (F2, F3)
- **WHEN** Phase 3 evaluates whether to fire
- **THEN** Phase 3 fires and verifies F1
  AND F2 and F3 are passed through to synthesis without verification

#### Scenario: Unresolved hypothesis escalates to verify

- **GIVEN** Phase 0 generated hypothesis H5 ("no timer invokes health run")
- **AND** Phase 2 could neither confirm nor refute H5
- **WHEN** Phase 3 runs
- **THEN** H5 is verified by an agent prompted to "try to construct a triggering scenario"
  AND the verdict is one of CONFIRMED, REFUTED, or UNRESOLVABLE

#### Scenario: No high-severity findings, all hypotheses resolved

- **GIVEN** Phase 2 produced only MEDIUM and LOW findings
- **AND** all Phase 0 hypotheses were confirmed or refuted in Phase 2
- **WHEN** Phase 3 evaluates whether to fire
- **THEN** Phase 3 does NOT fire
  AND all findings pass through to synthesis as-is

### Requirement: P45 failure-scenario enforcement

Every finding in the synthesis output MUST carry a `failureScenario` field describing the
concrete input/state that produces the wrong outcome. A finding without a failure scenario
MUST be demoted to `verdict: "ADVISORY"` regardless of its assigned severity.

#### Scenario: Finding without failure scenario demoted

- **GIVEN** Phase 2 produced a HIGH finding with `failureScenario: null`
- **WHEN** Phase 4 synthesizes the report
- **THEN** the finding's verdict is `"ADVISORY"` in the output
  AND its severity is preserved (still HIGH) for informational purposes

### Requirement: Structured synthesis output

Phase 4 MUST produce output conforming to REPORT_SCHEMA. The output MUST include:
triageSummary (lenses run and skipped), findings array (deduplicated, ranked by severity),
and unresolvedHypotheses array. Findings MUST be deduplicated: a hypothesis-confirm that
matches a standard finding by file and line MUST merge into one entry.

#### Scenario: Hypothesis-confirm deduplicates with standard finding

- **GIVEN** Phase 2 produced finding F1 at `runner.py:47` from standard mode
- **AND** Phase 2 produced finding F2 at `runner.py:47` from directed C-mode for H3
- **WHEN** Phase 4 synthesizes
- **THEN** F1 and F2 merge into one finding with `hypothesis: "H3"`
  AND the merged finding takes the higher severity of the two

### Requirement: Catalogue extensibility

The failure catalogue MUST be a standalone file (`catalogue.md`) read by the skill at
runtime. Adding a new category to the catalogue MUST NOT require modifying any prompt
template or skill. The catalogue MUST be versioned and the version MUST appear
in the synthesis output's `catalogueVersion` field.

#### Scenario: New category added to catalogue

- **GIVEN** a new category C8 is added to `catalogue.md`
- **WHEN** the proctor runs on the next PR
- **THEN** Phase 0 MAY generate hypotheses tagged C8
  AND no other file in the skill directory was modified

### Requirement: Review entry point consolidation

The proctor MUST be invocable as `/proctor` with optional mode hints (`quick`,
`pre-mortem`, `--lens`, `--tier`, or a PR number). Deprecated review entry
points MUST direct users to the proctor workflow.

#### Scenario: A user invokes a deprecated review entry point
- **GIVEN** a user invokes a retired review command
- **WHEN** the command displays its guidance
- **THEN** it directs the user to `/proctor`

### Requirement: Risk-proportional review context

The proctor MUST execute selected review lenses in one context-build, except
when its documented model-tier split requires at most two contexts.

#### Scenario: A feature review selects multiple lenses
- **GIVEN** triage selects multiple review lenses
- **WHEN** the review phase executes
- **THEN** the selected lenses share one context-build unless the documented
  model-tier split applies
