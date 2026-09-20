# workspace-bootstrap Specification

## Purpose
Define how `fieldkit init` creates a complete workspace interactively or from validated unattended input while preserving existing operator judgment.

## Requirements
### Requirement: Initialization supports validated unattended input

`fieldkit init` SHALL accept a YAML answers file containing the same managed values as the interactive wizard. It SHALL validate the complete document before writing any artifact, SHALL reject unknown keys, and SHALL run without reading stdin or requesting confirmation when the file is supplied.

#### Scenario: Valid answers initialize without prompts

- **GIVEN** a mapping-form answers file with non-empty `name`, `email`, and `data_dir` values
- **WHEN** the operator runs `fieldkit init --answers FILE`
- **THEN** initialization completes without reading stdin or requesting confirmation
- **AND** the managed config and workspace artifacts contain the supplied values

#### Scenario: Invalid answers cause no partial initialization

- **GIVEN** an answers file with a malformed root, unknown key, missing or empty required value, wrong value type, unsafe account name, OAuth secret without a client ID, or invalid ShadowBot identifier
- **WHEN** the operator runs `fieldkit init --answers FILE`
- **THEN** the command exits with the canonical data-error status 3
- **AND** no workspace or global configuration artifact is written

### Requirement: Initialization creates the complete judgment-config scaffold

Every successful interactive or answers-file initialization SHALL ensure the workspace contains the four operator judgment files with valid empty list roots when those files are absent.

#### Scenario: Fresh workspace receives all empty judgment files

- **GIVEN** the selected workspace has no judgment files
- **WHEN** initialization writes its artifacts
- **THEN** `config/clocks.json` parses as a mapping containing an empty `clocks` list
- **AND** `config/engines.json` parses as a mapping containing an empty `engines` list
- **AND** `config/people.json` parses as a mapping containing an empty `people` list
- **AND** `config/watchlist.json` parses as a mapping containing an empty `opportunities` list

#### Scenario: Rerun preserves operator judgment

- **GIVEN** one or more judgment files already exist with operator-authored bytes
- **WHEN** initialization is rerun
- **THEN** every existing judgment file remains byte-for-byte unchanged
- **AND** any missing judgment file is created with its valid empty root
