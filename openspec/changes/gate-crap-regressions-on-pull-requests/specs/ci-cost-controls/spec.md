## ADDED Requirements

### Requirement: Pull-request CI rejects CRAP failures in changed code

Pull-request CI MUST run a `CRAP (changed functions)` child of `Required checks`
whenever production Python under `src/fieldkit/` changes. It MUST apply the
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

#### Scenario: A pull request changes no production Python
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
