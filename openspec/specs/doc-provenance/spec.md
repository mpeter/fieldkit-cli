# doc-provenance Specification

## Purpose
Define the current behavioral contract for doc-provenance, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Derived docs carry a machine-readable provenance marker

Every doc emitted by a stamped generator SHALL begin with a YAML frontmatter block containing `caste: derived` or `caste: summary`, a `derived_from:` list naming the source(s) it summarizes, and a `generated_by:` entry naming the regeneration command, so a reader (agent or human) can distinguish a system of record from derived exhaust without external context.

The marker lives inside the file content, so any byte-for-byte export or copy of the artifact carries it unchanged.

#### Scenario: CLI reference regen is stamped

- **GIVEN** `scripts/generate_cli_docs.py` runs in write mode
- **WHEN** `docs/cli-reference.md` is written
- **THEN** the file SHALL start with a frontmatter block containing `caste: derived`
- **AND** `derived_from:` SHALL name the live CLI help surface
- **AND** `generated_by:` SHALL name `scripts/generate_cli_docs.py`

#### Scenario: Dependency map regen is stamped

- **GIVEN** `scripts/generate_dep_map.py` runs in write mode
- **WHEN** `docs/dependency-map.md` is written
- **THEN** the file SHALL start with a frontmatter block containing `caste: derived` and a `derived_from:` list naming the analyzed sources

#### Scenario: Enrichment coverage report is stamped as a summary

- **GIVEN** enriched contacts exist and `fieldkit enrich generate-report` runs
- **WHEN** `report.md` is written to the enrich directory
- **THEN** the report content SHALL start with a frontmatter block containing `caste: summary`
- **AND** `derived_from:` SHALL name `contacts-raw.json` and `contacts-enriched.json`

#### Scenario: Morning brief is stamped on every write path

- **GIVEN** `fieldkit brief` writes `morning-brief-<date>.md` via the no-llm path, the LLM-degraded fallback path, or the synthesized path
- **WHEN** the brief file is written
- **THEN** the stored file SHALL start with a frontmatter block containing `caste: summary`
- **AND** on the degraded path the `[DEGRADED]` banner SHALL remain the first body element after the frontmatter

### Requirement: Marker rendering has one home and is injection-safe

The provenance marker SHALL be rendered only by `fieldkit.provenance.derived_doc_marker()`, which MUST emit every key and value through the sanctioned `render_raw_key_value()` / `_yaml_scalar()` YAML path — no string-formatted YAML or document-separator injection risk.

#### Scenario: Source ref containing YAML-hostile characters cannot break the block

- **GIVEN** a `derived_from` entry containing `: ` or `---`
- **WHEN** `derived_doc_marker()` renders the frontmatter
- **THEN** the value SHALL be escaped/quoted such that the block still parses as a single YAML document with the original string value intact

#### Scenario: Caste vocabulary is closed

- **GIVEN** a caller passes a caste value other than `derived` or `summary`
- **WHEN** the code is type-checked
- **THEN** mypy SHALL reject the call (`DerivedCaste` is a `Literal["derived", "summary"]`)

### Requirement: Derived docs carry a human-readable exhaust banner

Every stamped doc SHALL also contain a human-readable blockquote banner, rendered by `fieldkit.provenance.derived_doc_banner()`, stating the document is generated exhaust and not a system of record, so the caste is visible in renderers that hide frontmatter.

#### Scenario: Banner is present in rendered body

- **GIVEN** any stamped generator writes its doc
- **WHEN** the file body (after the closing `---`) is read
- **THEN** it SHALL contain the exhaust banner blockquote naming the doc as "not a system of record"

### Requirement: Systems of record are not stamped

Hand-authored docs, config files, and pursuit frontmatter SHALL NOT receive a `caste` key from this capability; absence of the `caste` key is the system-of-record signal.

#### Scenario: Regenerating docs leaves hand-authored docs untouched

- **GIVEN** the repo contains hand-authored docs (e.g. `docs/dev-conventions.md`)
- **WHEN** `make docs` runs
- **THEN** only `docs/cli-reference.md` and `docs/dependency-map.md` SHALL change
- **AND** no hand-authored doc SHALL gain a `caste:` key
