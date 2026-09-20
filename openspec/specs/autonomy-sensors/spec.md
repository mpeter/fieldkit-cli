# autonomy-sensors Specification

## Purpose
Define the current behavioral contract for autonomy-sensors, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: A scheduled health run senses regressions against the frozen gates

The system MUST provide a scheduled job that runs the quality gate bundle
against `origin/main` in a detached ephemeral worktree.  Every gate subprocess
MUST receive a home rooted inside that worktree and MUST NOT inherit
`FIELDKIT_DATA_DIR`, rather than the operator's configured workspace, so the
observed result is reproducible without user configuration and retains the
normal config-derived runtime-data contract.  The run MUST source the
Gazepy CRAP ceiling from the target worktree's Makefile and MUST NOT modify any
gate threshold, coverage floor, Gazepy baseline, invariant test, or severity
definition.  It MUST retain separate baseline-comparison and baseline-free
ceiling invocations.

#### Scenario: a configured operator host senses the same test precondition as CI
- **GIVEN** the operator has a real `~/.config/fieldkit/config.yaml` and
  workspace data
- **WHEN** the nightly health run executes against `origin/main`
- **THEN** its pytest subprocess sees an isolated home with no operator config
- **AND** pytest creates its normal minimal CI-style configuration there
- **AND** no `FIELDKIT_DATA_DIR` override changes the suite's config behavior
- **AND** the operator's config and runtime data remain unchanged

#### Scenario: direct local quality run has the CI configuration precondition
- **WHEN** a developer runs the pytest suite with a configured fieldkit home
- **THEN** the suite replaces the controller and worker configuration with a
  temporary minimal config before test execution
- **AND** it does not read, modify, or remove the operator's config or runtime data

#### Scenario: the target Makefile changes the ratified ceiling
- **GIVEN** the target worktree Makefile contains an unambiguous
  baseline-free Gazepy `--max-crapload` value
- **WHEN** the nightly health run invokes the Gazepy ceiling check
- **THEN** it passes that Makefile value to Gazepy
- **AND** it does not use an independently maintained health-sensor literal

#### Scenario: the target Makefile cannot define a safe ceiling invocation
- **GIVEN** the target worktree Makefile has no unambiguous numeric
  baseline-free Gazepy `--max-crapload` value
- **WHEN** the nightly health run executes
- **THEN** the Gazepy ceiling check is recorded as an execution error
- **AND** the overall run is `partial`, not falsely green and not a guessed threshold

### Requirement: New regressions are filed as deduplicated issues; green runs are silent

Each *new* regression MUST be filed as one GitHub issue through the
`fieldkit issue raise` code path, with the failing check's output in the body.
A regression that already has a matching open issue MUST NOT be re-filed. A fully
green run MUST file nothing and emit no "all clear" issue, comment, or notification.

#### Scenario: green run files nothing
- **GIVEN** every gate, skill eval, and doc-freshness check passes
- **WHEN** the nightly health run executes
- **THEN** no issue is filed and the issue-raise path is not invoked

#### Scenario: a persistent regression is not refiled nightly
- **GIVEN** a regression was filed as an open issue on a previous night and is still open
- **WHEN** the nightly health run executes again and the same regression is still failing
- **THEN** no duplicate issue is filed for it

#### Scenario: a new regression is filed once
- **GIVEN** a check that passed yesterday fails today with no matching open issue
- **WHEN** the nightly health run executes
- **THEN** exactly one issue is filed for it, carrying the failing output in its body

### Requirement: The sensor's own failure is observable, never silent

A failure of the health runner itself (as distinct from a gate failing) MUST be
recorded observably. The run MUST distinguish a `partial` outcome (some checks ran)
from a `fatal` outcome (the runner could not run the bundle at all); misclassifying
the two is a defect.

#### Scenario: runner crash surfaces as fatal
- **GIVEN** the health runner cannot execute the gate bundle at all (e.g. the
  checkout or tool invocation itself errors before any check runs)
- **WHEN** the nightly health run is attempted
- **THEN** the outcome is recorded as `fatal` in the run-status file, not as `partial`, and not silently swallowed
