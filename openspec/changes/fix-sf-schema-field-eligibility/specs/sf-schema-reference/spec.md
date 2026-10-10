## MODIFIED Requirements

### Requirement: Safe observed-population classification

The command MUST report every eligible described field as `observed-populated`, `observed-null`,
or `not-sampled`. A described field is eligible when its describe entry is a mapping with a string
`name` and it is not marked `deprecatedAndHidden: true`. Eligibility MUST NOT depend on any
attribute that Salesforce reports only at the object level, including `queryable`. A non-`null`
observed value, including `0`, `false`, or an empty string, SHALL be `observed-populated`; a field
observed only with `null` SHALL be `observed-null`; a field absent from all returned samples SHALL
be `not-sampled`.

#### Scenario: samples have mixed field states
- **GIVEN** explicit record responses containing populated, null, and absent fields
- **WHEN** the command aggregates the samples
- **THEN** it SHALL render the corresponding observed-population state for each field

#### Scenario: describe entries carry no per-field queryable attribute
- **GIVEN** a describe response whose object-level `queryable` is `true` and whose field entries
  have `name`, `label`, and `type` but no `queryable` key
- **WHEN** the operator runs the schema command for one valid record ID
- **THEN** the output SHALL list every described field that is not `deprecatedAndHidden`

#### Scenario: a field is deprecated and hidden
- **GIVEN** a describe response containing a field marked `deprecatedAndHidden: true`
- **WHEN** the command renders the schema reference
- **THEN** that field SHALL NOT appear in the output or in any record request

## ADDED Requirements

### Requirement: An empty eligible field set is a data error

The command MUST exit with the data-error outcome when the describe response contains no
eligible field, including an empty `fields` list. It MUST NOT render an empty reference as success
and MUST NOT substitute a fallback field list for the record requests.

#### Scenario: every described field is ineligible
- **GIVEN** a describe response whose fields are all `deprecatedAndHidden: true`, or whose `fields` list is empty
- **WHEN** the operator runs the schema command
- **THEN** the CLI SHALL exit 3 with a message stating that no eligible fields were described
- **AND** no record request SHALL be made
