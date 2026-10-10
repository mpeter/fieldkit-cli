## Why

The Gaze CRAP baseline gate runs only in `make quality-full`, which the
scheduled Full enforcement workflow executes once a day. A pull request can
pass every required check, merge, and turn `main` red the next morning. This
has happened three times in a week:

- #47: `_build_steps` and `add_note` regressed and were repaired in #62.
- #69: watch regressions from #52 were repaired after the fact.
- The 2026-10-09 scheduled run on `6a82182` failed `gazepy-baseline` with one
  regression: `contact/_enrich_helpers.py:run_enrichment_pipeline`, CRAP
  +7.01 and GazeCRAP +7.00, introduced by #90.

Each occurrence costs a follow-up repair PR. While `main` is red, a new
regression doesn't show up as a new failure, and a release cannot be cut.

## What Changes

1. Restore the baseline on `main` by decomposing the checkpoint-resume decision
   that #90 added inline to `run_enrichment_pipeline`, without changing its
   behavior.
2. Add a `CRAP (changed functions)` child to the pull-request `Required checks`
   rollup. It runs in parallel with the other children, only when production
   Python changes, measures coverage over the whole suite with Python 3.13's
   low-overhead `sys.monitoring` core, and applies the baseline gate's failure
   rules to functions in `src/fieldkit/` files the pull request changed.
   `make pr-check` is unchanged; `make crap-changed` runs the same check locally.
3. Keep the complete `gazepy-baseline`, `gazepy-ceiling`, and
   `gazepy-contract-coverage` stages in `make quality-full` unchanged. The new
   stage adds coverage earlier; it does not replace or relax the full gate.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `ci-cost-controls`: pull-request CI gains a parallel, changed-file CRAP
  check.

### Removed Capabilities
- None.

## Impact

- `.github/workflows/ci.yml` (new job and rollup child), `Makefile`
  (optional `crap-changed` target), `scripts/gaze_changed.py`,
  `tests/test_quality_contract.py`, and contributor documentation.
- `src/fieldkit/contact/_enrich_helpers.py` (behavior-preserving refactor).
- Cost: none on this public repository's standard runners. Wall time: the job
  runs in parallel and is expected to finish within the existing slowest child
  (`Lint (ruff)`, about 100 to 130 seconds); local `make pr-check` is unchanged.
- This is a quality-policy change. The repository contract requires explicit
  maintainer review before merge.

## Constitution Alignment

Assessed against the Unbound Force org constitution.

### I. Autonomous Collaboration

**Assessment**: PASS

The stage communicates through artifacts that already exist: the committed
baseline, a coverage JSON report, and the quality-stage evidence summary. It
introduces no runtime coupling between workflows.

### II. Composability First

**Assessment**: N/A

Development tooling only. No runtime dependency or installation profile
changes.

### III. Observable Quality

**Assessment**: PASS

Regressions are reported per function with location and CRAP delta in the
existing quality-stage output, and the stage result appears as a named child of
`Required checks`.

### IV. Testability

**Assessment**: PASS

The changed-function filter is a pure function over a gazepy JSON report and a
changed-path list, testable with synthetic reports and no repository state.
