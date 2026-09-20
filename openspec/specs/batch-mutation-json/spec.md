# batch-mutation-json Specification

## Purpose
Define the current behavioral contract for batch-mutation-json, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Batch mutators report completed side effects before an abort

A batch-mutating command in JSON mode SHALL emit one valid list-envelope document after an expected filesystem mutation failure, including ordered records for prior completed items and the aborting item, and SHALL exit with partial code 1.

#### Scenario: Second item fails after the first mutation completes

- **WHEN** the first item completes a durable mutation and the second item's filesystem mutation raises `OSError`
- **THEN** stdout contains exactly one valid JSON document
- **AND** `items` records the completed first item followed by the failed second item
- **AND** `count` equals the length of `items`
- **AND** `outcome` is `partial`
- **AND** `aborted_at` identifies the second item
- **AND** the command exits 1

#### Scenario: Later items remain untouched

- **WHEN** an expected filesystem mutation fails during a batch
- **THEN** the command does not attempt any item after `aborted_at`
- **AND** retry boundaries are deterministic from the emitted document

### Requirement: Partial-result errors are stable and disclosure-safe

The JSON error SHALL use stable machine-readable fields and SHALL NOT include raw exception text, a traceback, or an absolute failure path. The aborting item record SHALL state any durable sub-step known to have completed before failure.

#### Scenario: Ingest frontmatter changes before move failure

- **WHEN** `ingest route` updates an item's frontmatter and its subsequent move raises `OSError`
- **THEN** the failed item record states that frontmatter changed while the move did not complete
- **AND** the top-level error identifies the failed move operation with a stable code

### Requirement: Human and JSON modes share partial accounting

Both output modes SHALL use the same per-item outcomes and exit 1 for a contained filesystem failure. Human mode SHALL retain completed narration and add a concise diagnostic and partial summary; JSON mode SHALL reserve stdout for its single document.

#### Scenario: Human batch fails after prior success

- **WHEN** a human-mode batch completes one mutation and the next filesystem mutation fails
- **THEN** prior completion narration remains visible
- **AND** the command reports the failed item without a traceback
- **AND** a partial summary is printed
- **AND** the command exits 1

### Requirement: Unrelated failures retain canonical handling

Only expected per-item filesystem mutation failures SHALL become partial batch records. Validation, configuration, authentication, and unexpected programming failures SHALL retain their existing handlers and exit mappings.

#### Scenario: Validation fails before mutation

- **WHEN** command validation fails before a batch begins
- **THEN** no partial batch document is fabricated
- **AND** the canonical validation exit mapping remains in effect
