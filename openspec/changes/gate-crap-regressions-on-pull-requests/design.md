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
2. **Coverage comes from a parallel full-suite run on Python 3.13.** The
   original plan instrumented the local impact run, but measurement showed
   3.11's coverage tracer adds about 75% to suite time, which would slow
   `make pr-check` and, in CI, make the coverage job the slowest child. Python
   3.13's `sys.monitoring` core adds about 25%. A 3.13 full-suite coverage run
   reported zero baseline regressions on `main` and matched 3.11 line coverage
   in 811 of 812 files, so it is a faithful input to the 3.11-recorded
   baseline. The whole suite removes the partial-coverage false positives an
   impact selection would cause. Test failures are gated by `Test (pytest)`;
   this job evaluates coverage even when a test fails.
2a. **Both baseline failure rules apply.** gazepy reports functions that moved
   or were renamed since the baseline as new; it fails those only at the
   new-function threshold (15). The filter applies the same rule to new
   functions in changed files, matching `gazepy-baseline`.
2b. **CRAP only; skip test analysis.** gazepy's default run analyzes the test
   suite for contract coverage, which feeds GazeCRAP and took about 58 s in CI.
   The filter passes an empty `--tests` directory: CRAP scores were identical
   for all 2160 functions, and the scan drops to about 10 s. A change that
   worsens only GazeCRAP (lower contract coverage with unchanged line coverage
   and complexity) is therefore caught by the scheduled complete run, not the
   pull request.
3. **No bypass; the baseline is a one-way ratchet.** The check starts from
   `.gaze/baseline.json` as committed at the merge base and takes the change's
   own edits to it only where they add an entry or lower a score. A raised
   score, a removed entry for a function that still exists, or an added entry
   above the new-function threshold fails the check, because that file becomes
   the baseline scheduled enforcement reads once the change merges, and an
   added entry would otherwise exempt its function from the threshold. Additions let a refactor re-track moved or renamed functions
   without a scheduled regeneration job; lowered scores lock in gains. A
   deliberate increase is a maintainer-reviewed pull request that changes only
   the baseline, which this job does not run on, merged first. Same-named
   functions that gazepy keys alike are compared by their highest score.
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
- **Added PR time.** The job runs in parallel; its expected duration is close
  to the existing slowest child. Local `make pr-check` is unchanged.
- **Python version skew.** Coverage is measured on 3.13 against a baseline
  recorded on 3.11. One file differed (slightly higher on 3.13). Two tests fail
  on 3.13 (symlink-loop handling, #119); they do not affect this
  job's result.
