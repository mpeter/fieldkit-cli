## Purpose

Keep Gmail retry-warning records visible to established alert routing.

## Requirements

### Requirement: Gmail retry warnings retain alert-routing identity

The Gmail retry helper MUST emit recoverable-retry warning records with logger
name `gmail-sync`.

#### Scenario: Recoverable Gmail API request retries
- **GIVEN** a Gmail API call fails once with a retryable HTTP response
- **WHEN** the retry helper schedules another attempt
- **THEN** the emitted warning record SHALL have name `gmail-sync`

### Requirement: Gmail retry behavior

The Gmail retry helper MUST preserve retry classification, retry policy, and
error behavior while using the established `gmail-sync` warning identity.

#### Scenario: Retry succeeds after a transient failure
- **GIVEN** a Gmail API call raises a retryable error and then succeeds
- **WHEN** the retry helper executes it
- **THEN** it SHALL return the successful result after retrying
