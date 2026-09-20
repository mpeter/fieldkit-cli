# ingest-reprocess Specification

## Purpose
Define safe, observable selection and retry behavior for persisted ingest artifacts.
## Requirements
### Requirement: Explicit all-version reprocessing

`fieldkit ingest reprocess` SHALL accept `--force` to select artifacts for the requested pipeline regardless of stored pipeline version. Force selection SHALL continue to apply `--account` and `--limit`, and SHALL preserve deterministic creation order.

#### Scenario: Current-version artifact is selected

- **GIVEN** a transcript artifact whose stored pipeline version equals the current transcript-ingest version
- **WHEN** the operator runs `fieldkit ingest reprocess --pipeline transcript-ingest --force --dry-run`
- **THEN** the artifact is listed as pending reprocessing
- **AND** the command exits 0

#### Scenario: Account and limit constrain force selection

- **GIVEN** current-version transcript artifacts in multiple account paths
- **WHEN** the operator supplies `--force`, `--account <slug>`, and `--limit 1`
- **THEN** exactly the first matching artifact for that account is selected

### Requirement: Force selector validation

`--force` and `--from-version` SHALL be mutually exclusive. The root CLI SHALL reject their combination as caller data error exit 3 before opening the ingest database or acquiring a document service.

#### Scenario: Conflicting selectors are rejected

- **GIVEN** no database or document-service access has occurred
- **WHEN** the operator supplies both `--force` and `--from-version 0.1.0`
- **THEN** the command explains that the selectors cannot be combined
- **AND** exits 3 without performing reprocessing

#### Scenario: Existing omission guard remains

- **GIVEN** force mode is not requested
- **WHEN** the operator starts a live reprocess run without `--from-version`
- **THEN** the existing version-guard warning is emitted
- **AND** the command exits 1 before remote or file mutation

### Requirement: Force mode remains observable

Human dry-run output SHALL identify force selection. Every JSON result document SHALL contain a boolean `force` field and SHALL retain ordered `completed`, `skipped`, `failed`, and `pending` outcome groups.

#### Scenario: Forced JSON preview is self-describing

- **GIVEN** two selected current-version artifacts
- **WHEN** the operator runs force mode with `--dry-run --json`
- **THEN** stdout contains exactly one JSON document with `force: true`
- **AND** both source identifiers appear in input order under `pending`

#### Scenario: Version-selected JSON remains explicit

- **GIVEN** a reprocess run selected by `--from-version`
- **WHEN** JSON output is requested
- **THEN** the result document contains `force: false`
- **AND** retains the existing batch outcome structure

### Requirement: Successful degradation remains observable

`fieldkit ingest run` SHALL classify a successfully written artifact as degraded when either transcript LLM stage uses its exception fallback or the final extracted confidence is `low`. A deliberate `NO_LLM` result with confidence `stub` SHALL NOT be classified as degraded unless a stage fallback occurred.

#### Scenario: Stage fallback is degraded success

- **GIVEN** transcript processing catches an exception from either LLM stage
- **WHEN** the fallback artifact is written successfully
- **THEN** the source is counted as processed and degraded
- **AND** it is not counted as an error

#### Scenario: Low-confidence extraction is degraded success

- **GIVEN** both stages return normally with final confidence `low`
- **WHEN** the artifact is written successfully
- **THEN** the source is counted as processed and degraded

#### Scenario: Deliberate stub remains distinct

- **GIVEN** no stage raises and extraction returns confidence `stub`
- **WHEN** the artifact is written successfully
- **THEN** the source is counted as processed without being counted as degraded

### Requirement: Run summaries report degradation

A live ingest run SHALL report the degraded count in its human summary and ordered degraded source identifiers in JSON. Degraded sources SHALL remain members of the processed/completed result set, and degradation alone SHALL NOT change the command exit status.

#### Scenario: Human summary exposes degraded count

- **GIVEN** a run writes two artifacts and one is degraded
- **WHEN** the human summary is emitted
- **THEN** it reports `2 processed (1 degraded)`
- **AND** retains the skipped and error counts

#### Scenario: JSON preserves completed retry boundaries

- **GIVEN** parallel completion order differs from input order and one successful artifact is degraded
- **WHEN** `fieldkit ingest run --json` completes without errors
- **THEN** the degraded source appears in both `completed` and `degraded`
- **AND** both lists use original input order
- **AND** the command exits 0

#### Scenario: Other batch JSON remains compatible

- **GIVEN** an ingest command does not supply degradation outcomes
- **WHEN** it emits a batch JSON document
- **THEN** its existing JSON keys and bytes remain unchanged
