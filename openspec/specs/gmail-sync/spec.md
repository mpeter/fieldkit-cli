# gmail-sync Specification

## Purpose
Define the current behavioral contract for gmail-sync, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Per-message batch outcomes

The Gmail sync engine SHALL account for every identifier submitted to a batch fetch as either a successful message, a terminal HTTP 404 disappearance, or an unresolved failure. It SHALL NOT include message identifiers or message content in failure logs or summaries.

#### Scenario: One callback returns HTTP 404
- **WHEN** one requested message callback returns HTTP 404 and another succeeds
- **THEN** the result contains one successful message and one `not_found` failure
- **THEN** no log line contains either message identifier

#### Scenario: One callback returns a non-auth, non-404 exception
- **WHEN** one requested message callback returns a non-auth, non-404 exception
- **THEN** the result counts one unresolved failure

#### Scenario: One callback returns an authentication failure
- **WHEN** one requested message callback returns HTTP 401 or a non-quota HTTP 403
- **THEN** the batch raises `GmailAuthError` instead of returning a partial result

#### Scenario: Credential refresh fails before callbacks
- **WHEN** Gmail's batch 401 handling raises a permanent credential refresh error before invoking callbacks
- **THEN** the batch raises `GmailAuthError` instead of a generic data error

#### Scenario: One callback returns a quota failure
- **WHEN** one requested message callback returns an HTTP 403 classified as quota or rate limiting
- **THEN** the result counts one unresolved failure

#### Scenario: Callback returns neither response nor exception
- **WHEN** a callback contains no response and no exception
- **THEN** the result counts one unresolved failure

### Requirement: Partial sync result and summary

`fieldkit gmail sync` SHALL report successful additions and failures by category and SHALL exit 1 whenever at least one requested message was not fetched. Authentication failure SHALL continue to exit 2.

#### Scenario: Mixed success and 404
- **WHEN** a sync fetches one message and receives one HTTP 404
- **THEN** its final summary reports one added and one failed as not found
- **THEN** the command exits 1

#### Scenario: All requested messages succeed
- **WHEN** every requested message is fetched
- **THEN** the command exits 0

### Requirement: Retry-safe checkpoints

The sync engine SHALL advance beyond terminal HTTP 404 disappearances and SHALL NOT advance beyond unresolved per-message failures.

#### Scenario: Full sync has an unresolved callback failure
- **WHEN** a full-sync page has a non-404 callback failure
- **THEN** the persisted checkpoint identifies the current page rather than the next page
- **THEN** processing stops after the first affected chunk without requesting later chunks or pages
- **THEN** this retry boundary takes precedence over `--max-messages`
- **THEN** initial sync is not marked complete

#### Scenario: Full sync authenticates unsuccessfully in a later chunk
- **WHEN** a later chunk in a full-sync page raises `GmailAuthError`
- **THEN** the current page checkpoint remains published so the entire page is replayed
- **THEN** the auth failure exits 2

#### Scenario: Full sync reaches max messages mid-page
- **WHEN** `--max-messages` is reached before every chunk in the current page is accounted for
- **THEN** sync finishes every chunk in that page and publishes its successor checkpoint
- **THEN** sync stops before requesting the successor page
- **THEN** a second capped run resumes at the successor and makes forward progress

#### Scenario: Incremental sync has an unresolved callback failure
- **WHEN** an incremental history page has a non-404 callback failure
- **THEN** deletion and label events already present on that page are processed
- **THEN** no successor history page is requested
- **THEN** `last_history_id` remains at its starting value

#### Scenario: Full sync has only 404 failures
- **WHEN** every callback failure in a full-sync page is HTTP 404
- **THEN** its successor page checkpoint is persisted because the missing messages are terminally unavailable
- **THEN** the command still exits 1 for that run

