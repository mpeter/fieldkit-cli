# driver-independent-done-checks Specification

## Purpose
Define the current behavioral contract for driver-independent-done-checks, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: trusted structured completion contract

The driver MUST snapshot and validate versioned structured argv checks and referenced
checker blobs from the trusted base before agent execution, with no post-agent or shared-
checkout fallback.

The single version authority MUST be the `done_checks` frontmatter object. Expected exit
MUST default to 0, optional expected stdout MUST match exactly, and resource budgets MUST
come from driver-owned command policies rather than author input. The contract MUST
contain 1–64 checks and reject unknown fields, duplicate IDs, invalid types, and the
design's explicit size-limit violations. All required predicates and executable quality
gates MUST map to structured check IDs; migration MUST preserve count and absence semantics.

#### Scenario: candidate changes its work order
- **WHEN** the candidate edits or deletes its work order or checker
- **THEN** verification uses the unchanged hashes snapshotted from the trusted base

### Requirement: shell-free bounded execution

The verifier MUST reject shell/eval semantics and run accepted checks with explicit argv,
bounded time and output, and process-group cleanup.

#### Scenario: structured check contains a wrapper around inline evaluation
- **WHEN** validation sees a shell entrypoint, interpreter inline-evaluation flag, or equivalent wrapper
- **THEN** validation fails before agent launch and executes nothing

#### Scenario: a check times out with descendants
- **WHEN** a check exceeds its deadline
- **THEN** the verifier terminates and reaps its process group within the cleanup bound and records failure

#### Scenario: an approved make target takes more than 90 seconds
- **WHEN** `make quality`, `make quality-full`, or `make gazepy` remains within the cumulative verification deadline
- **THEN** it MUST retain its existing internal stage guards and MUST NOT receive a generic 90-second outer cap

#### Scenario: cumulative service time is consumed
- **WHEN** admission, agent, setup, and verification approach 6600 seconds from the shared driver-run start
- **THEN** no further checks launch, active checks stop at the deadline, and verification fails explicitly while reserving the final 600 seconds of the existing 7200-second allowance for bounded finalization and cleanup

#### Scenario: wrapper leaf shadows a trusted executable
- **WHEN** a candidate supplies a same-name tool in PATH or its `.venv`
- **THEN** the verifier MUST execute the recorded trusted outer and normalized leaf executable identities, never resolve the uv/uvx leaf from candidate state

#### Scenario: captured output exceeds its allowance
- **WHEN** combined streams exceed 8 MiB for one check or 64 MiB for the attempt
- **THEN** the verifier stops the process group, records incomplete-output byte counts/hashes, and fails without storing raw output

### Requirement: frozen verification authority

Before candidate commands execute, the verifier MUST compare the explicit trusted-base
authority manifest in the design with candidate Git blobs and modes. Changes, additions,
or deletions MUST produce `authority_changed`, prevent execution, and deny success.
Application and test code outside that bounded manifest remain candidate inputs; no
hostile-code sandbox guarantee is made.

#### Scenario: a candidate weakens the quality recipe
- **WHEN** the PR changes the trusted Makefile or another manifested authority
- **THEN** no completion command executes and independent verification cannot report success

### Requirement: exact submitted head

The verifier MUST assess a clean detached worktree at the resolved PR head and MUST recheck
full PR identity after verifier cleanup and before terminal evidence/success. After every
check it MUST require unchanged HEAD/tracked content and no unexpected nonignored
untracked files, so check-produced source edits cannot stand in for the submitted commit.

It MUST also require the configured repository, base `main`, exact driver-created head
branch, exactly one open PR, and a full commit SHA.

#### Scenario: PR head moves during verification
- **WHEN** the final head SHA differs from the initially verified SHA
- **THEN** the attempt fails and cannot transition the issue to success

### Requirement: durable privacy-bounded evidence

The verifier MUST atomically persist mode-0600 evidence in a mode-0700 directory containing identifiers, hashes,
statuses, exits, durations, and output metadata without raw output or PII.
Files MUST be private before any bytes are written. Checks-complete evidence MUST precede
verifier-worktree/artifact cleanup; terminal evidence MUST include its cleanup outcome and
precede local/GitHub success finalization. Atomic replacement MUST include file and
directory fsync.

#### Scenario: evidence cannot be persisted
- **WHEN** the evidence write, fsync, permission, or replace step fails
- **THEN** verification fails before success transition, and bounded resource cleanup is still attempted

#### Scenario: verifier cleanup fails after checks pass
- **WHEN** checks-complete evidence is durable but verifier-worktree or artifact cleanup fails
- **THEN** terminal evidence MUST record failed cleanup and neither local nor GitHub success may finalize

### Requirement: fail-closed driver finalization

The driver MUST require passing independent verification and durable evidence before local
success finalization and GitHub success transition, while preserving retry decisions and
typed authentication propagation.

#### Scenario: a completion check fails
- **WHEN** the agent exits zero and pushes a PR but an independent check fails
- **THEN** the existing retryable-or-exhausted path runs and success is not reported

#### Scenario: authentication fails at a driver boundary
- **WHEN** an `AuthError` subclass is raised
- **THEN** local failure evidence and unsuccessful reserved-attempt finalization are attempted without GitHub projection, and the typed error reaches `cli_main()` without being reclassified as an ordinary child exit

#### Scenario: driver is a dry run
- **WHEN** the operator selects driver dry-run
- **THEN** neither the agent nor completion checks execute

### Requirement: authoring and migration are serial

PR1 MUST establish and validate the structured contract before PR2 executes it. Migration
MUST re-enumerate live work orders after its prerequisite merge and MUST NOT activate documents that
lack `issues:` frontmatter.
PR1 MUST update the applicable execution-agent instructions and their maintained mirrors;
PR2 owns the typed trusted snapshot and its creation API. Neither implementation work
order is driver-activated before its manually reviewed implementation is merged.

#### Scenario: migration inventory is collected
- **WHEN** PR1 begins after its prerequisite merges
- **THEN** it records an explicit live inventory and validates every member before PR2 starts
