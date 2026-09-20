# contact-enrichment Specification

## Purpose
Define the current behavioral contract for contact-enrichment, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Contact batches enrich concurrently within a fixed bound

The contact enrichment pipeline SHALL overlap independent eligible-contact enrichment work within each existing batch and MUST NOT use more concurrent workers than the smaller of the eligible-contact count and `BATCH_SIZE`.

#### Scenario: Multiple eligible contacts are enriched
- **GIVEN** a batch contains multiple contacts that pass exclusion checks
- **WHEN** the batch is enriched
- **THEN** their `enrich_contact()` calls overlap within a bounded executor
- **AND** no more than `BATCH_SIZE` worker calls run concurrently
- **AND** Gmail schema and index preparation runs once on the coordinator before workers open read-only cache connections

#### Scenario: No contacts are eligible
- **GIVEN** every contact in a batch is excluded
- **WHEN** the batch is enriched
- **THEN** no worker executor is created
- **AND** the enriched and failed results are empty

### Requirement: Parallel enrichment preserves deterministic coordinator behavior

The pipeline SHALL return enriched and failed contacts in input order, SHALL perform domain reconciliation and memory-file writes on the calling thread, and MUST propagate an unexpected worker failure before marking the batch complete.

#### Scenario: Workers complete out of order
- **GIVEN** eligible contacts finish enrichment in a different order from their input order
- **WHEN** the coordinator assembles the batch result
- **THEN** enriched and failed results follow input order
- **AND** memory files are written serially in that same order

#### Scenario: A worker raises unexpectedly
- **GIVEN** one eligible contact raises an unexpected exception during enrichment
- **WHEN** the coordinator consumes the batch
- **THEN** the exception propagates
- **AND** the batch checkpoint and aggregate contact file are not advanced for that batch