#### Scenario: Incremental sync has only 404 failures
- **WHEN** every callback failure in incremental history is HTTP 404
- **THEN** `last_history_id` advances to the `historyId` from the final processed `history.list` response because the missing messages are terminally unavailable
- **THEN** a later `getProfile` snapshot is not used as the processed boundary
- **THEN** the command still exits 1 for that run

### Requirement: Gmail sync supports a forced resumable full refresh

`fieldkit gmail sync --full` SHALL start a full-mailbox upsert scan even when incremental state is valid. A forced full operation SHALL retain page checkpoints across interruption and SHALL NOT delete existing message or thread rows.

#### Scenario: Operator starts a forced full refresh

- **GIVEN** a database marked complete with a valid history ID
- **WHEN** the operator runs `fieldkit gmail sync --full`
- **THEN** incremental sync is bypassed
- **AND** a new full scan starts from the first message page
- **AND** the forced operation is marked incomplete until finalization

#### Scenario: Forced refresh is interrupted and resumed

- **GIVEN** an incomplete forced full operation with a saved page token
- **WHEN** the operator runs `fieldkit gmail sync --full` again or runs it without a selection flag
- **THEN** the full scan resumes from that page token

#### Scenario: Forced refresh completes

- **WHEN** the final full-scan page is committed and history replay from the pre-scan ID succeeds
- **THEN** all scan-window message, label, and deletion events have been reconciled
- **AND** the database is marked complete with the replay's latest history ID
- **AND** the forced-operation marker and page checkpoint are cleared

#### Scenario: Mailbox changes during forced refresh

- **GIVEN** a history event occurs after the pre-scan checkpoint is captured
- **WHEN** the forced refresh reaches the end of its message scan
- **THEN** history replay starts from the pre-scan checkpoint before complete state is published
- **AND** the event is reconciled before history advances and the operation completes

#### Scenario: Pre-scan history expires before completion

- **GIVEN** Gmail rejects the captured pre-scan history ID with HTTP 404 at finalization
- **WHEN** the full message scan reaches exhaustion
- **THEN** the database remains marked incomplete
- **AND** the scan checkpoint restarts at the first page with a newly captured history ID
- **AND** the command exits 1 with a retry message

### Requirement: Gmail sync supports resumable date-bounded backfill

`fieldkit gmail sync --since YYYY-MM-DD` SHALL upsert messages on or after UTC midnight of that date using a Gmail epoch query and isolated checkpoints.

#### Scenario: Operator starts a bounded backfill

- **WHEN** the operator runs `fieldkit gmail sync --since 2026-06-01`
- **THEN** message listing uses an `after:` epoch boundary that includes UTC midnight on June 1
- **AND** matching messages are processed through the normal batch upsert path

#### Scenario: Same bounded date resumes

- **GIVEN** a saved bounded page token for a date
- **WHEN** the same `--since` date is requested again
- **THEN** the bounded scan resumes from that token

#### Scenario: Bounded query matches no messages

- **WHEN** the first result page contains no messages
- **THEN** the bounded operation is finalized and its isolated checkpoint is cleared

#### Scenario: Bounded date changes

- **GIVEN** a saved bounded checkpoint for one date
- **WHEN** a different `--since` date is requested
- **THEN** only the bounded checkpoint is reset
- **AND** the new query starts from its first page

#### Scenario: Bounded scan preserves global continuity

- **WHEN** a bounded scan starts, stops at `--max-messages`, raises an API/auth exception, or completes
- **THEN** `initial_sync_complete`, `last_history_id`, and full-scan checkpoint state remain unchanged

#### Scenario: Scanner operation raises

- **GIVEN** message listing or fetching raises an API, auth, or retry-policy exception
- **WHEN** a full or bounded scan is running
- **THEN** the exception propagates unchanged to fieldkit's existing top-level taxonomy
- **AND** no completion cleanup runs

### Requirement: Gmail sync selection and limit contracts are explicit

Automatic state-driven selection SHALL remain the default. `--full` and `--since` SHALL be mutually exclusive, and `--max-messages` SHALL apply cumulatively to the selected resumable operation.

