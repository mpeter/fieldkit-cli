# sf-set-identify-pain Specification

## Purpose
Define the current behavioral contract for sf-set-identify-pain, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Identify Pain is available through the guarded Opportunity writer

fieldkit SHALL include `Identify_Pain_Long__c` in the `Opportunity` entry of the
single `sf set-field` allowlist with the human label `Identify Pain Long`. The
field SHALL appear in command help and `--list-fields` output.

#### Scenario: Operator discovers the field

- **WHEN** the operator opens `sf set-field --help` or runs `sf set-field --list-fields`
- **THEN** the Opportunity field list includes `Identify_Pain_Long__c`
- **AND** its label is `Identify Pain Long`

### Requirement: Identify Pain writes retain existing safeguards

An Identify Pain invocation SHALL use the existing Opportunity write path. Without
confirmation it SHALL display the current and proposed values without writing.
With explicit confirmation it SHALL send exactly the requested
`Identify_Pain_Long__c` value to `update_opportunity_fields()`. Existing pre-write
record fetch, not-found, authentication, API-error, JSON, and exit behavior SHALL
remain unchanged.

#### Scenario: Preview is safe

- **GIVEN** a valid Opportunity id and Identify Pain narrative
- **WHEN** the operator invokes `sf set-field` without confirmation
- **THEN** fieldkit displays the proposed field change
- **AND** performs no Salesforce update

#### Scenario: Confirmed write uses the guarded payload

- **GIVEN** the same valid Opportunity id and narrative
- **WHEN** the operator invokes the existing explicit confirmation path
- **THEN** fieldkit calls the Opportunity writer once
- **AND** the payload contains only `Identify_Pain_Long__c` with the requested narrative
