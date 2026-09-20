# linked-worktree-hook-install Specification

## Purpose
Define the current behavioral contract for linked-worktree-hook-install, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Git-resolved hook destination

`make hooks` SHALL resolve the hooks directory through Git and SHALL use the resolved absolute directory for copied custom pre-push and post-commit hooks.

#### Scenario: Linked worktree installation

- **GIVEN** the command runs from a linked worktree whose `.git` is a pointer file
- **WHEN** the hook installation recipe is evaluated
- **THEN** the direct copy and permission-update destinations use the shared Git hooks directory
- **AND** no destination is derived by appending `hooks` to the linked worktree's `.git` path
- **AND** a legacy symlink is replaced without modifying its target
- **AND** the installed post-commit hook remains usable after the linked worktree is removed
- **AND** installed hook files have executable mode `0755`

#### Scenario: Custom hook preparation fails

- **GIVEN** an existing custom hook is installed
- **WHEN** preparing its successor fails
- **THEN** `make hooks` exits nonzero without claiming success
- **AND** the existing hook remains unchanged
- **AND** the temporary file is removed

#### Scenario: Custom hook replacement fails

- **GIVEN** prepared custom hook files
- **WHEN** atomically publishing a destination fails
- **THEN** `make hooks` exits nonzero without claiming success
- **AND** all temporary files are removed
- **AND** a pre-push publication is rolled back if post-commit publication fails
- **AND** the complete prior hook set is restored

#### Scenario: Rollback restoration fails

- **GIVEN** a prior hook was captured before mutation
- **WHEN** installation fails and that hook cannot be restored
- **THEN** every unrestored recovery backup remains in the shared hooks directory
- **AND** the failure identifies the original installation failure, every cleanup failure, and every preserved backup

#### Scenario: Cleanup fails after validated publication

- **GIVEN** the complete successor hook set was published and validated
- **WHEN** removing a staging artifact or obsolete backup fails
- **THEN** the command exits nonzero and reports every cleanup failure
- **AND** the complete validated successor hook set remains installed

#### Scenario: Termination arrives during final cleanup

- **GIVEN** hook installation has entered final backup and staging cleanup
- **WHEN** the process receives `SIGHUP` or `SIGTERM`
- **THEN** every registered cleanup is attempted before signal delivery
- **AND** any prior installation failure remains the reported failure with the termination request attached

#### Scenario: Termination arrives during temporary-file allocation

- **GIVEN** the installer has allocated a staged file or recovery backup
- **WHEN** the process receives `SIGHUP` or `SIGTERM` before cleanup ownership would otherwise be registered
- **THEN** signal delivery waits until cleanup ownership is registered
- **AND** final cleanup removes the allocated path

#### Scenario: Hook inventory stays consistent

- **GIVEN** the installer supports generated and repository-owned hooks
- **WHEN** it generates, stages, snapshots, publishes, or validates hooks
- **THEN** every phase derives its hook names and source behavior from one manifest

#### Scenario: Primary checkout and retained hook remain runnable

- **GIVEN** hooks are installed from either the primary checkout or a linked worktree
- **WHEN** installation completes and the linked worktree is removed
- **THEN** the complete hook set remains installed in the shared Git directory
- **AND** the retained post-commit hook executes successfully from the primary checkout

#### Scenario: Make command overrides cannot bypass installation

- **GIVEN** an environment or command line defines hook-installer command variables as a successful no-op
- **WHEN** the operator runs `make hooks`
- **THEN** Make invokes the repository-owned Python installer directly
- **AND** success is printed only after that installer succeeds

#### Scenario: Checkout path contains shell syntax

- **GIVEN** the checkout directory name contains literal shell substitution syntax
- **WHEN** the operator runs `make hooks`
- **THEN** the checkout name is not evaluated by the shell
- **AND** the repository-relative installer runs successfully

#### Scenario: Termination accompanies a publication failure

