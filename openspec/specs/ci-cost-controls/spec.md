# ci-cost-controls Specification

## Purpose
Define the current behavioral contract for ci-cost-controls, including required behavior, failure modes, and observable outcomes.

## Requirements

### Requirement: Pull-request CI is bounded and release compatibility is explicit

Ordinary pull requests MUST report the stable `Required checks` context over
the bounded quality children. Installed-artifact compatibility MUST be invoked
separately for a release candidate and MUST NOT be represented as a passed or
skipped ordinary PR child.

#### Scenario: Code pull request passes bounded validation
- **GIVEN** a code pull request whose bounded quality children succeed
- **WHEN** `Required checks` evaluates its required children
- **THEN** it succeeds without starting an artifact compatibility matrix
- **AND** a release owner can dispatch artifact compatibility for the exact
  candidate revision when release evidence is needed

### Requirement: Complete and public-only controls use proportionate triggers

Complete quality enforcement MUST remain scheduled and manually dispatchable.
Public-only analysis MUST not allocate a hosted runner while repository
visibility makes the control inapplicable.

#### Scenario: A main push occurs in the private repository
- **GIVEN** the repository is private
- **WHEN** a commit reaches the default branch
- **THEN** full enforcement does not start solely because of that push
- **AND** public-only analysis does not run a placeholder hosted job

### Requirement: Pull-request CI rejects CRAP failures in changed code

Pull-request CI MUST run a `CRAP (changed functions)` child of `Required checks`
whenever production Python under `src/fieldkit/` or `.gaze/baseline.json`
changes. It MUST apply the
base revision's committed Gaze baseline failure rules to functions in changed files: a
tracked function whose CRAP rose, and an untracked function whose CRAP exceeds
the new-function threshold. It MUST NOT fail for functions in unchanged files,
which remain the responsibility of complete scheduled enforcement. It MUST run
in parallel with the other children, MUST NOT add a stage to `make pr-check`,
and complete enforcement MUST continue to run the whole-tree baseline, ceiling,
and contract-coverage gates unchanged.

#### Scenario: A pull request raises complexity of a changed function
- **GIVEN** a pull request that adds untested branches to a tracked function in
  a changed `src/fieldkit/` file
- **WHEN** pull-request CI runs
- **THEN** `CRAP (changed functions)` SHALL fail
- **AND** its output SHALL name the function, its location, and its CRAP change

#### Scenario: A pull request adds a complex untested function
- **GIVEN** a pull request that adds a function whose CRAP exceeds the
  new-function threshold
- **WHEN** pull-request CI runs
- **THEN** `CRAP (changed functions)` SHALL fail and name the function

#### Scenario: A pull request changes neither production Python nor the baseline
- **GIVEN** a pull request that changes only documentation, tests, scripts, or
  configuration
- **WHEN** pull-request CI runs
- **THEN** `CRAP (changed functions)` SHALL succeed without installing
  dependencies or running tests

#### Scenario: An unchanged function already regressed on the base
- **GIVEN** a base revision where an unchanged function exceeds its baseline
- **WHEN** a pull request that does not touch that function's file runs CI
- **THEN** `CRAP (changed functions)` SHALL pass
- **AND** scheduled complete enforcement SHALL still report the regression

#### Scenario: A pull request improves a function
- **GIVEN** a pull request that lowers a changed function's CRAP score
- **WHEN** pull-request CI runs
- **THEN** `CRAP (changed functions)` SHALL pass without requiring a baseline
  update

#### Scenario: A pull request re-tracks moved functions
- **GIVEN** a pull request whose `.gaze/baseline.json` adds entries or lowers
  scores
- **WHEN** pull-request CI runs
- **THEN** `CRAP (changed functions)` SHALL compare against those entries

#### Scenario: A pull request loosens the baseline
- **GIVEN** a pull request whose `.gaze/baseline.json` raises a score,
  removes the entry of a function that still exists, or adds an entry above
  the new-function threshold
- **WHEN** pull-request CI runs
- **THEN** `CRAP (changed functions)` SHALL fail, name the entry, and keep
  comparing against the base revision's score
