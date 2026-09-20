# pipeline-account-review-opening Specification

## Purpose
Define how pipeline reviews are generated, persisted, and reopened for either the global pipeline or one configured account without falling back across scopes.

## Requirements
### Requirement: Pipeline generation persists an account-scoped review

`fieldkit pipeline --account SLUG` SHALL validate `SLUG` as a configured account before collection, SHALL render pursuit health, champion signals, and pursuit coverage only for that account, and SHALL save the result as `briefs/pipeline-review-SLUG-YYYY-MM-DD.md`. Running `fieldkit pipeline` without an account SHALL retain the global `briefs/pipeline-review-YYYY-MM-DD.md` contract.

#### Scenario: Scoped generation contains only the selected account

- **GIVEN** a workspace with configured accounts `acme-corp` and `example-co` and pursuits for both
- **WHEN** the operator runs `fieldkit pipeline --account acme-corp`
- **THEN** the saved filename contains `acme-corp` and the review date
- **AND** pursuit rows, champion signals, and pursuit coverage contain only `acme-corp`
- **AND** no global review artifact is created or replaced by that invocation

#### Scenario: Scoped generation preserves a zero-pursuit coverage result

- **GIVEN** `acme-corp` is configured but has no active pursuits
- **WHEN** the operator generates its scoped pipeline review
- **THEN** pursuit coverage contains the configured threshold and zero active pursuits for `acme-corp`
- **AND** pursuit coverage contains no other configured account

#### Scenario: Invalid account causes no collection or write

- **GIVEN** an unsafe or unconfigured account slug
- **WHEN** the operator requests scoped generation
- **THEN** the command exits with status 3 before collecting data
- **AND** no review artifact is written

### Requirement: Pipeline opening selects one explicit artifact scope

`fieldkit pipeline open --account SLUG` SHALL validate `SLUG` and open only the newest saved review for that account. `fieldkit pipeline open` SHALL open only the newest global review. Neither form SHALL fall back to the other artifact class.

#### Scenario: Scoped open ignores newer global and other-account reviews

- **GIVEN** saved global, `acme-corp`, and `example-co` reviews with different dates
- **WHEN** the operator runs `fieldkit pipeline open --account acme-corp`
- **THEN** the newest `acme-corp` review is sent to the system viewer
- **AND** no global or `example-co` review is opened

#### Scenario: Global open ignores scoped reviews

- **GIVEN** a global review and a newer account-scoped review
- **WHEN** the operator runs `fieldkit pipeline open`
- **THEN** the global review is sent to the system viewer

#### Scenario: Missing requested scope fails closed

- **GIVEN** reviews exist but none belong to the requested artifact scope
- **WHEN** the operator runs the corresponding open command
- **THEN** the command exits with status 3
- **AND** it reports the exact scoped or global generation command
- **AND** the system viewer is not invoked

#### Scenario: JSON identifies the opened scope

- **GIVEN** a matching saved review exists
- **WHEN** the operator opens it with `--json`
- **THEN** the response retains the selected path, URI, review date, age, stale status, and opened status
- **AND** `account` is the selected slug for a scoped review or null for a global review
