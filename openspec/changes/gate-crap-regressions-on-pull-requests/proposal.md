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
2. Add a bounded `gazepy-changed` stage to `make quality` (and therefore
   `make pr-check` and the `Required checks` pull-request rollup). It reports a
   CRAP regression against `.gaze/baseline.json` only for functions defined in
   `src/fieldkit/` files changed relative to `QUALITY_BASE`, using coverage
   from the existing impact-selected pytest run.
3. Keep the complete `gazepy-baseline`, `gazepy-ceiling`, and
   `gazepy-contract-coverage` stages in `make quality-full` unchanged. The new
   stage adds coverage earlier; it does not replace or relax the full gate.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `ci-cost-controls`: pull-request quality gains a bounded, changed-file CRAP
  regression stage.

### Removed Capabilities
- None.

## Impact

- `Makefile` (`quality` target), `scripts/` (new changed-function filter),
  `tests/test_quality_contract.py`, contributor documentation for
  `make pr-check`.
- `src/fieldkit/contact/_enrich_helpers.py` (behavior-preserving refactor).
- Pull-request wall time grows by one coverage-instrumented impact run and one
  gazepy AST pass, roughly 30 to 60 seconds.
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
