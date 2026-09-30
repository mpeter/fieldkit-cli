# agent-instruction-surface Specification

## Purpose

Define one inspectable public repository instruction hierarchy and fail-closed
verification that private harness adapters cannot become public requirements.

## Requirements

### Requirement: one root owner with reviewed scoped domain guides

The public repository SHALL include root `AGENTS.md` as contribution guidance.
The repository MAY include the reviewed `src/fieldkit/{ingest,llm,pursuit,sf,watch}/AGENTS.md`
domain guides, whose instructions are subordinate to the root guidance and apply
only within their source subtrees. Any additional exported `AGENTS.md` SHALL fail
the instruction-surface gate until its ownership and scope receive review.
Repository-local harness adapters and compatibility files SHALL remain excluded
local tool state and SHALL NOT be required by a public contribution workflow.

#### Scenario: clean public export

- **GIVEN** the exported tree contains `AGENTS.md`
- **AND** excluded repository-local harness adapter trees are absent
- **WHEN** the agent instruction surface gate runs
- **THEN** it validates the same structural contract used in the private candidate
- **AND** it exits successfully without an absent-adapter skip

#### Scenario: adapter path becomes public

- **WHEN** the public-tree policy includes a repository-local harness adapter path
- **THEN** the gate fails before contributor or release evidence can pass

#### Scenario: unreviewed domain instruction owner appears

- **WHEN** an exported `AGENTS.md` appears outside the root and five reviewed
  domain guide paths
- **THEN** the gate fails until that instruction owner's scope is explicitly reviewed

### Requirement: bounded and full quality enforce the surface

Bounded and full local quality SHALL each execute the non-skipping agent
instruction surface gate exactly once. Duplicate, combined, included, or
dynamically generated definitions of either quality target SHALL fail closed.

#### Scenario: retired mirror workflow returns

- **WHEN** a retired script or test path returns
- **OR** an included Makefile target, hook, or current specification restores
  the retired repository-local mirror workflow declaration
- **THEN** the gate fails with a deterministic structural finding

#### Scenario: later Make recipe overrides a verified quality target

- **WHEN** a second definition replaces either reviewed quality recipe
- **THEN** the instruction-surface gate fails rather than accepting the shadowed definition

### Requirement: user-level skill destinations remain separate

The repository instruction surface gate SHALL NOT prohibit supported user-level
skill installation destinations. Product behavior that installs shipped skills
for an explicitly selected harness remains governed by the skill-install
specification rather than by repository-local adapter maintenance.

#### Scenario: shipped skill installation names a supported harness

- **WHEN** product documentation or source names an explicit user-level skill destination
- **THEN** the repository instruction surface gate does not classify that reference as a retired mirror workflow
