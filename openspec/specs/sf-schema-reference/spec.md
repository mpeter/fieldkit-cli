# sf-schema-reference Specification

## Purpose
Define the current behavioral contract for sf-schema-reference, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Explicit-record schema reference

The `fieldkit sf schema <object> --record-id ID ...` command MUST retrieve describe metadata for
the validated object and MUST retrieve only the explicitly supplied record IDs. It MUST NOT use
SOQL, SOSL, record discovery, or persistent output. It MUST reject more than ten record IDs
before constructing an API client.

#### Scenario: explicit sample is inspected
- **GIVEN** a valid object API name and one or more valid record IDs
- **WHEN** the operator runs the schema command
- **THEN** the client SHALL request object describe metadata and only those record IDs

#### Scenario: oversized explicit sample is rejected
- **GIVEN** more than ten valid record IDs
- **WHEN** the operator runs the schema command
- **THEN** the CLI SHALL report a data error without constructing an API client

### Requirement: Safe observed-population classification

The command MUST report every eligible described field as `observed-populated`, `observed-null`,
or `not-sampled`. A non-`null` observed value, including `0`, `false`, or an empty string, SHALL
be `observed-populated`; a field observed only with `null` SHALL be `observed-null`; a field
absent from all returned samples SHALL be `not-sampled`.

#### Scenario: samples have mixed field states
- **GIVEN** explicit record responses containing populated, null, and absent fields
- **WHEN** the command aggregates the samples
- **THEN** it SHALL render the corresponding observed-population state for each field

### Requirement: Schema output does not disclose sample values

The command MUST render describe metadata, observed-population state, and supplied sample count
without rendering or logging values from any sampled record.

#### Scenario: populated field contains a sensitive value
- **GIVEN** a sampled record with a non-null sensitive field value
- **WHEN** the command renders the schema reference
- **THEN** the output SHALL identify the field as `observed-populated` without containing that
  value

### Requirement: Invalid or incomplete samples fail explicitly

The command MUST validate the object API name and record IDs before making a request. It SHALL
report malformed identifiers or a requested record that cannot be read as a data error, and SHALL
preserve authentication failures as authentication errors.

#### Scenario: supplied record is unavailable
- **GIVEN** a syntactically valid record ID that Salesforce does not return
- **WHEN** the command reads the explicit sample
- **THEN** the CLI SHALL exit with the data-error outcome rather than render a partial reference
