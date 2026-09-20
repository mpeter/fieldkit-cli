# AgentReady worktree lifecycle

## Purpose

Keep AgentReady full-quality assessment isolated from the caller checkout while
making temporary-worktree lifecycle failures actionable.

## Requirements

### Requirement: AgentReady assessment preserves caller checkout

The AgentReady assessment wrapper MUST create its temporary assessment worktree
without changing the invoking repository's HEAD or checked-out branch, and it
MUST execute the external assessor from that temporary worktree.

#### Scenario: successful assessment
- **GIVEN** a repository checked out on a named feature branch
- **WHEN** the AgentReady assessment succeeds
- **THEN** the repository SHALL remain on the original branch and the external
  assessor SHALL receive the temporary worktree as its working directory

### Requirement: AgentReady assessment cleans up temporary worktrees

The wrapper MUST attempt to remove every successfully created temporary
worktree after assessment completes, including when assessment fails.

#### Scenario: failed assessment
- **GIVEN** a temporary assessment worktree was created
- **WHEN** the external assessment returns a non-zero exit status
- **THEN** the wrapper SHALL attempt worktree removal before returning failure

### Requirement: AgentReady cleanup failure is observable

The wrapper MUST report a failed worktree-removal command with the temporary
path and command failure context, and MUST return a non-zero result.

#### Scenario: removal failure
- **GIVEN** an assessment worktree was created
- **WHEN** `git worktree remove --force` fails
- **THEN** the wrapper SHALL emit a diagnostic naming the temporary worktree
  and SHALL fail rather than silently leaving it behind
