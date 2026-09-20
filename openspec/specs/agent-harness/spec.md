# Agent Harness Specification

## Purpose

Define how the project harness routes work across model tiers, resolves design
contracts before execution, and preserves evidence and protected workflow
boundaries through delegation and interruption.

## Requirements

### Requirement: Risk-routed delegation
The project harness SHALL preserve the operator-selected parent model and route
bounded execution to a lower-cost configured model, with a separately configured
frontier judgment role where available.

#### Scenario: Routine work
- **WHEN** a task has a settled contract and bounded independent execution
- **THEN** the coordinator delegates a scoped evidence packet to the execution tier
- **AND** does not duplicate the same investigation in the parent

#### Scenario: Frontier unavailable
- **WHEN** the active client cannot resolve the requested frontier model
- **THEN** the coordinator reports the capability gap and required route explicitly
- **AND** does not silently claim a frontier review occurred

### Requirement: Design before implementation
Design-bearing work SHALL resolve its contract and concrete failure scenarios
before implementation or agent-ready admission.

#### Scenario: Ambiguous work order
- **WHEN** an executor finds contradictory acceptance criteria or scope
- **THEN** it stops the affected work and returns evidence to the coordinator
- **AND** it does not choose a conservative interpretation and push a PR

### Requirement: Evidence and protected boundaries
Delegation and pause records SHALL identify ownership, current revision, completed
checks, remaining gates, and exact next action without weakening protected rules.

#### Scenario: Passing subset of quality checks
- **WHEN** a subset passes but the mandatory full quality gate has not passed
- **THEN** the worker reports incomplete verification and does not push as ready

#### Scenario: Resume after interruption
- **WHEN** a coordinator resumes a checkpoint
- **THEN** it revalidates branch, worktree, issue and PR state before mutation
- **AND** preserves paused work and counts it under the existing WIP policy

### Requirement: Coherent development guidance
Project guidance SHALL have one verification cadence, distinguish bounded checks
from full enforcement, and preserve all protected gates. Discovery SHOULD be
driven by uncertainty rather than required before familiar local operations.

#### Scenario: Coherent edit
- **GIVEN** a developer is making several intermediate edits for one change
- **WHEN** the change is ready for feedback
- **THEN** the agent runs the applicable changed-file checks and targeted tests
- **AND** reports bounded success separately from mandatory full enforcement

#### Scenario: Retrieval unavailable
- **GIVEN** a familiar repository task and an unavailable knowledge service
- **WHEN** the agent has sufficient local evidence to proceed
- **THEN** it continues with local files and tool help without treating the outage as absence of prior decisions

#### Scenario: Removed duplicate skill
- **GIVEN** a fresh session in the updated project
- **WHEN** it follows the development entry points
- **THEN** it reaches the canonical verification guide without being routed to the retired project skill

### Requirement: Explicit client readiness
The coordinator SHALL distinguish configuration on disk from loaded client state
and actual inference. Frontier transfer SHALL preserve the required review tier
and revision without silently substituting an unavailable model.

#### Scenario: Session started in an older checkout
- **GIVEN** the active client started outside the intended updated worktree
- **WHEN** the coordinator finds newer project settings on disk elsewhere
- **THEN** it requests a fresh client rooted in the intended worktree and does not claim that reading files reconfigured the current session

#### Scenario: Frontier transfer
- **GIVEN** the active client cannot perform the required frontier review
- **WHEN** a capable client receives the review
- **THEN** the packet identifies the required tier, immutable revision, scope and acceptance checks
- **AND** the return identifies the resolved model and actual review result before the coordinator accepts it
