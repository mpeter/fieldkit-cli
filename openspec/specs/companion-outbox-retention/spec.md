# companion-outbox-retention Specification

## Purpose
Define the current behavioral contract for companion-outbox-retention, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Proposal expiry is explicit, bounded, and previewed

The system SHALL provide `fieldkit companion prune`, defaulting to proposals at
least 14 days old and a maximum batch of 25. Without `--confirm`, or with
`--dry-run`, it SHALL report the exact candidates and perform no write. Combining
`--confirm` and `--dry-run` SHALL be rejected as caller data error.

#### Scenario: Default preview
- **GIVEN** valid pending proposals both older and newer than 14 days
- **WHEN** the operator runs `fieldkit companion prune`
- **THEN** only the old proposals appear as selected
- **AND** every proposal and suppression file remains unchanged

#### Scenario: Confirmed batch is bounded
- **GIVEN** more eligible proposals than the selected limit
- **WHEN** the operator confirms pruning
- **THEN** at most that many oldest eligible proposals are retired
- **AND** the result reports the remaining eligible count

### Requirement: Unsafe proposal states fail closed

The system SHALL exclude legacy Markdown, invalid envelopes, and claimed or
otherwise non-approvable envelopes from automatic expiry.

#### Scenario: Unknown execution outcome is preserved
- **GIVEN** a version-1 proposal whose command is null because execution may be in progress
- **WHEN** a confirmed prune pass runs
- **THEN** that proposal remains in the outbox
- **AND** the result reports it as skipped

### Requirement: Expiry reactivates a condition deliberately

Before removing an eligible proposal, the system SHALL apply the normal 24-hour
successful-action cooldown and revalidate the locked proposal identity. After
removal it SHALL record an expired outcome. A still-true condition SHALL become
eligible only after the cooldown expires.

#### Scenario: Expired condition does not return immediately
- **GIVEN** an old proposal suppresses a still-true attention item
- **WHEN** the proposal is successfully expired
- **THEN** the proposal is absent from the pending outbox
- **AND** the item remains suppressed for 24 hours
- **AND** it may return after that cooldown if its source condition persists

#### Scenario: Proposal changes during retirement
- **GIVEN** the selected proposal no longer matches the identity validated under its lock
- **WHEN** retirement attempts to commit
- **THEN** the proposal is not deleted
- **AND** the result reports a conflict or partial outcome

### Requirement: Proposal usefulness has durable outcome evidence

The system SHALL record successful approvals and expiries in month-rotated JSONL
without proposal bodies, and SHALL report the observed ignored rate as unique
expired outcomes divided by unique approved plus expired outcomes.

#### Scenario: Approval contributes to the denominator
- **GIVEN** an approvable proposal executes successfully
- **WHEN** its outbox claim is completed
- **THEN** one approved lifecycle outcome is recorded for that proposal

#### Scenario: Retry does not inflate the ignored rate
- **GIVEN** an expiry outcome was appended before an interrupted deletion
- **WHEN** a later prune retries that proposal
- **THEN** outcome aggregation counts the proposal name once

#### Scenario: No terminal outcomes exist
- **GIVEN** no approved or expired outcome has been recorded
- **WHEN** prune reports statistics
- **THEN** ignored rate is unavailable rather than zero percent
