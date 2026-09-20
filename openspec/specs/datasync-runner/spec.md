# datasync-runner Specification

## Purpose
Define the current behavioral contract for datasync-runner, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Independent watcher steps execute concurrently

During a real `fieldkit sync`, the runner SHALL start backstory-health, pursuit-stalls, and slack-threads without waiting for a preceding watcher to finish. Gmail, people-index, ingest, and optional Salesforce steps SHALL retain their dependency order outside that concurrent phase.

#### Scenario: Watcher critical path is bounded by the slowest sibling

- **GIVEN** all three watcher steps block until released
- **WHEN** the watcher phase runs
- **THEN** all three have started before any is released
- **AND** later dependency phases do not start early

### Requirement: Concurrent results remain deterministic and complete

The runner SHALL wait for every watcher, retain the original step indices and ordering in results and output, and report each sibling's independent outcome.

#### Scenario: One watcher fails while siblings succeed

- **GIVEN** one concurrent watcher exits nonzero
- **WHEN** all watcher futures settle
- **THEN** both successful siblings remain successful in the result list
- **AND** the failed watcher remains failed
- **AND** the aggregate command exits partial code 1

#### Scenario: Verbose output completes out of order

- **GIVEN** concurrent watchers finish in a different order from the step list
- **WHEN** verbose results are rendered
- **THEN** their summaries and bounded stream sections appear in original step order

### Requirement: Dry-run remains an ordered non-concurrent preview

The runner SHALL preserve dry-run as a sequential, non-executing preview of the
ordered commands and SHALL NOT create a worker pool for it.

#### Scenario: Operator previews sync

- **GIVEN** `fieldkit sync --dry-run`
- **WHEN** watcher steps are visited
- **THEN** no executor is created
- **AND** the existing ordered command preview is emitted
