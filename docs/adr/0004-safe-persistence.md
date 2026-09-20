---
status: Accepted
applies_to: fieldkit-cli
---

# Safe local persistence

## Context

Workspace and runtime state can be changed by commands that fail or overlap.

## Decision

Use the project’s locked update helper for multi-writer JSON, atomic replace for
single-writer files, and the canonical pursuit I/O layer for frontmatter.

## Consequences

State writes are recoverable and consistent, and untrusted text cannot bypass
the frontmatter boundary through ad hoc interpolation.