- **GIVEN** hook publication fails and termination is requested before restoration completes
- **WHEN** the installer reports the result
- **THEN** the publication failure remains the primary diagnostic
- **AND** the termination request is included as context

#### Scenario: Termination arrives after the final snapshot

- **GIVEN** the complete prior hook set has been captured but publication has not committed
- **WHEN** the installer records `SIGHUP`, `SIGINT`, or `SIGTERM`
- **THEN** it either avoids publication or restores every hook it published
- **AND** it does not exit nonzero with an uncommitted successor set installed

#### Scenario: External hook replaces an absent-destination rollback placeholder

- **GIVEN** rollback has atomically displaced a newly published hook with a temporary placeholder
- **WHEN** another process replaces that placeholder before removal
- **THEN** rollback preserves the external replacement at the live destination
- **AND** it reports the conflicting update instead of deleting it

#### Scenario: Concurrent installation

- **GIVEN** multiple worktrees invoke `make hooks` concurrently
- **WHEN** they install into the shared hooks directory
- **THEN** the custom-hook publication transactions are serialized
- **AND** lock waits and installer subprocesses have bounded timeouts

#### Scenario: Hooks directory changes during creation

- **GIVEN** the shared hooks directory is absent before installation
- **WHEN** another process replaces the newly created path with a symlink
- **THEN** the installer rejects the path before creating the lock or hook files
- **AND** no file is written through the symlink target

#### Scenario: Hooks directory permissions permit replacement

- **GIVEN** the shared hooks directory is absent or is group/world writable
- **WHEN** the operator runs `make hooks`
- **THEN** a new directory is created with mode `0700`
- **AND** an existing group/world-writable directory is rejected before hook publication

#### Scenario: Hooks directory changes after validation

- **GIVEN** the validated shared hooks directory is renamed and replaced with a symlink during installation
- **WHEN** staging or publication continues
- **THEN** mutations remain anchored to the validated directory inode
- **AND** the symlink target remains unchanged
- **AND** the command exits nonzero and restores the prior hook set

#### Scenario: Termination arrives during process acquisition

- **GIVEN** an installer subprocess has started but `Popen` has not returned control to the caller
- **WHEN** the process receives `SIGHUP` or `SIGTERM`
- **THEN** delivery waits until the child is tracked
- **AND** the complete child process group is terminated and reaped

#### Scenario: External hook update races with rollback

- **GIVEN** this installation has snapshotted and published a hook
- **WHEN** another process replaces that destination before rollback
- **THEN** rollback does not overwrite the external replacement
- **AND** the prior recovery backup remains available and the conflict is reported

#### Scenario: External hook update races with publication

- **GIVEN** this installation has snapshotted a hook
- **WHEN** another process replaces that destination before publication
- **THEN** publication aborts without overwriting the external replacement
- **AND** any earlier publications are rolled back

#### Scenario: Installation stops during publication

- **GIVEN** a complete existing hook set and a complete staged successor set
- **WHEN** the installer stops between per-hook atomic publications
- **THEN** every destination still contains either its complete prior hook or its complete successor
- **AND** publication does not create an absent or partially written live hook

#### Scenario: Termination follows a publication failure

- **GIVEN** some successor hooks were published and a later publication failed
- **WHEN** `SIGHUP` or `SIGTERM` arrives before restoration begins
- **THEN** signal delivery is deferred until every prior hook restoration is attempted
- **AND** the command exits nonzero after rollback and cleanup

#### Scenario: Installation is interrupted

- **GIVEN** an installer subprocess is running in its isolated process group
- **WHEN** the parent installer receives an interrupt, `SIGHUP`, or `SIGTERM`
- **THEN** it terminates and reaps the process group within bounded waits before releasing the transaction lock
- **AND** it propagates the interruption

#### Scenario: Rollback is interrupted

