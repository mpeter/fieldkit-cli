# golive-revenue-sourcing Specification

## Purpose
Define the current behavioral contract for golive-revenue-sourcing, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Go-live revenue rows are deterministic and sourced

fieldkit SHALL assemble each Salesforce CPQ quote line into a typed revenue
row containing its quote identifier, SKU, product family, shared revenue
bucket, quantity, unit price, annual value, and a source reference of the form
`sf:quote-line:<record-id>`. It SHALL sort rows by the shared bucket order and
then SKU, and SHALL NOT add timestamps or inferred monetary values.

#### Scenario: A fixed opportunity is assembled repeatedly

- **GIVEN** one fixed set of quote lines with stable Salesforce record ids
- **WHEN** the revenue block is assembled twice
- **THEN** both results are identical and use the shared family buckets
- **AND** every emitted annual value carries its quote-line source reference

#### Scenario: Quantity or unit price is absent

- **GIVEN** a quote line with an annual value but no quantity or unit price
- **WHEN** the revenue block is assembled
- **THEN** the missing values remain null
- **AND** fieldkit does not derive a replacement unit price from other fields

### Requirement: The go-live command reports all quote lines with provenance

`fieldkit golive <OPP_NUMBER_OR_ID>` SHALL resolve a 5–12 digit Salesforce
Opportunity Number or accept a 15/18-character Salesforce opportunity id,
then fetch quote lines through the shared Salesforce component walk and
render every quote represented in the result.
Human output SHALL include per-bucket totals, a grand total, and a trailing
source-reference column. `--json` SHALL emit the deterministic typed revenue
rows.

#### Scenario: An opportunity has multiple quotes

- **GIVEN** an opportunity whose component walk returns lines from two quotes
- **WHEN** the operator runs `fieldkit golive <OPP_NUMBER_OR_ID>`
- **THEN** lines from both quotes are visible and grouped by quote identifier
- **AND** totals are computed from the emitted annual values

#### Scenario: An opportunity has no quote lines

- **GIVEN** an opportunity whose component walk returns no quote lines
- **WHEN** the operator runs the human command
- **THEN** it prints `no quote lines — nothing to source`
- **AND** exits 0

#### Scenario: JSON output is empty

- **GIVEN** an opportunity whose component walk returns no quote lines
- **WHEN** the operator runs the command with `--json`
- **THEN** it emits an empty JSON list
- **AND** exits 0

### Requirement: Go-live command failures use the central exit contract

The go-live command SHALL allow Salesforce authentication failures to reach
the central CLI exception mapper and SHALL classify an unknown opportunity as
a data error.

#### Scenario: Salesforce authentication fails

- **GIVEN** an expired or invalid Salesforce session
- **WHEN** the operator runs the go-live command
- **THEN** the authentication exception propagates to `cli_main()`
- **AND** the process exits 2

#### Scenario: The opportunity cannot be resolved

- **GIVEN** an opportunity identifier or number that does not resolve
- **WHEN** the operator runs the go-live command
- **THEN** it reports that the opportunity was not found
- **AND** exits 3
