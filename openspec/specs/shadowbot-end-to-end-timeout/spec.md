## Purpose

Define the end-to-end timeout behavior for ShadowBot queries.

## Requirements

### Requirement: ShadowBot query timeout is end-to-end

The ShadowBot client MUST treat the configured query timeout as a monotonic
deadline covering thread creation and streaming response consumption.

#### Scenario: thread creation consumes query budget
- **GIVEN** a query timeout and delayed thread creation
- **WHEN** stream creation begins
- **THEN** the client SHALL use only the remaining query budget for the stream

### Requirement: ShadowBot stream deadline handles heartbeats

The client MUST check its deadline while consuming every stream line, including
SSE heartbeat lines that do not produce a response event.

#### Scenario: non-terminal heartbeat stream expires
- **GIVEN** a stream that repeatedly sends heartbeat lines without a final event
- **WHEN** the monotonic query deadline expires
- **THEN** the client SHALL raise a query error rather than wait indefinitely

### Requirement: ShadowBot deadline failure is explicit

On deadline expiry, the client MUST return a non-zero query outcome with an
error that identifies timeout expiry rather than an upstream final response.

#### Scenario: stream exceeds total timeout
- **GIVEN** an open ShadowBot response stream
- **WHEN** its remaining query budget is exhausted
- **THEN** the CLI SHALL report a query failure and exit non-zero
