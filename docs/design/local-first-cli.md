# Local-first CLI design

This guide helps contributors preserve fieldkit's core design while adding a
command, integration, or persisted feature.

## Preconditions

- The base package must work without an external integration installed.
- A command can write only to a configured workspace, runtime-data root, or
  documented fieldkit configuration location.
- A command that crosses a network or changes an external system must have a
  bounded timeout and an explicit user-facing failure mode.

## Invariants

- Commands adapt input and output; domain modules own behavior.
- Configuration, workspace content, and runtime data remain separate roots.
- Optional integrations are imported only when their command is invoked.
- Exit status distinguishes success, retryable failure, required user action,
  and invalid data.
- Public examples use fictional data and never require a credential.

## Design rationale

fieldkit is designed as a local-first command-line tool so that a useful first
workflow is inspectable and offline. Integrations add capability after explicit
installation and configuration; they do not turn the base package into a
network service.

The architecture chose thin command adapters over command-owned domain logic.
That keeps a behavior reusable by hooks and tests, and gives each convention
one authoritative implementation. Persistent roots are intentionally separate
so a checkout, user work, and generated data cannot be confused or committed
together.

## Contributor action

Before changing a boundary, read the relevant decision record in
[`docs/adr`](../adr/0001-local-first-roots.md) and update this guide or that
record when its preconditions, invariants, or trade-offs change.
