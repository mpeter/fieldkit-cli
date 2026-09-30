# Local-first CLI design

This guide helps contributors preserve fieldkit's core design while adding a
command, integration, or persisted feature.

## Preconditions

- The base package must work without an external integration installed.
- A command can write only to a configured workspace, runtime-data root, or
  documented fieldkit configuration or disposable scratch location. The
  [product model](../concepts.md#persistent-roots-and-harness-scratch) describes
  harness worktrees; [local data and privacy](../privacy.md) describes private
  temporary snapshots and their cleanup limits.
- A command that crosses a network or changes an external system must have a
  bounded timeout and an explicit user-facing failure mode.

## Invariants

- The intended architecture puts input and output adaptation in commands and
  behavior in domain modules. This is contributor policy, not proof that every
  existing command already meets it.
- Installed code, workspace content, and runtime data have separate
  responsibilities, resolved through configured roots. Runtime data defaults
  to the workspace's `data` directory; physical separation is optional. See the
  [product model](../concepts.md#persistent-roots-and-harness-scratch).
- Optional integrations are imported only when their command is invoked.
- Pipeline collection and rendering belong to `fieldkit.pipeline`; brief
  collection and rendering belong to `fieldkit.brief`. The command adapters
  select options, call those domains, and present reports and failures.
- Exit status distinguishes success, retryable failure, required user action,
  and invalid data; dispatcher interruption exits `130`. The
  [exit-code contract](../reference/exit-codes.md) defines the shared CLI
  boundaries and process-launcher behavior.
- Public examples use fictional data and never embed credentials. Offline
  first-success examples require no credentials. Integration examples state
  their authorization prerequisites and retain credentialed verification
  ownership in the documentation contract.

## Design rationale

fieldkit is designed as a local-first command-line tool so that a useful first
workflow is inspectable and offline. Integrations add capability after explicit
installation and configuration; they do not turn the base package into a
network service.

The architecture chose thin command adapters over command-owned domain logic.
That keeps a behavior reusable by hooks and tests, and gives each convention
one authoritative implementation. The [roots decision](../adr/0001-local-first-roots.md)
separates content responsibilities so a checkout, user work, and generated data
can be handled according to their ownership and persistence needs. Contributors
must follow the configured locations rather than assume disjoint directories.

## Contributor action

Before changing a boundary, read the relevant decision record in
[decision records](../adr/0001-local-first-roots.md) and update this guide or that
record when its preconditions, invariants, or trade-offs change.
