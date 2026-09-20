# web-alert-events Specification

## Purpose
Define the current behavioral contract for web-alert-events, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Alert snapshot filesystem work does not block the event loop

The web alert SSE stream SHALL execute every `DataSource.alert_mtimes()` call in
a worker thread rather than directly on the asyncio event loop. This applies to
both the initial snapshot and each snapshot taken after a polling sleep.

#### Scenario: Initial snapshot leaves the event loop responsive

- **GIVEN** the initial alert snapshot is blocked on filesystem work
- **WHEN** another coroutine is ready on the same event loop
- **THEN** that coroutine runs before the snapshot finishes
- **AND** the stream emits its `hello` frame after the snapshot result arrives

#### Scenario: Polling snapshot leaves the event loop responsive

- **GIVEN** the stream has emitted its `hello` frame and a later alert snapshot
  is blocked on filesystem work
- **WHEN** another coroutine is ready on the same event loop
- **THEN** that coroutine runs before the polling snapshot finishes
- **AND** the resulting alert or heartbeat frame is emitted after the snapshot
  result arrives

### Requirement: Offloading preserves the SSE contract

Moving snapshot work off the event loop SHALL NOT change stream frame content,
frame ordering, mtime-based change detection, poll timing, the `max_polls`
limit, or snapshot exception propagation.

#### Scenario: Existing frames retain their order and payloads

- **GIVEN** an alert file appears or changes between snapshots
- **WHEN** the stream polls the data source
- **THEN** the stream emits the initial `hello` frame followed by sorted `alert`
  frames with the existing name and mtime payload
- **AND** quiet polls continue to emit the existing ping heartbeat

#### Scenario: Snapshot errors remain visible

- **GIVEN** `alert_mtimes()` raises while taking an initial or polling snapshot
- **WHEN** the stream awaits that worker result
- **THEN** the same exception propagates to the stream consumer
- **AND** the stream does not retry or translate the failure
