## ADDED Requirements

### Requirement: Pull-request quality rejects CRAP regressions in changed code

The bounded pull-request quality gate MUST compare CRAP scores for functions
defined in `src/fieldkit/` files changed relative to the quality base against
the committed Gaze baseline, and MUST fail when any such function regresses.
It MUST NOT fail for regressions in unchanged files, which remain the
responsibility of complete scheduled enforcement. Complete enforcement MUST
continue to run the whole-tree baseline, ceiling, and contract-coverage gates
unchanged.

#### Scenario: A pull request raises complexity of a changed function
- **GIVEN** a pull request that adds untested branches to a function in a
  changed `src/fieldkit/` file
- **WHEN** `make pr-check` runs with that pull request's quality base
- **THEN** the `gazepy-changed` stage SHALL fail
- **AND** its output SHALL name the function, its location, and its CRAP delta

#### Scenario: A pull request changes only documentation
- **GIVEN** a pull request whose changed files are all documentation
- **WHEN** the bounded quality gate runs
- **THEN** the `gazepy-changed` stage SHALL be skipped with the same
  docs-only rule as the impact pytest stage

#### Scenario: An unchanged function already regressed on the base
- **GIVEN** a base revision where an unchanged function exceeds its baseline
- **WHEN** a pull request that does not touch that function's file runs the
  bounded gate
- **THEN** the `gazepy-changed` stage SHALL pass
- **AND** scheduled complete enforcement SHALL still report the regression

#### Scenario: A pull request improves a function
- **GIVEN** a pull request that lowers a changed function's CRAP score
- **WHEN** the bounded gate runs
- **THEN** the `gazepy-changed` stage SHALL pass without requiring a baseline
  update
