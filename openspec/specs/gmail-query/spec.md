# gmail-query Specification

## Purpose
Define the current behavioral contract for gmail-query, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Gmail account query supports option and subcommand entry points

`fieldkit gmail query` SHALL accept `--account SLUG` as a nonbreaking alias for the existing `account NAME` subcommand and SHALL route both entry points through the same account-query behavior.

#### Scenario: Operator uses the account option alias

- **WHEN** the operator runs `fieldkit gmail query --account acme-corp`
- **THEN** fieldkit returns the same account query result produced by `fieldkit gmail query account acme-corp` with default options

#### Scenario: Existing account subcommand remains valid

- **WHEN** the operator runs `fieldkit gmail query account acme-corp`
- **THEN** the command retains its existing options, output, and exit behavior

#### Scenario: Alias is combined with a subcommand

- **WHEN** the operator supplies group-level `--account` and an explicit query subcommand
- **THEN** fieldkit exits 3 with a usage error before opening the Gmail database

#### Scenario: Query group has no selection

- **WHEN** the operator runs `fieldkit gmail query` without an account alias or subcommand
- **THEN** fieldkit displays query help without opening the Gmail database