#### Scenario: No selection flag is supplied

- **WHEN** Gmail sync runs without `--full` or `--since`
- **THEN** complete state selects incremental sync and incomplete state selects or resumes full sync as before

#### Scenario: Selection flags conflict

- **WHEN** `--full` and `--since` are supplied together
- **THEN** fieldkit exits 3 with a usage error before authentication or database mutation

#### Scenario: Message ceiling pauses an operation

- **WHEN** the persisted operation count reaches a nonzero `--max-messages` ceiling
- **THEN** the scan stops with its checkpoint intact
- **AND** no unprocessed message from the fetched page is skipped by that checkpoint
- **AND** a later invocation with a larger ceiling or zero resumes it

#### Scenario: Message ceiling equals mailbox size

- **GIVEN** the fully processed page reaches the message ceiling and has no next-page token
- **WHEN** scan outcome is selected
- **THEN** the operation is exhausted rather than paused
- **AND** its completion reconciliation and cleanup run immediately

#### Scenario: Message ceiling is negative

- **WHEN** the operator supplies `--max-messages -1`
- **THEN** fieldkit exits 3 with a usage error before authentication or database mutation

#### Scenario: Message ceiling is zero

- **WHEN** the operator supplies `--max-messages 0`
- **THEN** the selected operation has no message ceiling

### Requirement: Gmail sync operations are serialized per database

Only one Gmail sync command SHALL modify a selected database's sync state at a time.

#### Scenario: Another sync owns the database

- **GIVEN** one Gmail sync holds the selected database's operation lock
- **WHEN** a second full, incremental, or bounded sync targets that database
- **THEN** the second command writes `Gmail sync already running for the selected database.` to stderr
- **AND** exits 1 before authentication or state mutation

#### Scenario: Two paths alias one database

- **GIVEN** two absolute, relative, or symlink paths resolve to the same SQLite database
- **WHEN** concurrent syncs use those paths
- **THEN** both derive the same operation lock

### Requirement: Non-interactive consent requirements fail as authentication errors

When Gmail sync requires a new interactive OAuth consent flow, the command SHALL
run that flow only when stdin is a TTY. In a non-interactive context it SHALL
raise `GmailAuthError`, map to exit 2, and direct the operator to run
`fieldkit gmail sync` in an interactive terminal. The interactive flow SHALL
print an authorization URL for deliberate manual opening rather than launching a
browser automatically (`open_browser=False`).

#### Scenario: Scheduled sync needs interactive consent

- **GIVEN** Gmail sync requires a new OAuth consent flow
- **AND** stdin is not a TTY, as in a systemd service or cron job
- **WHEN** the command attempts to authenticate
- **THEN** it exits 2 without opening a browser or waiting for unattended input
- **AND** its error directs the operator to run the command interactively

#### Scenario: Interactive recovery prints a consent URL

- **GIVEN** Gmail sync requires a new OAuth consent flow
- **AND** stdin is a TTY
- **WHEN** the operator runs `fieldkit gmail sync`
- **THEN** the command prints an authorization URL without launching a browser
  automatically
- **AND** successful consent caches credentials for later unattended runs

### Requirement: Scheduled authentication recovery is operator-actionable

The operations documentation SHALL explain how an operator-created Gmail sync
systemd service exposes exit 2 through `systemctl` and the journal, how to
recover by running sync interactively, and why its service SHOULD use
`Restart=no` for authentication failures that require user action. It SHALL NOT
claim that fieldkit ships or installs a Gmail sync unit.

#### Scenario: Operator diagnoses a failed scheduled sync

- **GIVEN** an operator-created Gmail sync service failed because consent was
  required
- **WHEN** the operator follows the runbook
- **THEN** the documented status and journal commands reveal the exit-2 and
  non-TTY failure
- **AND** the recovery runs sync interactively before retrying the service
