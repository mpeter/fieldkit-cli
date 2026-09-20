# llm-log-storage Specification

## Purpose
Define the current behavioral contract for llm-log-storage, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Explicit LLM log overrides remain within approved storage

When `FIELDKIT_LLM_LOG` is set, the shared LLM log path resolver SHALL accept only
a non-empty absolute path without surrounding whitespace whose resolved target is equal to or contained by
`get_fieldkit_home()`, `get_fieldkit_data()`, `~/.config/fieldkit`, or
`~/.local/share/fieldkit`. Rejected values SHALL raise `ConfigError` before SQLite
opens or creates a file.

#### Scenario: Override is under the runtime data root

- **GIVEN** `FIELDKIT_LLM_LOG` names an absolute path beneath `get_fieldkit_data()`
- **WHEN** the shared resolver runs
- **THEN** it returns the resolved absolute path
- **AND** both LLM writes and driver spend readers use that path

#### Scenario: Override escapes approved roots

- **GIVEN** the override is empty, invalid, relative, has surrounding whitespace,
  is outside every approved root, uses a sibling prefix collision, or resolves
  through a symlink outside an approved root
- **WHEN** the shared resolver runs
- **THEN** it raises `ConfigError`
- **AND** no database file is created at the rejected target

#### Scenario: Workspace configuration is unavailable

- **GIVEN** workspace and data roots cannot be loaded
- **AND** the override resolves beneath `~/.config/fieldkit` or
  `~/.local/share/fieldkit`
- **WHEN** the shared resolver runs
- **THEN** the stable-root containment check still accepts the path

### Requirement: The unset default remains migration-compatible

When `FIELDKIT_LLM_LOG` is absent, the shared resolver SHALL use
`get_fieldkit_data()/llm-calls.db` for ordinary writes. A reader SHALL fall back
to `~/.local/share/fieldkit/llm-calls.db` only when the new ordinary default is
absent. An explicit override SHALL remain exact and SHALL NOT use fallback.

#### Scenario: Ordinary logging uses the configured data root

- **GIVEN** no explicit override is set
- **WHEN** an LLM call initializes logging
- **THEN** it writes to `get_fieldkit_data()/llm-calls.db`

#### Scenario: A reader sees legacy history before a new database exists

- **GIVEN** no explicit override is set
- **AND** the new ordinary database is absent
- **AND** the legacy database exists
- **WHEN** a read-only consumer resolves history
- **THEN** it reads the legacy database without moving or deleting it

### Requirement: Driver LLM logs are isolated and durable per run

Each real driver execution SHALL reserve a unique database path below
`get_fieldkit_data()/driver/llm-runs/`, provide it to the child as
`FIELDKIT_LLM_LOG`, and provide the same path directly to the parent spend
summary reader. The path SHALL remain outside the disposable worktree.

#### Scenario: Parent reports from the child's durable database

- **GIVEN** a driver child writes tagged LLM calls during an execution
- **WHEN** the child returns and the parent builds its spend summary
- **THEN** both processes use the same per-run database
- **AND** the database survives worktree removal

#### Scenario: Concurrent runs do not share audit databases

- **GIVEN** the driver executes two issues concurrently
- **WHEN** their child environments are built
- **THEN** each receives a distinct durable database path

#### Scenario: Isolated child accepts the parent data root

- **GIVEN** configured `fieldkit_data` is outside `fieldkit_home`
- **AND** a driver child redirects `FIELDKIT_DATA_DIR` into its worktree
- **WHEN** the child validates its parent-generated `FIELDKIT_LLM_LOG`
- **THEN** the configured parent data root remains an approved root
- **AND** the durable per-run path is accepted

### Requirement: Daily driver spend includes all retained run databases

The daily spend reader SHALL sum today's `driver-issue-*` rows across each
distinct ordinary, applicable legacy, and durable per-run database. If any
existing candidate cannot be queried reliably, the result SHALL be unknown so
the spend guard fails closed.

#### Scenario: Spend is distributed across run databases

- **GIVEN** today's tagged rows exist in multiple durable run databases
- **WHEN** the daily cap is evaluated
- **THEN** every distinct database contributes exactly once to the total

#### Scenario: One retained database is malformed

- **GIVEN** at least one existing candidate database cannot be queried
- **WHEN** the daily cap is evaluated
- **THEN** the total is unknown
- **AND** the configured cap denies the next run

#### Scenario: Retained databases cannot be enumerated

- **GIVEN** the durable run directory cannot be scanned
- **WHEN** the daily cap is evaluated
- **THEN** the total is unknown
- **AND** the configured cap denies the next run

#### Scenario: Multiple issues are otherwise runnable below the cap

- **GIVEN** a spend cap is configured and current spend remains below it
- **AND** multiple disjoint issues could run concurrently
- **WHEN** the driver selects work from that single spend measurement
- **THEN** it admits at most one issue
