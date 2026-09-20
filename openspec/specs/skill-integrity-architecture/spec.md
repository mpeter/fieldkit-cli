# skill-integrity-architecture Specification

## Purpose
Define the current behavioral contract for skill-integrity-architecture, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Explicit repository context

Every skill-integrity operation that reads repository files MUST receive an `IntegrityContext` derived from one repository root rather than reading mutable path globals.

#### Scenario: Production invocation

- **WHEN** `uv run python scripts/check_skill_integrity.py` runs without an injected context
- **THEN** the validator derives all paths from the checkout containing the executable script

#### Scenario: Isolated fixture invocation

- **WHEN** a test invokes the validator with a context rooted at `tmp_path`
- **THEN** every registry and validation read remains inside that fixture root

### Requirement: Stable executable contract

`scripts/check_skill_integrity.py` MUST remain the executable CLI owner and preserve its arguments, human output, JSON report schema, violation ordering, warnings, and exit semantics.

#### Scenario: Clean corpus

- **WHEN** the validator scans a corpus with no error-severity violations
- **THEN** it emits the same report contract and exits zero

#### Scenario: Invalid corpus

- **WHEN** one or more error-severity violations are found
- **THEN** the report retains their codes, metadata, messages, suggestions, and deterministic order and the process exits one

### Requirement: Cohesive bounded modules

The validator implementation MUST separate model/context, corpus/schema, references, graph, governance, and reporting concerns under `scripts/skill_integrity/`, with no implementation module exceeding 400 lines and the executable script below 500 lines.

#### Scenario: Completed extraction

- **WHEN** file sizes are measured after the final implementation phase
- **THEN** the executable and every extracted module satisfy their respective line limits without re-export shims

### Requirement: Test isolation without dynamic module rebinding

Validator tests MUST use ordinary imports and injected contexts instead of dynamically loading the executable and rebinding its module-level path constants.

#### Scenario: Parallel test workers

- **WHEN** validator tests execute under xdist with separate `tmp_path` fixtures
- **THEN** each invocation reads only its own fixture corpus and shares no mutable path state

### Requirement: Evidence-based AgentReady ratchet

The implementation MUST measure AgentReady after the split and MUST NOT lower the `file_size_limits` floor. An upward floor change is permitted only when it does not exceed the measured post-split score.

#### Scenario: Score improves

- **WHEN** the post-split `file_size_limits` score exceeds the committed floor
- **THEN** the final implementation may raise the floor to the observed score and records the measurement

#### Scenario: Score does not improve

- **WHEN** the measured score is equal to or below the committed floor
- **THEN** the floor remains unchanged and the discrepancy is reported without weakening another gate
