---
status: Accepted
applies_to: fieldkit-cli
---

# Safe local persistence

## Context

Workspace and runtime state can be changed by commands that fail or overlap.

## Decision

Use `locked_json_update()` for cooperating multi-writer JSON, `atomic_yaml_write()`
or a same-directory atomic replacement for single-writer files, and
`fieldkit.pursuit.io` for pursuit frontmatter. Hold a shared target lock over
the entire read, validation, and publication cycle when several writers can
update the same Markdown file.

## Consequences

Readers see one complete version of an individually replaced file. A lock only
serializes writers that cooperate on the same lock. Frontmatter writes use the
canonical I/O layer rather than interpolating untrusted text into YAML.

## Scope and current implementation

These guarantees apply to cooperating writers using the owning persistence
helper. Atomic replacement protects one file, not a group of files or a combined
database/filesystem transaction. Do not infer power-loss durability or protection
against a hostile same-user process renaming directories during a write.

For shared Markdown, use a bounded target-specific lock across the complete
operation. The lock-path helper puts hashed lock names beneath the resolved
runtime-data root and rejects redirected lock directories. The bounded
text-snapshot reader refuses non-regular, oversized, or invalid UTF-8 input.
Pursuit body updates retain the original frontmatter and recheck the snapshot
before replacement. Use exclusive atomic creation when an existing destination
must not be overwritten.

Transcript ingestion coordinates multiple outputs with a versioned prepared-intent
journal in the pipeline database. It saves exact decisions before publishing
files, then applies source-owned note, pursuit, and task effects. Completion
records the artifact, marks the source processed, and removes the journal in one
database transaction only after every required effect succeeds. Reruns preserve
matching owned effects and refuse ambiguous ownership; they do not roll back
earlier file writes. The ingest replay domain owns this sequence. The shared
owned-Markdown parser validates task and pursuit list markers; the note publisher
validates its standalone ownership footer.

See [contributor patterns](../patterns/README.md) for canonical implementation
entry points and [pipeline recovery](../guides/pipeline-workflow.md#resume-interrupted-transcript-ingestion)
for the user-facing retry contract.
