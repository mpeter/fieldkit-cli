# watch-domain-boundary Specification

## Purpose
Define ownership and dependency boundaries for morning-brief rendering so command
modules remain thin adapters and shared behavior lives in domain modules.
## Requirements
### Requirement: Morning-brief rendering is owned by the watch domain

The watch domain SHALL own quota-section rendering, project-health-section
rendering, companion-outbox pointer rendering, and top-level morning-brief
rendering. Command modules SHALL parse or adapt CLI inputs and call the domain
implementation; they SHALL NOT own or re-export these render functions.

#### Scenario: A brief is rendered through the domain owner

- **GIVEN** pursuit, quota, project-health, and alert inputs accepted by the
  current morning-brief implementation
- **WHEN** the brief command renders a brief
- **THEN** it calls the watch-domain renderer
- **AND** section content, ordering, filtering, and errors match the existing
  observable contract
- **AND** a pending companion proposal pointer retains its existing content and
  position before the footer

#### Scenario: Old command ownership is absent

- **GIVEN** the extraction is complete
- **WHEN** source and tests are inspected for the former command-layer render
  module and moved symbol paths
- **THEN** no command-layer definition, re-export, import, or patch target remains

### Requirement: Render dependencies live below the command layer

Quota calculation used by morning-brief rendering SHALL be owned by the watch
domain. The SF-policy-dependent pursuit collector SHALL remain in the pipeline
command layer, and the brief command SHALL inject that collector into the
renderer. Project classification used by morning-brief rendering SHALL be owned
by the pursuit domain. No watch-domain module SHALL import a command or SF module.

#### Scenario: Quota rendering resolves domain dependencies

- **GIVEN** the morning brief renders a quota section
- **WHEN** it receives the collector from the brief command and calculates quota
  gaps
- **THEN** quota math resolves from the watch domain
- **AND** SF-aware collection remains outside the watch domain
- **AND** collection runs with the same all-account root only after quota config
  is present
- **AND** collection errors and the existing rendered-duration boundary are
  preserved
- **AND** the existing input, result, math, and text contracts are preserved

#### Scenario: Project-health rendering resolves domain classification

- **GIVEN** the morning brief renders project health
- **WHEN** it classifies a pursuit
- **THEN** classification resolves from the pursuit domain
- **AND** the existing classification categories and rendered text are preserved

#### Scenario: Project-health dependencies are unavailable

- **GIVEN** project classification cannot be imported or project data cannot be
  read or classified
- **WHEN** the morning brief renders project health
- **THEN** the section degrades to the existing empty output
- **AND** the existing debug or warning observability is preserved
- **AND** the rest of the brief still renders

### Requirement: Watch MCP client has one domain-owned import path

The MCP session implementation SHALL live only at
`fieldkit.watch.morning_brief_mcp`. Command-layer modules and tests MUST import
that canonical domain module directly. The retired
`fieldkit.commands.watch.morning_brief_mcp` module MUST NOT be present or
importable, and its path MUST be protected by the repository's import tombstone
policy.

#### Scenario: Canonical MCP import remains available
- **GIVEN** an installed fieldkit source tree
- **WHEN** a caller imports `MCPSession` from
  `fieldkit.watch.morning_brief_mcp`
- **THEN** the canonical MCP session class is available without importing a
  command-layer compatibility module

#### Scenario: Retired command-layer import cannot return
- **GIVEN** the completed domain migration
- **WHEN** source code imports
  `fieldkit.commands.watch.morning_brief_mcp`
- **THEN** the repository import policy rejects the retired path
### Requirement: Watch implementations have canonical domain ownership

Each watch implementation helper MUST have one canonical import path beneath
`fieldkit.watch`. Command modules MAY expose Click adapters, but MUST NOT
re-export domain helpers or retain compatibility-only implementation modules.

#### Scenario: A watcher helper is imported by implementation or tests

- **WHEN** code imports a watch implementation helper
- **THEN** it imports the corresponding `fieldkit.watch` module
- **AND** no command-layer compatibility path provides that helper.

#### Scenario: A pure command compatibility path was retired by the domain migration

- **WHEN** the deleted pure command-layer compatibility path is imported
- **THEN** the import fails
- **AND** the path is recorded in the repository's tombstone policy.
