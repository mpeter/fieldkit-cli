# watcher-logging Specification

## Purpose
Define the current behavioral contract for watcher-logging, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Watcher files capture the watcher namespace

Each watcher module SHALL emit through a logger descended from `fieldkit.watch`. A watcher logging session SHALL capture DEBUG-and-higher records from that namespace in its per-run file for both direct domain calls and CLI-dispatched calls.

#### Scenario: direct watcher call is captured

- **GIVEN** a watcher logging session is active
- **WHEN** a watcher domain module emits an INFO record during a direct call
- **THEN** the session's file contains that record with the existing timestamp, level, logger name, and message format

#### Scenario: unrelated process record is excluded

- **GIVEN** a watcher logging session is active
- **WHEN** a logger outside `fieldkit.watch` emits a record
- **THEN** the watcher file does not contain that record

### Requirement: Watcher sessions do not mutate root logging

Setup, active execution, and teardown SHALL leave the process root logger's handlers, handler order, level, and propagation state unchanged. Watcher records SHALL not propagate from the watcher namespace to root terminal handlers while the session is active.

#### Scenario: root handlers remain active for other components

- **GIVEN** the root logger has terminal and file handlers before watcher setup
- **WHEN** a watcher session starts and an unrelated component logs
- **THEN** the root handlers remain installed in their original order and receive the unrelated record

#### Scenario: watcher record does not duplicate to the terminal

- **GIVEN** the root logger has a terminal handler
- **WHEN** a watcher emits an INFO record during an active session
- **THEN** the watcher file receives the record and the root terminal handler does not

### Requirement: Teardown restores watcher namespace configuration

Teardown SHALL remove and close only the matching per-run file handler and SHALL restore the watcher namespace logger's prior level and propagation flag even when watcher execution raises.

#### Scenario: exceptional watcher cleanup restores state

- **GIVEN** the watcher namespace has non-default logging configuration before setup
- **WHEN** the watcher body raises an exception
- **THEN** context-manager cleanup restores that configuration and leaves no per-run handler attached
