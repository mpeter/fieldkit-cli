---
status: Accepted
applies_to: fieldkit-cli
---

# Thin command adapters

## Context

Command modules need a consistent boundary between CLI syntax and product
behavior.

## Decision

Keep commands responsible for parsing input, selecting domain operations,
rendering output, and passing failures or exit status to the shared CLI
boundary. Reusable collection, decisions, and rendering belong under
`fieldkit` domain packages, with imports flowing from commands to domains.
For example, the brief and pipeline command adapters call `fieldkit.brief` and
`fieldkit.pipeline` implementations.

## Consequences

Hooks, tests, and future commands can reuse one implementation, while shared
CLI handling owns the canonical error and status mapping. This is a contributor
boundary for new and refactored code, not a claim that every existing command
already meets it. See the [exit-code contract](../reference/exit-codes.md) for
the process and CLI boundaries.
