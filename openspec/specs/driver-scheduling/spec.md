# driver-scheduling Specification

## Purpose

Define how the driver chooses safe, independent work orders for one execution
tick. A contributor can use this contract to author `covers` and `depends_on`
frontmatter that the driver can schedule without overlapping active changes.

## Requirements

### Requirement: The driver MUST NOT start a work order whose covers intersect in-flight changes

Before executing an `agent-ready` candidate, the driver MUST compute the files
changed by open pull requests targeting `main`. It MUST skip a candidate whose
work-order `covers` set intersects that busy set. A skipped issue MUST retain
its `agent-ready` label and record the blocking path and pull request number.
If the busy set cannot be determined, the driver MUST fail closed and execute
nothing for that tick.

#### Scenario: Candidate blocked by an open pull request

- **GIVEN** a work order covers `src/fieldkit/sf/client.py`
- **AND** an open pull request targeting `main` modifies that file
- **WHEN** the driver tick runs
- **THEN** the work order MUST NOT execute
- **AND** the skip MUST identify the blocking file and pull request

#### Scenario: Busy-set lookup fails

- **GIVEN** the lookup of open pull request files fails
- **WHEN** the driver tick runs
- **THEN** no issue MUST execute
- **AND** the run status MUST record a skipped outcome with the error

### Requirement: The driver MUST honor `depends_on` frontmatter

The driver MUST honor optional `depends_on: [NNNN, ...]` issue numbers in a
work order. It MUST not execute the work order while any listed issue is open.
A nonexistent referenced issue MUST be treated as open and logged.

#### Scenario: Declared dependency remains open

- **GIVEN** a work order declares a dependency in `depends_on`
- **AND** that dependency remains open
- **WHEN** the driver tick runs
- **THEN** the work order MUST be skipped with a reason naming the dependency

#### Scenario: Dependencies are closed

- **GIVEN** every issue declared in `depends_on` is closed
- **AND** no covers conflict exists
- **WHEN** the driver tick runs
- **THEN** the work order MUST be eligible for selection

### Requirement: Concurrent work orders MUST have pairwise-disjoint covers

The driver MUST select only work orders with pairwise-disjoint `covers` sets
within one tick. When `driver.max_concurrent` exceeds one, it MAY execute up to
that many selected issues in separate worktrees. Selection priority MUST remain
oldest-first by issue number. A missing or empty `covers` field, or an
OpenSpec/Speckit-sourced issue, MUST be treated as covering all files and can
run only when no other in-flight or batched work conflicts.

#### Scenario: Disjoint work orders run together

- **GIVEN** `driver.max_concurrent` is 2
- **AND** the two oldest eligible work orders have disjoint covers
- **WHEN** the driver tick runs
- **THEN** both MUST execute in separate worktrees
- **AND** each MUST produce its own `driver-run-status.json` entry

#### Scenario: A later overlapping work order waits

- **GIVEN** three eligible work orders are considered oldest-first
- **AND** the third overlaps the first
- **WHEN** the driver tick runs with capacity for three
- **THEN** the third MUST be skipped with a batch-overlap reason

#### Scenario: The default preserves one-work-order execution

- **GIVEN** `driver.max_concurrent` is 1
- **WHEN** the driver tick runs
- **THEN** it MUST select at most one eligible work order

### Requirement: The work-order execution agent MUST resolve edit sites by quoted snippet, not line number

The work-order execution agent MUST locate each edit site by searching the
target file for the work order's quoted snippet. A cited line number is
advisory; a mismatch between the cited line and the snippet's actual location
MUST NOT halt the run. The execution agent MUST stop and exit with an error
naming the file and snippet when the snippet is absent from the file or matches
more than one location.

#### Scenario: Drifted line number with a unique snippet

- **GIVEN** a work order cites a snippet at line 128
- **AND** the snippet now appears exactly once at line 133
- **WHEN** the work-order execution agent executes the work order
- **THEN** it MUST apply the edit at line 133 and continue

#### Scenario: Snippet missing

- **GIVEN** a work order cites a snippet that no longer exists in the file
- **WHEN** the work-order execution agent executes the work order
- **THEN** it MUST stop and exit with an error naming the file and snippet

#### Scenario: Ambiguous snippet

- **GIVEN** a work order cites a snippet that appears twice in the file
- **WHEN** the work-order execution agent executes the work order
- **THEN** it MUST stop and exit with an error naming the file and snippet
