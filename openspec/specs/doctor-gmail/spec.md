# doctor-gmail Specification

## Purpose
Define the current behavioral contract for doctor-gmail, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Gmail doctor accepts an explicit database path

`fieldkit doctor gmail` SHALL accept `--db PATH` and inspect that database instead of the configured Gmail database. When omitted, it SHALL inspect the configured database as before.

#### Scenario: Operator selects a healthy database

- **GIVEN** a structurally sound Gmail database at an explicit path
- **WHEN** the operator runs `fieldkit doctor gmail --db PATH`
- **THEN** the selected database is inspected
- **AND** the command exits 0 with the existing healthy result shape

#### Scenario: Operator selects a missing database

- **GIVEN** an explicit path that does not exist
- **WHEN** the operator runs `fieldkit doctor gmail --db PATH`
- **THEN** the command returns the existing unconfigured Gmail doctor result
- **AND** exits 2 rather than a Click usage error

#### Scenario: Operator omits the override

- **GIVEN** a configured Gmail database path
- **WHEN** `fieldkit doctor gmail` or the root `fieldkit doctor` aggregate runs without `--db`
- **THEN** the configured database is inspected

### Requirement: Gmail doctor output contracts remain stable

Selecting a database SHALL NOT alter the human or JSON result schemas, database integrity checks, or healthy/unhealthy exit mapping.

#### Scenario: Explicit database is requested with JSON

- **GIVEN** an explicit database path
- **WHEN** the operator runs `fieldkit doctor gmail --db PATH --json`
- **THEN** the existing Gmail doctor JSON object is emitted for the selected database
