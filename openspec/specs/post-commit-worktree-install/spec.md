# post-commit-worktree-install Specification

## Purpose
Define the current behavioral contract for post-commit-worktree-install, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Invoking worktree resolution

The post-commit hook SHALL obtain the invoking checkout root and committed revision from Git's current hook execution context. It SHALL NOT derive the checkout from the hook program's symlink target.

#### Scenario: Linked worktree commit

- **GIVEN** the shared hook source resolves into the primary checkout
- **AND** Git reports a distinct linked-worktree top level and its `HEAD`
- **WHEN** package inputs changed
- **THEN** the install subprocess uses the linked-worktree top level as its working directory

#### Scenario: Primary checkout commit

- **GIVEN** Git reports the primary checkout top level
- **WHEN** package inputs changed
- **THEN** the install subprocess uses that primary checkout as its working directory

### Requirement: Root resolution fails closed

If Git cannot return one valid checkout root and revision, the hook SHALL emit an actionable warning, SHALL skip checkout-local synchronization and installation, and SHALL return zero so the commit is not blocked. It SHALL NOT fall back to the hook source checkout.

#### Scenario: Malformed Git context

- **WHEN** Git root discovery fails or returns malformed output
- **THEN** no install or sync subprocess runs
- **AND** stderr explains that manual installation is required
- **AND** the hook exits zero

### Requirement: Shared checkout root

The hook SHALL use the resolved invoking worktree root for commit diff inspection, agent-surface synchronization, sync-script lookup, and package installation.

#### Scenario: Agent surface changes in a linked worktree

- **GIVEN** `.opencode/agents/` or `.opencode/commands/` changed in a linked worktree
- **WHEN** the hook synchronizes `.claude/`
- **THEN** it loads and runs the sync script from that linked worktree

### Requirement: Install provenance

After a successful package reinstall, the hook SHALL identify the invoking worktree's abbreviated HEAD revision and SHALL state that uncommitted package changes, if any, were included.

#### Scenario: Successful reinstall

- **WHEN** `uv tool install` succeeds for the resolved worktree
- **THEN** stdout includes the revision Git reported for that invocation
- **AND** stdout does not claim the installed bytes came exclusively from that commit

### Requirement: Existing executable replacement

The automated hook and manual Makefile install command SHALL explicitly permit uv to replace the existing managed `fieldkit` executable.

#### Scenario: fieldkit is already installed

- **GIVEN** a prior uv-managed `fieldkit` executable exists
- **WHEN** either installation path reinstalls a new checkout
- **THEN** uv replaces the executable and exits successfully
