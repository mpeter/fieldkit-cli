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
