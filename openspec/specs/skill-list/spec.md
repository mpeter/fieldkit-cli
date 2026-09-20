# skill-list Specification

## Purpose
Define the current behavioral contract for skill-list, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Skill list can show complete descriptions

`fieldkit skill list` SHALL accept `-v` and `--verbose`. In human mode, the option SHALL emit every selected skill's complete stored description without first-sentence reduction or character truncation.

#### Scenario: Operator requests verbose output

- **GIVEN** a skill description containing multiple sentences and more than 70 characters
- **WHEN** the operator runs `fieldkit skill list --verbose`
- **THEN** the complete description is present in the human output
- **AND** the compact truncated description is not substituted for it

#### Scenario: Operator uses the short flag with a group

- **GIVEN** skills inside and outside a requested group
- **WHEN** the operator runs `fieldkit skill list -v --group GROUP`
- **THEN** only matching skills are rendered
- **AND** their complete descriptions are present

### Requirement: Existing skill list contracts remain stable

Verbose support SHALL preserve default compact human output, JSON content, ordering, summaries, no-match behavior, and exit status.

#### Scenario: Verbose mode is omitted

- **WHEN** the operator runs `fieldkit skill list`
- **THEN** descriptions retain the existing first-sentence and 70-character compact rendering

#### Scenario: JSON and verbose are combined

- **WHEN** the operator runs `fieldkit skill list --json --verbose`
- **THEN** the output is the same JSON payload produced by `--json` alone
