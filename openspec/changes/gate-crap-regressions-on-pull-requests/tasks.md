## 1. Restore the baseline on main

- [x] 1.1 Extract `_checkpoint_resume_index()` from `run_enrichment_pipeline` in `src/fieldkit/contact/_enrich_helpers.py`, preserving the resume and warning behavior added by #90
- [x] 1.2 Add parameterized unit tests for `_checkpoint_resume_index()` covering: no checkpoint, legacy version, missing `account_scope`, scope mismatch, fingerprint mismatch, offset out of range, and a valid resume; assert on the direct return value
- [x] 1.3 Run `make quality-full` locally (or dispatch Full enforcement) and confirm `gazepy-baseline` reports zero regressions; land this group as its own PR before group 2

## 2. Changed-function CRAP stage

- [x] 2.1 Confirm whether `gazepy crap --format json --baseline` emits per-target regression records, and record the exact JSON shape in the script's module docstring
- [x] 2.2 Add `scripts/gaze_changed.py`: read the gazepy JSON report and the changed-path list, keep regressions in changed `src/fieldkit/` files, print each as `function location CRAP-delta`, exit 1 if any remain, and give every subprocess a named timeout
- [x] 2.3 [P] Add unit tests for `scripts/gaze_changed.py` using synthetic reports: regression in a changed file fails, regression in an unchanged file passes, improvement passes, new function passes, empty change list passes
- [x] 2.4 Add the `CRAP (changed functions)` CI job (Python 3.13, `COVERAGE_CORE=sysmon`, gated on a `src` output of `Detect code changes`), add it to the `Required checks` rollup, and add an optional `make crap-changed` target
- [x] 2.5 Update `tests/test_quality_contract.py` for the new stage and ensure the `Required checks` rollup includes it
- [x] 2.6 [P] Add `coverage-changed.json` to `.gitignore`

## 3. Documentation and verification

- [x] 3.1 [P] Document the stage and the baseline-update procedure in the contributor guide next to `make pr-check`
- [x] 3.2 CI-only change: use the `skip-changelog` label instead of a fragment
- [x] 3.3 Prove the gate on a throwaway branch: reintroduce an untested branch in a changed function and confirm `make pr-check` fails naming it, then confirm a docs-only change skips the stage
- [x] 3.4 Verify constitution alignment: stage output is machine-parseable and named in the evidence summary (III); the filter is tested in isolation (IV)
- [ ] 3.5 Request explicit maintainer review, as required for quality-policy changes
