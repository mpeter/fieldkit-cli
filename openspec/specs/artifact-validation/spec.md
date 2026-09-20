# artifact-validation Specification

## Purpose
Define the current behavioral contract for artifact-validation, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Built artifacts are inspected through a versioned policy

The project MUST validate wheel and source-distribution metadata and contents through a checked-in,
fail-closed policy. Validation MUST emit human-readable results and versioned JSON with source
revision, artifact digest, criterion identifiers, and status.

#### Scenario: Required packaged asset is missing
- **GIVEN** a wheel omits a declared template, schema, SQL file, bundled skill, or web static asset
- **WHEN** artifact validation runs
- **THEN** it fails the corresponding asset-family criterion and names the missing archive path

#### Scenario: Forbidden content enters an artifact
- **GIVEN** an artifact contains a cache, credential, runtime database, private config, test tree,
  local agent state, or disallowed planning/history path
- **WHEN** artifact validation runs
- **THEN** it fails with artifact path and policy rule
- **AND** it does not print sensitive file contents

### Requirement: Artifact smoke runs outside the checkout

Wheel and source-distribution smoke tests MUST install into empty environments outside the source
tree, remove the checkout from Python import resolution, and exercise the exact artifact digest
recorded in their evidence.

#### Scenario: Source tree masks a packaging omission
- **GIVEN** a runtime file exists in the checkout but not in the built wheel
- **WHEN** installed-artifact smoke runs from an unrelated directory
- **THEN** resolution cannot fall back to the checkout
- **AND** the smoke fails against the recorded wheel digest

#### Scenario: Wheel and source distribution disagree
- **GIVEN** wheel and source artifacts built from one revision
- **WHEN** their metadata, required content, and installation smoke are compared
- **THEN** any user-visible capability present in only one artifact fails validation
