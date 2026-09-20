# pursuit-audit-output Specification

## Purpose
Define the current behavioral contract for pursuit-audit-output, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Human audit summaries expose bounded finding details

For every affected pursuit file, human-readable `fieldkit pursuit audit` output SHALL show every critical finding and the first non-critical finding inline. If additional non-critical findings remain, it SHALL state their count and direct the operator to the generated report.

#### Scenario: File has one warning

- **GIVEN** an audit result with one warning and no critical finding
- **WHEN** the human summary is rendered
- **THEN** the warning message appears below that file's summary row
- **AND** no continuation count is shown

#### Scenario: File has several warnings

- **GIVEN** an audit result with three warnings
- **WHEN** the human summary is rendered
- **THEN** the first warning message appears below that file's summary row
- **AND** the output states that two more findings are available in the report

#### Scenario: File has critical and non-critical findings

- **GIVEN** an audit result with critical and non-critical findings
- **WHEN** the human summary is rendered
- **THEN** every critical message appears inline
- **AND** the first non-critical message appears exactly once

### Requirement: Existing audit output contracts remain stable

Inline detail rendering SHALL NOT change JSON output, generated report content, finding classification, or audit exit codes.

#### Scenario: Operator requests JSON

- **GIVEN** an audit result containing warnings
- **WHEN** the operator runs `fieldkit pursuit audit --json`
- **THEN** the existing JSON document is emitted without human summary lines

### Requirement: Pursuit audit fixes can be previewed without writes

`fieldkit pursuit audit --fix --dry-run` SHALL compute and display the same deterministic corrections as apply mode without changing pursuit files or writing an audit report.

#### Scenario: Preview finds correctable files
- **WHEN** one or more pursuit files contain auto-correctable fields
- **THEN** output identifies each file and its rename/removal counts
- **AND** reports the total corrections that would be applied
- **AND** all workspace bytes and mtimes remain unchanged

#### Scenario: Preview finds no corrections
- **WHEN** no pursuit file needs an auto-correction
- **THEN** output states that no auto-corrections are needed
- **AND** no workspace file is created or changed

#### Scenario: Preview completes
- **WHEN** preview calculation finishes
- **THEN** audit findings and exit status describe the unchanged files
- **AND** the default audit report is not written

#### Scenario: Dry-run is ambiguous
- **WHEN** `--dry-run` is supplied without `--fix`, or with `--check-yaml`, `--json`, or `--output`
- **THEN** fieldkit exits 3 with a usage error before workspace lookup

### Requirement: Applied pursuit audit fixes are atomic

Non-preview `fieldkit pursuit audit --fix` SHALL replace each changed pursuit file atomically after computing the same transformation used by preview mode.

#### Scenario: Operator applies corrections
- **WHEN** `fieldkit pursuit audit --fix` changes a pursuit file
- **THEN** readers observe either the complete old file or the complete corrected file
