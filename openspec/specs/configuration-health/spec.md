# configuration-health Specification

## Purpose
Define the current behavioral contract for configuration-health, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Empty and partial configuration are healthy states

Minimal initialization MUST succeed with generic values and no enabled integrations. General
diagnostics MUST validate base behavior and enabled integrations while reporting deliberately
unconfigured integrations as informational. An enabled integration with missing authentication
MUST retain exit 2.

#### Scenario: New user chooses no integrations
- **GIVEN** no prior fieldkit files, credentials, environment overrides, or network access
- **WHEN** the user performs minimal initialization and runs general diagnostics
- **THEN** only generic configuration is written under sanctioned roots
- **AND** diagnostics report the base installation healthy with integrations optional

#### Scenario: One configured integration lacks credentials
- **GIVEN** a user explicitly enables an integration without valid authentication
- **WHEN** general or service diagnostics validate it
- **THEN** the integration is reported as needing user authentication
- **AND** the command exits 2 rather than treating it as disabled

### Requirement: Shipped defaults are organization-neutral

Runtime defaults, examples, fixtures, prompts, schemas, and packaged assets MUST NOT infer an
organization, employee identity, private host/project, customer value, operator username, or home
path. Integration-specific public vendor/protocol references MAY remain only through narrowly
documented policy classifications.

#### Scenario: Public identity validation finds a marker
- **GIVEN** a tracked or packaged file contains an organization or workstation marker
- **WHEN** the identity validator runs
- **THEN** an unclassified marker fails with its path and rule identifier
- **AND** approved fictional or public integration values require path-scoped rationale
