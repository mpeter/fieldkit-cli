# driver-retry-throttle Specification

## Purpose

Define durable, host-local retry accounting for autonomous driver issue execution when GitHub lifecycle mutations are unavailable.

## Requirements

### Requirement: Durable local attempt reservation

The driver MUST persist a versioned, per-repository-and-issue retry reservation
under the fieldkit data root before it creates a worktree or starts OpenCode. The
reservation MUST use an atomic, multi-writer-safe update primitive. An issue MUST
NOT execute when its local started-attempt count has reached `MAX_ATTEMPTS`.

#### Scenario: GitHub mutation outage cannot exceed the attempt cap
- **GIVEN** GitHub issue reads succeed but all label and comment mutations fail
- **WHEN** the driver repeatedly evaluates the same `agent-failed` issue
- **THEN** it SHALL start no more than `MAX_ATTEMPTS` OpenCode executions for that issue

#### Scenario: Reservation persistence failure
- **GIVEN** a candidate issue is eligible and the local retry ledger cannot be persisted
- **WHEN** the driver attempts to reserve its next attempt
- **THEN** it MUST skip worktree creation and OpenCode execution for that issue

### Requirement: Local retry authority and recovery

The first local reservation MUST seed its count from the highest valid GitHub
`attempt:N` label. After that reservation, local state MUST be authoritative for
eligibility and GitHub label changes MUST NOT implicitly increase or reset the
local attempt budget. A surviving `running` reservation MUST be treated as an
already started attempt after a prior driver process ends.

#### Scenario: Partial GitHub label history
- **GIVEN** an issue has `attempt:2` and no local retry entry
- **WHEN** the driver reserves an attempt
- **THEN** it MUST reserve attempt three and MUST NOT permit a fourth attempt

#### Scenario: Interrupted reserved execution
- **GIVEN** a prior driver process persisted a `running` reservation and then terminated
- **WHEN** a later driver process evaluates the issue
- **THEN** it MUST count the reservation against the attempt cap before deciding eligibility

### Requirement: Retry state observability and explicit reset

The driver CLI MUST provide machine-readable retry status for local entries and
an explicit reset operation that requires an audit reason. Reset MUST refuse an
actively running entry and MUST NOT require GitHub mutation access.

#### Scenario: Inspecting local suppression during outage
- **GIVEN** GitHub mutations are unavailable and an issue is locally exhausted
- **WHEN** an operator requests retry status in JSON mode
- **THEN** the command MUST report the issue key, phase, and started-attempt count

#### Scenario: Resetting an exhausted issue
- **GIVEN** an issue is locally exhausted
- **WHEN** an operator resets it with a non-empty reason
- **THEN** the local ledger MUST record the reset audit data and make a future reservation possible

### Requirement: Best-effort GitHub lifecycle projection

After local state finalization, the driver MUST attempt to project the assigned attempt
and outcome to GitHub labels/comments. A projection failure MUST NOT reduce the
local count or make a terminal local entry eligible.

#### Scenario: Failed projection after an exhausted attempt
- **GIVEN** the final local attempt has finished and GitHub label mutation fails
- **WHEN** the next driver tick evaluates the issue
- **THEN** the driver MUST skip the issue based on its local exhausted state
