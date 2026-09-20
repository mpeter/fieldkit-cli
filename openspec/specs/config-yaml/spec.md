# config-yaml Specification

## Purpose
Define the current behavioral contract for config-yaml, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Operator config mutations preserve unrelated YAML presentation

Commands that mutate an existing operator-authored `config.yaml` SHALL preserve comments, mapping order, scalar quoting, and unrelated values while changing only their owned configuration fields.

#### Scenario: quota update preserves operator annotations and ordering

- **GIVEN** an existing `config.yaml` with comments, quoted scalars, and deliberately ordered unrelated keys
- **WHEN** the operator updates pipeline quota
- **THEN** those comments, scalar styles, keys, and their relative order remain present
- **AND** only `pipeline.quota.target` and `pipeline.quota.period` receive the requested values

#### Scenario: quota section is added without rewriting unrelated content

- **GIVEN** an existing mapping-form `config.yaml` without a pipeline quota section
- **WHEN** the operator sets pipeline quota
- **THEN** the quota section is added while unrelated presentation remains present

#### Scenario: owned quota mapping aliases unrelated defaults

- **GIVEN** `pipeline` or `pipeline.quota` aliases a mapping also exposed under an unrelated key
- **WHEN** the operator updates pipeline quota
- **THEN** the owned mapping is detached before mutation
- **AND** the unrelated mapping retains its original values

### Requirement: Config mutation remains atomic

The round-trip config mutation SHALL publish a complete valid YAML document through an atomic replacement only after parsing, mutation, and serialization succeed.

#### Scenario: serialization fails before publication

- **GIVEN** an existing valid `config.yaml`
- **WHEN** quota mutation cannot be serialized
- **THEN** the original file bytes remain unchanged

#### Scenario: config file does not exist

- **GIVEN** no `config.yaml` exists
- **WHEN** the operator sets pipeline quota
- **THEN** a valid config file is atomically created with the requested quota values

#### Scenario: config path is a symbolic link

- **GIVEN** `config.yaml` is a symbolic link
- **WHEN** the operator sets pipeline quota
- **THEN** the command reports a configuration error without replacing the link or its target

### Requirement: Quota cache reflects a successful mutation

After a successful quota write, subsequent reads in the same process SHALL return the newly written quota values.

#### Scenario: cached quota is replaced

- **GIVEN** quota configuration was read before the mutation
- **WHEN** the operator writes a different target and period
- **THEN** the next quota read returns the new values