- **GIVEN** multiple hooks require restoration
- **WHEN** one restoration is interrupted
- **THEN** the installer attempts every remaining restoration
- **AND** it preserves every unrestored backup and reports the interruption

#### Scenario: Supported operating system

- **GIVEN** a supported Linux or macOS checkout
- **WHEN** hook installation is requested
- **THEN** the transaction uses Python standard-library filesystem, lock, and subprocess primitives
- **AND** it does not require GNU-only command-line utilities or flags

#### Scenario: Destination is a directory

- **GIVEN** a hook destination is a directory, FIFO, socket, or other special file
- **WHEN** hook installation is requested
- **THEN** the command exits nonzero before mutating installed hooks

#### Scenario: Destination is a symlink

- **GIVEN** an installed hook is a symlink to a path outside the repository
- **WHEN** hook installation is requested
- **THEN** the symlink is removed before a package tool can write the destination
- **AND** the external target remains unchanged

#### Scenario: Invalid custom hook source

- **GIVEN** a custom hook source is empty, exceeds 1 MiB, or is not a regular file
- **WHEN** hook installation is requested
- **THEN** the command exits nonzero before mutating installed hooks

#### Scenario: Custom hook source changes during staging

- **GIVEN** a custom hook source changes in place while it is being read
- **WHEN** hook installation is requested
- **THEN** the command exits nonzero before mutating installed hooks

#### Scenario: Package tool reports ineffective success

- **GIVEN** a package tool exits zero without creating a required hook
- **WHEN** installation reaches final validation
- **THEN** the command exits nonzero and restores the complete prior hook set
- **AND** it does not print a success claim

#### Scenario: Existing hook ownership

- **GIVEN** an existing hook destination is not owned by the current user
- **WHEN** hook installation is requested
- **THEN** the command exits nonzero before mutation so rollback cannot change ownership

#### Scenario: Inherited Git repository selection

- **GIVEN** the caller's environment contains Git variables selecting another repository
- **WHEN** hook installation is requested from the intended checkout
- **THEN** repository resolution and installer subprocesses ignore those inherited selectors
- **AND** hooks are installed only in the intended repository's shared hooks directory

#### Scenario: Inherited Git configuration selection

- **GIVEN** the caller's environment contains Git variables that replace or inject configuration
- **WHEN** hook installation checks persistent hook routing
- **THEN** those inherited selectors cannot hide a configured `core.hooksPath`
- **AND** the command rejects unsupported routing before mutation

#### Scenario: Primary checkout installation

- **GIVEN** the command runs from the primary checkout
- **WHEN** the hook installation recipe is evaluated
- **THEN** the same Git-resolved shared hooks directory is used

#### Scenario: Custom hooks path

- **GIVEN** the repository configures `core.hooksPath` outside its Git directory
- **WHEN** hook installation is requested
- **THEN** the command exits nonzero before installing hooks
- **AND** it explains that pre-commit does not support the configuration
- **AND** it does not print a success claim
- **AND** sibling worktrees with their own worktree-scoped hook path remain outside this invocation's installation scope

### Requirement: Resolution failure

If Git cannot resolve the hooks directory, `make hooks` SHALL fail and SHALL NOT print its success messages.

#### Scenario: Not inside a Git repository

- **GIVEN** the recipe runs where Git cannot resolve its hooks path
- **WHEN** hook installation is requested
- **THEN** the command exits nonzero before installing or claiming success

#### Scenario: Git resolution times out or is interrupted

- **GIVEN** a Git query or one of its descendants does not exit
- **WHEN** the resolution timeout expires or the installer is interrupted
- **THEN** the installer terminates and reaps the Git query process group within bounded waits
- **AND** it exits without installing or claiming success

#### Scenario: Git lacks path-format support

- **GIVEN** a supported host whose Git provides `--git-common-dir` but not `--path-format`
- **WHEN** hook installation resolves a primary or linked worktree
- **THEN** it resolves the shared hooks directory without using `--path-format`
