---
status: Accepted
applies_to: fieldkit-cli
---

# Verifiable public release

## Context

A public release must not expose private history or rely on an unverifiable
copy of the source tree.

## Decision

Build a deterministic clean export from a classified source tree, bind it to
artifact digests and provenance, and verify the public root independently.

## Consequences

The public repository has a clean history while retainable evidence can prove
the exported content and package artifacts came from the reviewed candidate.
