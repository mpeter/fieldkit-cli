---
status: Accepted
applies_to: fieldkit-cli
---

# Thin command adapters

## Context

Command modules need a consistent boundary between CLI syntax and product
behavior.

## Decision

Commands parse input, call domain behavior, render output, and return its exit
status. Shared behavior belongs in the domain rather than a command adapter.

## Consequences

Hooks, tests, and future commands reuse one implementation, and error handling
has a single authoritative mapping.
