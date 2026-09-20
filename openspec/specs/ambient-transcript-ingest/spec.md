# ambient-transcript-ingest Specification

## Purpose
Define the current behavioral contract for ambient-transcript-ingest, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Ambient discovery registers only stable completed snapshots

`fieldkit ingest discover --pipeline ambient-transcript-ingest` SHALL discover validated `session-*.jsonl` files under the configured fieldkit home's `scratch/ambient` directory, exclude the lexicographically newest session by default, and register each stable content hash at most once.

#### Scenario: Active newest session is protected
- **GIVEN** two or more ambient session files exist
- **WHEN** discovery runs without `--include-latest`
- **THEN** every stable file except the newest is eligible
- **AND** the newest file is neither read for ingest nor registered

#### Scenario: Explicit stopped-recorder override includes the latest snapshot
- **GIVEN** the operator knows the companion is stopped
- **WHEN** discovery runs with `--include-latest`
- **THEN** the newest file is eligible only if its size and modification time remain stable around the bounded read

### Requirement: Ambient JSONL is validated and bounded

Each eligible source SHALL contain valid JSON objects with the documented segment fields, remain under the ambient root, and fit the configured ingest input limits. Invalid sources SHALL fail individually without aborting unrelated sources.

#### Scenario: A file changes or contains a malformed segment
- **GIVEN** an ambient source changes after discovery or contains invalid JSONL
- **WHEN** processing verifies the source
- **THEN** no meeting artifact is written for that source
- **AND** the source is failed and the batch reports a partial result

### Requirement: Noise and uncertain routes do not create meeting artifacts

The pipeline SHALL mark obvious noise as a successful skip and SHALL defer sources whose content matches zero or multiple configured accounts.

#### Scenario: Session contains no meaningful transcript
- **GIVEN** a stable session contains only blank or stub transcription content or remains below the meaningful transcript threshold
- **WHEN** it is processed
- **THEN** it is marked processed with a durable noise outcome
- **AND** no meeting note or LLM call is produced

#### Scenario: Content does not identify exactly one account
- **GIVEN** a meaningful session matches zero or several account keyword sets
- **WHEN** it is processed
- **THEN** it returns to pending and is reported as deferred
- **AND** no artifact is written and the batch exits partial

### Requirement: Routed sessions reuse extraction with ambient provenance

A session matching exactly one account SHALL pass its bounded speaker-labelled text through the existing transcript extraction stages and write an account meeting note with ambient source provenance and pursuit matching.

#### Scenario: One account is identified from transcript content
- **GIVEN** a meaningful validated session contains a whole-word keyword for exactly one configured account
- **WHEN** the pipeline processes it
- **THEN** it writes a meeting note under that account with extracted participants, topics, decisions, and actions
- **AND** frontmatter records `ambient-transcript-ingest`, the source hash and root-relative path, version, and low routing confidence without a Google Doc URL

### Requirement: Ambient processing is claim-safe and observable

Concurrent runs SHALL use the existing atomic source claim, preserve per-source terminal or retryable state, and report ordered processed, skipped, deferred, and failed outcomes in human and JSON modes.

#### Scenario: Two workers target the same source
- **GIVEN** an ambient source is pending
- **WHEN** concurrent workers try to process it
- **THEN** exactly one worker claims it
- **AND** at most one terminal artifact is recorded
