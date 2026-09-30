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

#### Scenario: Pull-request listing reaches its retrieval limit

- **GIVEN** the open pull-request listing reaches the configured retrieval limit
- **WHEN** completeness of the busy set cannot be established
- **THEN** no issue MUST execute in that tick
- **AND** the diagnostic MUST identify the potentially truncated listing

#### Scenario: A file entry is malformed

- **GIVEN** a pull-request file entry is not an object with a nonempty path
- **WHEN** the driver reads either the initial listing or paginated file results
- **THEN** it MUST reject the busy set rather than silently omit that entry
- **AND** no issue MUST execute in that tick

#### Scenario: Paginated file observations are incomplete or inconsistent

- **GIVEN** a pull request requires paginated file retrieval
- **WHEN** the response reaches the API file limit or omits a path already observed
- **THEN** the driver MUST reject the uncertain busy set
- **AND** no issue MUST execute in that tick

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
oldest-first by issue number. Every admitted prompt MUST declare nonempty
`covers` authority for its edit sites. WorkOrder frontmatter and OpenSpec/Speckit
`driver.yaml` use the same contract; the prompt format alone MUST NOT make its
covers universal. A prompt with missing or invalid covers MUST fail admission
before execution rather than run with inferred unrestricted authority.

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

#### Scenario: OpenSpec prompts declare disjoint authority

- **GIVEN** two OpenSpec prompts have validated, pairwise-disjoint covers in their frozen `driver.yaml` contracts
- **AND** both are otherwise eligible and the concurrency limit is 2
- **WHEN** the driver selects work for the tick
- **THEN** both MAY be selected without treating their format as covering all files

#### Scenario: A prompt omits covers authority

- **GIVEN** a prompt declares edit sites but omits nonempty covers
- **WHEN** the driver validates the frozen contract
- **THEN** it MUST reject the prompt before agent launch
- **AND** it MUST NOT infer unrestricted execution authority

### Requirement: The driver MUST validate structured edit sites at the frozen execution revision

Each WorkOrder prompt MUST carry a version-1 `edit_sites` object in its
frontmatter. OpenSpec and Speckit prompt directories MUST carry edit-site and
done-check authority in `driver.yaml`. Every edit-site entry MUST name a safe candidate-relative path and an
exact, nonempty anchor. The driver MUST freeze `origin/main`, read scheduling
fields and target blobs from that commit, prove each anchor occurs exactly once,
and create the execution worktree from the same commit. It MUST reject missing,
ambiguous, unsafe, malformed, or unavailable input before launching an agent.
Line numbers and Markdown fences are prose only and MUST NOT become inferred
execution authority.

#### Scenario: Drifted line number with a unique snippet

- **GIVEN** a prompt's prose cites an old line number
- **AND** its structured anchor occurs exactly once at a different line in the frozen revision
- **WHEN** the driver validates the prompt
- **THEN** it MUST admit the prompt without using the stale line number

#### Scenario: Snippet missing

- **GIVEN** a structured anchor no longer exists in its target
- **WHEN** the driver validates the prompt
- **THEN** it MUST not launch the execution agent
- **AND** it MUST record a non-passing reason naming the relative path

#### Scenario: Ambiguous snippet

- **GIVEN** a structured anchor appears twice in its target
- **WHEN** the driver validates the prompt
- **THEN** it MUST not launch the execution agent
- **AND** it MUST record a non-passing reason naming the relative path

### Requirement: Driver execution MUST use a packaged portable instruction source

The driver MUST construct the unattended OpenCode invocation from the executor
instructions shipped inside the fieldkit package. It MUST use OpenCode's
built-in build agent in plugin-free mode and MUST NOT depend on a repository-local
agent definition excluded from the public tree.

#### Scenario: Fresh checkout has no local agent configuration

- **GIVEN** fieldkit is installed from a public artifact
- **AND** the checkout contains no `.claude` or `.opencode` agent definition
- **WHEN** the driver launches an eligible prompt
- **THEN** the invocation MUST use the packaged executor instructions
- **AND** it MUST explicitly select the built-in build agent

### Requirement: Blocked and uncertain ticks MUST remain visibly non-passing

The driver MUST expose every blocked or uncertain nonempty tick as non-passing.
An empty queue MAY exit successfully. A source-binding failure, invalid prompt,
spend denial, busy-set failure, unresolved dependency, or all-blocked queue MUST
expose its reason in human and JSON output and MUST return a nonzero exit status.
Retryable scheduling failures MUST return partial/retryable status; authentication
failures MUST propagate to the canonical authentication exit status (`2`), rather
than being converted into a retryable scheduling result. Each candidate skipped by scheduling MUST receive its own persisted
status record.

#### Scenario: All candidates are blocked

- **GIVEN** agent-ready issues exist but none is eligible
- **WHEN** `fieldkit driver run` completes
- **THEN** output MUST name the blocking reason
- **AND** the command MUST return partial/retryable status
