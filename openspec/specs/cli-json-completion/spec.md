# cli-json-completion Specification

## Purpose
Define the current behavioral contract for cli-json-completion, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Every eligible CLI leaf has a machine-readable result

Every leaf command not listed in the seven recorded JSON exemptions SHALL expose `--json`. JSON mode SHALL emit exactly one valid JSON document on stdout and SHALL suppress human rendering and progress output from stdout.

#### Scenario: Successful command emits one document
- **GIVEN** a non-exempt leaf command completes successfully with `--json`
- **WHEN** stdout is parsed as JSON
- **THEN** parsing succeeds as one complete document
- **AND** no human heading, table, progress line, or trailing prose appears outside that document

#### Scenario: Future eligible command omits JSON
- **GIVEN** a new non-exempt leaf is registered without `--json`
- **WHEN** the flag-contract gate runs
- **THEN** the gate fails regardless of the absolute number of other covered leaves

### Requirement: JSON rendering preserves behavior and exit taxonomy

Adding JSON output SHALL NOT change target selection, ordering, read/write classification, dry-run behavior, side effects, authentication propagation, or exit codes. Batch results SHALL expose ordered completed, skipped, and failed outcomes sufficient to identify the retry boundary.

#### Scenario: Dry run remains non-mutating
- **GIVEN** a workspace-writing command supports `--dry-run`
- **WHEN** it runs with both `--dry-run` and `--json`
- **THEN** its JSON describes the proposed outcome
- **AND** the workspace remains unchanged

#### Scenario: Partial batch remains partial
- **GIVEN** a batch command completes some items and fails another
- **WHEN** it runs with `--json`
- **THEN** the document identifies completed and failed items in deterministic order
- **AND** the command exits 1

### Requirement: JSON mode never requires interactive input

A command invoked with `--json` SHALL complete without prompting. If existing non-interactive selectors are insufficient, the invocation SHALL fail as caller data error before mutation.

#### Scenario: Interactive selection would be required
- **GIVEN** a JSON invocation omits the existing options needed to bypass a prompt
- **WHEN** the command validates its arguments
- **THEN** it exits 3 with no mutation
- **AND** it does not read from interactive input

### Requirement: JSON is produced from structured command outcomes

Commands SHALL serialize typed or explicitly structured outcomes from their domain/helper seams. They SHALL NOT capture and parse their own Click, Rich, logging, or subprocess prose to construct JSON.

#### Scenario: Human wording changes
- **GIVEN** a human renderer's wording changes without changing the domain outcome
- **WHEN** the corresponding command runs with `--json`
- **THEN** the JSON keys and values remain unchanged
