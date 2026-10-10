## Context

`make quality` is the bounded pull-request gate. Its last stage,
`impact-pytest`, runs the Tach-selected tests without coverage. `make
quality-full` runs the whole suite once with coverage, writes `coverage.json`,
and feeds it to three gazepy stages. `ci-cost-controls` deliberately keeps
complete enforcement scheduled rather than per-PR, so the fix must stay
bounded.

gazepy's `crap` command scans a file or directory and records each target's
`location` relative to the scanned path (for example
`contact/_enrich_helpers.py:696` when scanning `src/fieldkit/`). Scanning a
single file would therefore produce keys that never match the baseline.

## Goals / Non-Goals

### Goals
- Catch CRAP regressions in the pull request that introduces them.
- Stay within the bounded PR budget.
- Restore `main` to a passing Full enforcement run before enabling the stage.

### Non-Goals
- Changing thresholds, the ceiling, contract-coverage minimums, or the
  committed baseline policy.
- Running the complete suite on pull requests.
- Gating GazeCRAP (`--max-gaze-crapload`), which this repository has never
  enforced.

## Decisions

1. **Scan the whole tree, filter the report.** The stage runs
   `gazepy crap src/fieldkit/ --format json --coverprofile <impact coverage>
   --baseline .gaze/baseline.json` and passes the report to a new
   `scripts/gaze_changed.py`. The script keeps regressions whose `package`
   path maps to a changed `src/fieldkit/` file from `git diff --name-only
   QUALITY_BASE...HEAD` and exits 1 if any remain. Location keys therefore
   match the baseline exactly. Task 2.1 confirms that gazepy's JSON output
   carries the baseline comparison. If it doesn't, the script performs the
   comparison itself, keyed by `(package, receiver, function)`, using the same
   CRAP-delta rule gazepy applies.
2. **Coverage comes from the impact run.** `impact-pytest` gains
   `--cov --cov-report=json:coverage-impact.json`. Tach selects tests that
   import changed modules, so functions in changed files are covered by the
   tests most likely to exercise them. Functions outside changed files may
   show inflated CRAP from partial coverage; the filter discards them, which
   is why the filter is required rather than optional.
3. **No bypass flag.** A legitimate complexity increase is recorded by
   updating `.gaze/baseline.json` for that function in the same pull request,
   which is visible in review. This preserves the rule that gates are not
   weakened to make a change pass.
4. **Fix main first.** `run_enrichment_pipeline` regains its baseline score
   by moving the five-clause resume condition into a typed helper,
   `_checkpoint_resume_index(checkpoint, account, fingerprint, total) -> int`.
   It returns `0` for a missing, legacy, or incompatible checkpoint and the
   validated `total_processed` otherwise. The warning about an incompatible
   checkpoint stays in the pipeline so logging behavior is unchanged.

## Risks / Trade-offs

- **Partial-coverage false positive.** A changed function covered only by
  tests that Tach doesn't select would show an inflated score. Mitigation: the
  failure output names the function, and the remedy (a direct unit test) is
  the behavior the gate exists to encourage.
- **Baseline drift.** The 2026-10-09 run reported 461 new and 169 removed
  targets relative to the baseline. New functions aren't regressions, so the
  stage ignores them; the ceiling continues to bound them in full enforcement.
  Regenerating the baseline is out of scope and must only ever be done from a
  green `main`.
- **Added PR time.** Coverage instrumentation and one AST pass add roughly 30
  to 60 seconds to the bounded gate.
