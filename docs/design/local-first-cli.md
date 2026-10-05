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

## Architecture enforcement

Tach enforces module boundaries within the wheel's `src/fieldkit` package.
Its source roots are `src` and `tests`. These roots must not overlap because
Tach maps files against the first matching root.

Checkout-only `hooks/` and `scripts/` are outside that module graph. The hooks
still consume domain behavior rather than owning it.
`uv run python scripts/check_hook_boundaries.py` preserves their original
allowlist (`fieldkit`, `fieldkit.config`, `fieldkit.enrich`, `fieldkit.errors`),
resolves imports against the most specific Tach module, and rejects package
imports of checkout hooks. Local PR/full gates and hosted lint run this check. Their tests and the script
tests run independently of Tach on every code PR, both locally and in hosted
`Test (pytest)`. The complete enforcement gate still runs the entire test suite.
Selection follows helper package re-exports and tool-backed fixtures in
`conftest.py` and registered local pytest plugins, including nested plugin
registrations. Autouse tool fixtures retain all tests below their defining
conftest directory; registered plugin autouse fixtures apply across the suite.
Tool-backed pytest hooks retain tests in the same applicable scope, including
collection-time parameter generation and marker-controlled tool calls. The
selection engine's own lifecycle hooks do not imply a dependency on other tools.
Run those tests alone with
`uv run pytest tests/ --repo-tools-only -p no:tach -q -n 0`.

## Contributor action

Before changing a boundary, read the relevant decision record in
[`docs/adr`](../adr/0001-local-first-roots.md) and update this guide or that
record when its preconditions, invariants, or trade-offs change.
