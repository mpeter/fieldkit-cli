# pursuit-health-exit-policy Specification

## Purpose
Define the current behavioral contract for pursuit-health-exit-policy, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Valid pursuit health reports succeed by default

`fieldkit pursuit health` and `fieldkit pursuit projects` MUST exit 0 after emitting a valid non-empty report unless strict mode is explicitly requested, regardless of attention findings.

#### Scenario: Default pursuit report contains HIGH findings
- **GIVEN** pursuit health classification returns at least one HIGH item
- **WHEN** `fieldkit pursuit health` runs without `--strict`
- **THEN** the complete report is emitted
- **AND** the command exits 0

#### Scenario: Default project report contains ZOMBIE or UNKNOWN findings
- **GIVEN** project health classification returns at least one ZOMBIE or UNKNOWN row
- **WHEN** `fieldkit pursuit projects` runs without `--strict`
- **THEN** the complete report is emitted
- **AND** the command exits 0

#### Scenario: Default JSON report contains attention findings
- **GIVEN** either health command has attention findings
- **WHEN** it runs with `--json` and without `--strict`
- **THEN** valid JSON is emitted with every finding
- **AND** the command exits 0

### Requirement: Strict mode converts attention findings to exit 1

Both health commands MUST accept `--strict` and MUST exit 1 after emitting a valid report when their existing attention predicate is true. Strict mode MUST exit 0 when that predicate is false.

#### Scenario: Strict pursuit report contains HIGH findings
- **GIVEN** pursuit health classification returns at least one HIGH item
- **WHEN** `fieldkit pursuit health --strict` runs
- **THEN** the complete report is emitted
- **AND** the command exits 1

#### Scenario: Strict project report contains ZOMBIE or UNKNOWN findings
- **GIVEN** project health classification returns at least one ZOMBIE or UNKNOWN row
- **WHEN** `fieldkit pursuit projects --strict` runs
- **THEN** the complete report is emitted
- **AND** the command exits 1

#### Scenario: Strict report is clean
- **GIVEN** the selected health command has no attention findings
- **WHEN** it runs with `--strict`
- **THEN** the complete report is emitted
- **AND** the command exits 0

### Requirement: Data errors remain independent of strict mode

Both health commands MUST preserve exit 3 for missing configuration, missing account directories, or no matching report rows whether or not `--strict` is supplied.

#### Scenario: Strict mode has no reportable data
- **GIVEN** the selected health command cannot produce a report because required data is absent
- **WHEN** it runs with `--strict`
- **THEN** it emits the existing data diagnostic
- **AND** exits 3
